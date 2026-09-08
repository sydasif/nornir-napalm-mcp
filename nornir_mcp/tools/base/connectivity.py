"""Connectivity helpers for server-originated diagnostics.

This module contains pure, testable functions for ICMP ping and TCP reachability
checks that do not depend on Nornir or Netmiko.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any

from nornir_mcp.core.errors import ValidationError


def validate_probe_target(target: str) -> str:
    """Validate a target for ICMP ping or TCP reachability.

    Rules:
    - Strip surrounding whitespace.
    - Reject empty values.
    - Reject newline, carriage return, and control characters.
    - Reject any whitespace inside the target.
    - Accept valid IPv4 and IPv6 addresses (reject IPv6 with '%' zone index).
    - Accept conservative DNS hostnames (length <= 253, no consecutive
      dots, no leading/trailing dots).
    - Reject shell/CLI metacharacters: ; | & > < $ ` " ' \\ * ? [ ] ( ) { } , ! ~ #
      (IPv6 zone-index ``%`` is rejected earlier with a specific message.)

    Args:
        target: The target string to validate.

    Returns:
        The validated target string.

    Raises:
        ValidationError: If the target fails validation.
    """
    if not isinstance(target, str):
        raise ValidationError("Target must be a string")

    # Reject control characters (including newline and carriage return) in original target
    if any(ord(c) < 32 or ord(c) == 127 for c in target):
        raise ValidationError("Target contains control characters")

    # Strip surrounding whitespace
    target = target.strip()
    if not target:
        raise ValidationError("Target cannot be empty")

    # Reject any whitespace inside the target (after stripping)
    if re.search(r"\s", target):
        raise ValidationError("Target cannot contain whitespace")

    # Reject IPv6 zone index: '%' present (followed by interface/zone name)
    if "%" in target:
        # A colon implies an IPv6 address, so '%' is a zone index there.
        if "::" in target or ":" in target:
            raise ValidationError("IPv6 zone index is not allowed")
        else:
            raise ValidationError("Target contains forbidden characters")

    # Define forbidden shell/CLI metacharacters: ; | & > < $ ` " ' \\ * ? [ ] ( ) { } , ! ~ #
    forbidden_chars = set(";|&><$'\"\\*?[](),!~#`{}")
    if any(c in forbidden_chars for c in target):
        raise ValidationError("Target contains forbidden characters")

    # Try to parse as IP address (IPv4 or IPv6)
    try:
        ip = ipaddress.ip_address(target)
        return str(ip)
    except ValueError:
        # Not an IP address, treat as hostname
        pass

    # Validate as a conservative DNS hostname
    if len(target) > 253:
        raise ValidationError("Hostname too long (max 253 characters)")
    if target.startswith(".") or target.endswith("."):
        raise ValidationError("Hostname cannot start or end with a dot")
    if ".." in target:
        raise ValidationError("Hostname cannot contain consecutive dots")
    # Each label must be 1-63 characters, and only contain letters, digits, hyphens
    labels = target.split(".")
    for label in labels:
        if not label:
            raise ValidationError("Hostname label cannot be empty")
        if len(label) > 63:
            raise ValidationError("Hostname label too long (max 63 characters)")
        if not re.match(r"^[a-zA-Z0-9-]+$", label):
            raise ValidationError("Hostname label contains invalid characters")
        if label.startswith("-") or label.endswith("-"):
            raise ValidationError("Hostname label cannot start or end with a hyphen")

    return target


def build_ping_command(target: str, timeout_ms: int) -> list[str]:
    """Build a platform-appropriate ping command as an argv list.

    Args:
        target: The validated target hostname or IP address.
        timeout_ms: Requested timeout in milliseconds. The underlying ping
            utilities have a **1-second effective minimum**: Linux rounds up
            to whole seconds, and Darwin has no native timeout flag (the
            subprocess-level backstop in ``icmp_ping_host`` enforces the real
            deadline). Callers should not expect sub-second control.

    Returns:
        A list of strings suitable for subprocess.run.
    """
    system = platform.system().lower()
    if system == "windows":
        # Windows: ping -n 1 -w <timeout_ms> <target>
        return ["ping", "-n", "1", "-w", str(timeout_ms), target]
    elif system == "darwin":
        # Darwin/macOS: ping -c 1 <target> (no native timeout support in basic ping)
        return ["ping", "-c", "1", target]
    else:
        # Linux and other Unix-like: ping -c 1 -W <timeout_sec> <target>
        # Convert timeout_ms to seconds, minimum 1 second
        timeout_sec = max(1, round(timeout_ms / 1000))
        return ["ping", "-c", "1", "-W", str(timeout_sec), target]


def _extract_ping_rtt(output: str) -> float | None:
    """Parse the round-trip time from a single ping's stdout, if present.

    Handles the three common vendor formats:
    ``time=23.4 ms`` (Linux/GNU), ``time=23.446 ms`` (BSD/macOS), and
    ``time=23 ms`` / ``time<1 ms`` (Windows). Returns None when no numeric
    RTT can be found so the caller can fall back to wall-clock time.

    Args:
        output: The captured stdout of one ping invocation.

    Returns:
        The RTT in milliseconds, or None if it could not be parsed.
    """
    for match in re.finditer(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", output):
        return float(match.group(1))
    return None


def icmp_ping_host(
    hostname: str,
    timeout_ms: int,
    runner: Callable[[list[str], float], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[bool, float | None, str | None]:
    """Perform an ICMP echo request to a host.

    Args:
        hostname: The hostname or IP address to ping (will be validated).
        timeout_ms: Timeout in milliseconds for the ping command.
        runner: A callable that runs the command and returns a CompletedProcess.
                Defaults to subprocess.run with capture_output=True, text=True, check=False.

    Returns:
        A tuple (reachable, latency_ms, error_detail):
        - reachable: True if the host responded to ICMP echo, False otherwise.
        - latency_ms: The round-trip time in milliseconds reported by the ping
          output, or the process wall time if the RTT cannot be parsed from
          stdout (e.g. Darwin). None if unreachable.
        - error_detail: A string describing the error if unreachable, None if reachable.

    Raises:
        ValidationError: If the hostname fails validation.
    """
    # Validate the target
    validated_target = validate_probe_target(hostname)

    # Build the ping command
    command = build_ping_command(validated_target, timeout_ms)

    # Set up the runner
    if runner is None:

        def default_runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )

        runner = default_runner

    # Calculate subprocess timeout in seconds (add 2 seconds buffer)
    subprocess_timeout = timeout_ms / 1000.0 + 2.0

    start_time = time.perf_counter()
    try:
        completed = runner(command, subprocess_timeout)
    except subprocess.TimeoutExpired:
        return (False, None, "ping command timed out")
    except FileNotFoundError:
        return (False, None, "ping executable not found")
    except PermissionError:
        return (False, None, "permission denied to execute ping")
    except Exception as exc:  # pylint: disable=broad-except
        return (False, None, f"unexpected error: {exc}")
    end_time = time.perf_counter()

    # Check the return code
    if completed.returncode == 0:
        # Try to parse the real RTT from ping output before falling back to
        # process wall time (which is tens of ms of overhead). Common formats:
        #   Linux/GNU:  time=23.4 ms
        #   BSD/macOS:  time=23.446 ms
        #   Windows:    Reply from ...: time<1 ms / time=23 ms
        rtt = _extract_ping_rtt(completed.stdout)
        return (True, rtt if rtt is not None else (end_time - start_time) * 1000.0, None)
    else:
        # Try to extract a meaningful error from stderr or stdout
        error_detail = completed.stderr.strip()
        if not error_detail:
            error_detail = completed.stdout.strip()
        if not error_detail:
            error_detail = f"ping failed with return code {completed.returncode}"
        return (False, None, error_detail)


def check_tcp_host(
    hostname: str,
    port: int,
    timeout_ms: int,
    connector: Callable[[tuple[str, int], float], Any] | None = None,
) -> tuple[bool, float | None, str | None]:
    """Perform a TCP reachability check to a host:port.

    This is a server-originated reachability diagnostic. It opens a TCP
    connection (via ``socket.create_connection`` by default) and closes it
    immediately — it does NOT authenticate and does NOT execute any protocol
    or CLI commands. It is safe against ordinary unreachable results: only
    ``ValidationError`` propagates out of this function.

    Args:
        hostname: The hostname or IP address to probe (will be validated).
        port: The TCP port to connect to (1-65535 expected by the caller).
        timeout_ms: Timeout in milliseconds for the connection attempt.
        connector: A callable ``(address, timeout) -> socket`` used to open the
            connection. Defaults to ``socket.create_connection``.

    Returns:
        A tuple ``(reachable, latency_ms, error_detail)``:
        - reachable: True if the TCP connection succeeded, False otherwise.
        - latency_ms: The connect time in milliseconds if reachable, else None.
        - error_detail: A string describing the failure if unreachable, else None.

    Raises:
        ValidationError: If the hostname fails validation (allowed to propagate).
    """
    # Validate the target (allowed to propagate)
    validated_target = validate_probe_target(hostname)

    # Convert timeout to seconds for the socket layer
    timeout_sec = timeout_ms / 1000.0

    if connector is None:

        def default_connector(addr: tuple[str, int], timeout: float) -> Any:
            return socket.create_connection(addr, timeout=timeout)

        connector = default_connector

    start_time = time.perf_counter()
    try:
        conn = connector((validated_target, port), timeout_sec)
    except (OSError, TimeoutError) as exc:
        return (False, None, f"TCP connection failed: {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        return (False, None, f"unexpected error: {exc}")
    end_time = time.perf_counter()

    # Success: measure latency, close the connection, never leak the socket.
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - best-effort close
        pass

    latency_ms = (end_time - start_time) * 1000.0
    return (True, latency_ms, None)

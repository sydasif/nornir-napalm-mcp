"""Command input normalization and classification (spec §13/§14).

A read-only tool that forwards a user-supplied string to a device CLI must
never be able to inject a second command. Spec §5.3 requires the
newline-injection bypass (e.g. ``"show version\\nreload"``) to be
*structurally impossible*, so :func:`canonicalize` rejects any input
containing newlines, carriage returns, or control characters outright.

:func:`canonicalize` is pure — no device I/O — and returns a canonical
form (stripped, internal whitespace collapsed to single spaces,
lowercased) that is used for **policy matching only**. The original string
is what gets sent to devices; callers must never send the canonical form.

This module deliberately allows characters such as ``;`` (network device
CLIs treat them literally, not as shell separators); the safety boundary
is the line/control-character rejection here plus :func:`classify` below.

Classification decision table (D1/D2/D8 — normative reference)
----------------------------------------------------------------

Every device command is classified into exactly one category:

- READ_ONLY — safe to run through a read-only tool (``show``).
- SAFE_OPERATIONAL — operational but harmless (``ping``/``traceroute``).
- CONFIGURATION — changes device configuration.
- DANGEROUS — destructive; rejected by **every** tool in this build.
- BLOCKED — never runnable.
- UNKNOWN — no rule matched; denied by default.

Tool enforcement:

- ``nornir_run_command`` / ``nornir_run_commands`` (read tools) allow
  READ_ONLY and SAFE_OPERATIONAL **only**; everything else is rejected
  with guidance toward ``nornir_apply_config``.
- ``nornir_apply_config`` vetoes **only** DANGEROUS and BLOCKED lines;
  CONFIGURATION and UNKNOWN are allowed in config context.
- DANGEROUS is rejected by every tool in this build; BLOCKED is never
  runnable. Spec M4 approval may later revisit DANGEROUS.

The config-context gate is :func:`validate_config_lines`: each line is
canonicalized, expanded, and classified exactly as in read context, but
only DANGEROUS/BLOCKED lines (or shape-invalid ones) produce a
:class:`PolicyViolation`. CONFIGURATION and UNKNOWN lines pass — vetoing
UNKNOWN would make the tool unusable; bad sub-commands fail on-device
and surface in transcript parsing. An unknown platform denies **every**
line (default-deny, D8).

Rulesets exist for ``ios`` and ``eos`` **only**. Any other platform
classifies UNKNOWN — the classifier never falls through to IOS rules
(spec §13.2).

Abbreviations (spec §14.1)
---------------------------

Cisco-style CLIs accept abbreviated forms (``"wr e"`` for
``"write erase"``, ``"conf t"`` for ``"configure terminal"``, ``"rel"``
for ``"reload"``), so spec §33 demands abbreviated forms behave
identically to their full forms. Each ruleset carries a **curated**
abbreviation table (:attr:`PlatformRuleset.abbreviations`) expanded
before classification by :func:`_expand`.

Honest limits: this is a curated table, not a CLI parser. An abbreviation
missing from the table classifies by its literal tokens — which almost
always means UNKNOWN, i.e. rejected by read tools. The design fails
closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from nornir_mcp.core.errors import CommandRejectedError, ValidationError

# Maximum accepted command length in characters (spec §33 rejects
# excessively long commands).
MAX_COMMAND_LENGTH = 1024


def canonicalize(raw: str) -> str:
    """Validate and canonicalize a single-line device command.

    Args:
        raw: The raw command string as provided by the caller.

    Returns:
        The canonical form — stripped, internal whitespace collapsed to
        single spaces, lowercased — suitable for policy matching only.
        The original *raw* string is what should be sent to devices.

    Raises:
        ValidationError: If *raw* is empty/whitespace-only, contains a
            newline or carriage return, contains a control character
            (``ord < 32`` or ``127``), or exceeds ``MAX_COMMAND_LENGTH``
            characters.
    """
    if not raw:
        raise ValidationError("empty command provided")
    if "\n" in raw or "\r" in raw:
        raise ValidationError(
            "command must be a single line: newlines and carriage returns are rejected"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValidationError("command contains control characters")
    stripped = raw.strip()
    if not stripped:
        raise ValidationError("empty command provided")
    if len(raw) > MAX_COMMAND_LENGTH:
        raise ValidationError(f"command exceeds {MAX_COMMAND_LENGTH} characters")
    return " ".join(stripped.lower().split())


class CommandCategory(StrEnum):
    """Classification of a device command (spec §13/§14)."""

    READ_ONLY = "read_only"
    SAFE_OPERATIONAL = "safe_operational"
    CONFIGURATION = "configuration"
    DANGEROUS = "dangerous"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PlatformRuleset:
    """Prefix/pattern rules for one platform.

    All patterns are canonical strings (lowercase, single-spaced) compared
    against the canonicalized command on token boundaries.
    """

    read_prefixes: tuple[str, ...]
    safe_prefixes: tuple[str, ...]
    configuration_patterns: tuple[str, ...]
    dangerous_prefixes: tuple[str, ...]
    blocked: tuple[str, ...]
    abbreviations: dict[str, str]


# IOS ruleset (spec §13/§33).
IOS_RULESET = PlatformRuleset(
    read_prefixes=("show",),
    safe_prefixes=("ping", "traceroute"),
    configuration_patterns=(
        "configure terminal",
        "interface",
        "router",
        "ip route",
        "vlan",
        "description",
        "spanning-tree",
        "switchport",
        "write memory",
        "copy running-config startup-config",
    ),
    dangerous_prefixes=("reload",),
    blocked=("write erase", "erase startup-config", "format", "delete flash:"),
    abbreviations={
        "wr e": "write erase",
        "write e": "write erase",
        "conf t": "configure terminal",
        "conf ter": "configure terminal",
        "rel": "reload",
        "erase start": "erase startup-config",
    },
)


# EOS ruleset (spec §13/§33).
EOS_RULESET = PlatformRuleset(
    read_prefixes=("show",),
    safe_prefixes=("ping", "traceroute"),
    configuration_patterns=(
        "configure",
        "interface",
        "router",
        "ip route",
        "vlan",
        "description",
        "no shutdown",
        "shutdown",
    ),
    dangerous_prefixes=("reload",),
    blocked=("write erase", "zerotouch disable", "erase startup-config"),
    abbreviations={
        "wr e": "write erase",
        "conf t": "configure",
    },
)


# Supported platforms. Any platform absent from this map classifies UNKNOWN
# (spec §13.2: never fall through to another platform's rules).
RULESETS = {"ios": IOS_RULESET, "eos": EOS_RULESET}


def _expand(command: str, platform: str) -> str:
    """Expand a known abbreviated command to its full canonical form.

    The command is canonicalized first, then two expansion rules apply
    against the platform's abbreviation table:

    1. The whole canonical string is a key -> replaced by its expansion
       (so ``"wr e"`` -> ``"write erase"``).
    2. The first token alone is a key and the rest are arguments -> the
       token is replaced and the arguments preserved (so
       ``"rel in 5"`` -> ``"reload in 5"``).

    This is a curated table, not a parser: abbreviations missing from the
    table pass through unchanged and classify by their literal tokens
    (usually UNKNOWN -> rejected). Fail-closed.

    Args:
        command: The raw command string.
        platform: NAPALM platform name.

    Returns:
        The canonical command string with any known abbreviation expanded.

    Raises:
        ValidationError: If *command* fails canonicalization.
    """
    canonical = canonicalize(command)
    ruleset = RULESETS.get(platform)
    if ruleset is None:
        return canonical
    if canonical in ruleset.abbreviations:
        return ruleset.abbreviations[canonical]
    first, _, rest = canonical.partition(" ")
    if first in ruleset.abbreviations:
        return f"{ruleset.abbreviations[first]} {rest}" if rest else ruleset.abbreviations[first]
    return canonical


def _starts_with(tokens: list[str], prefix: list[str]) -> bool:
    """True if *tokens* begins with *prefix* on token boundaries."""
    return len(tokens) >= len(prefix) and tokens[: len(prefix)] == prefix


def _matches_any(tokens: list[str], patterns: tuple[str, ...]) -> bool:
    """True if *tokens* matches any canonical pattern as a token prefix."""
    return any(_starts_with(tokens, pattern.split()) for pattern in patterns)


def classify(command: str, platform: str) -> CommandCategory:
    """Classify a device command for a platform.

    The command is canonicalized and any known abbreviation expanded
    (:func:`_expand`) first — raising ``ValidationError`` for
    multi-line/control-character/empty/overlong input — then matched in
    order: BLOCKED, DANGEROUS, READ_ONLY, SAFE_OPERATIONAL,
    CONFIGURATION, else UNKNOWN. Patterns match on token boundaries, so
    e.g. ``"write erase"`` matches ``"write erase now"`` and the
    ``"reload"`` prefix matches ``"reload in 5"``.

    Args:
        command: The raw command string.
        platform: NAPALM platform name (``ios``/``eos`` have rulesets).

    Returns:
        The command's category. Unknown platforms always return UNKNOWN.

    Raises:
        ValidationError: If *command* fails canonicalization.
    """
    canonical = _expand(command, platform)
    ruleset = RULESETS.get(platform)
    if ruleset is None:
        return CommandCategory.UNKNOWN

    tokens = canonical.split()
    if _matches_any(tokens, ruleset.blocked):
        return CommandCategory.BLOCKED
    if _matches_any(tokens, ruleset.dangerous_prefixes):
        return CommandCategory.DANGEROUS
    if _matches_any(tokens, ruleset.read_prefixes):
        return CommandCategory.READ_ONLY
    if _matches_any(tokens, ruleset.safe_prefixes):
        return CommandCategory.SAFE_OPERATIONAL
    if _matches_any(tokens, ruleset.configuration_patterns):
        return CommandCategory.CONFIGURATION
    return CommandCategory.UNKNOWN


_REJECT_GUIDANCE: dict[CommandCategory, str] = {
    CommandCategory.CONFIGURATION: "Use nornir_apply_config for configuration changes.",
    CommandCategory.DANGEROUS: "Dangerous commands are rejected by every tool in this build.",
    CommandCategory.BLOCKED: "Blocked commands can never run.",
    CommandCategory.UNKNOWN: "Unknown commands are denied by default; use a known show or "
    "safe-operational command.",
}


class PolicyViolation(BaseModel):
    """One rejected line in a config list (the D2 gate).

    ``line`` is the offending line's text as provided; ``category`` is a
    string from the :class:`CommandCategory` catalog (or ``validation``
    for shape errors / ``unsupported_operation`` for unknown platforms);
    ``reason`` embeds the 1-based line number plus actionable guidance.
    """

    model_config = ConfigDict(frozen=True)

    line: str
    category: str
    reason: str


def validate_config_lines(lines: list[str], platform: str) -> list[PolicyViolation]:
    """Gate a config line list for ``nornir_apply_config`` (D2).

    Each line is canonicalized, abbreviated forms expanded, and
    classified exactly as in read context — but only DANGEROUS and
    BLOCKED classifications (plus shape errors: newlines, control
    characters, empty lines, oversized lines) produce a
    :class:`PolicyViolation`. CONFIGURATION and UNKNOWN lines produce no
    violation: bad sub-commands fail on-device and surface in transcript
    parsing, and vetoing UNKNOWN would make the tool unusable.

    Args:
        lines: The candidate configuration lines, in order.
        platform: NAPALM platform name (``ios``/``eos`` have rulesets).

    Returns:
        A list of violations, empty when the list may be applied.
        An empty *lines* list yields a single ``validation`` violation
        (nothing to apply). An unknown platform yields one
        ``unsupported_operation`` violation per line (default-deny, D8).
    """
    if not lines:
        return [
            PolicyViolation(
                line="",
                category="validation",
                reason="empty configuration provided — nothing to apply",
            )
        ]
    if platform not in RULESETS:
        supported = ", ".join(sorted(RULESETS))
        return [
            PolicyViolation(
                line=line,
                category="unsupported_operation",
                reason=(
                    f"line {index}: platform '{platform}' has no ruleset — "
                    f"configuration lines are denied by default "
                    f"(supported platforms: {supported})"
                ),
            )
            for index, line in enumerate(lines, start=1)
        ]

    violations: list[PolicyViolation] = []
    for index, line in enumerate(lines, start=1):
        try:
            expanded = _expand(line, platform)
        except ValidationError as exc:
            violations.append(
                PolicyViolation(
                    line=line,
                    category="validation",
                    reason=f"line {index} rejected: {exc}",
                )
            )
            continue
        category = classify(expanded, platform)
        if category not in (CommandCategory.DANGEROUS, CommandCategory.BLOCKED):
            continue
        if expanded == canonicalize(line):
            reason = (
                f"line {index} '{line}' is {category.value} — not permitted even in config mode"
            )
        else:
            reason = (
                f"line {index} '{line}' expands to '{expanded}' "
                f"({category.value.upper()}) — not permitted even in config mode"
            )
        violations.append(PolicyViolation(line=line, category=category.value, reason=reason))
    return violations


def assert_read_allowed(command: str, platform: str) -> None:
    """Assert *command* may run through a read-only tool (D1).

    Read-only tools (``nornir_run_command`` / ``nornir_run_commands``)
    allow READ_ONLY and SAFE_OPERATIONAL commands only; known
    abbreviations are expanded during classification, so abbreviated
    forms are judged identically to their full forms. Any other category
    raises :class:`CommandRejectedError` carrying the category and
    actionable guidance.

    Args:
        command: The raw command string.
        platform: NAPALM platform name.

    Raises:
        CommandRejectedError: If the command is not READ_ONLY or
            SAFE_OPERATIONAL.
        ValidationError: If *command* fails canonicalization.
    """
    category = classify(command, platform)
    if category in (CommandCategory.READ_ONLY, CommandCategory.SAFE_OPERATIONAL):
        return
    raise CommandRejectedError(
        f"Command '{command}' was classified as {category.value} and is not allowed "
        f"through a read-only tool. {_REJECT_GUIDANCE[category]}"
    )

"""Tests for policy.py — command canonicalization (input normalization).

Spec §5.3 requires newline-injection (e.g. ``"show version\\nreload"``)
to be structurally impossible; spec §33 rows pin empty/long command
rejection. This module is pure — no device I/O.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from nornir_mcp.errors import CommandRejectedError, ValidationError
from nornir_mcp.policy import (
    MAX_COMMAND_LENGTH,
    CommandCategory,
    PolicyViolation,
    assert_read_allowed,
    canonicalize,
    classify,
    validate_config_lines,
)


def test_canonicalize_collapses_whitespace_and_lowercases() -> None:
    """Canonical form: stripped, collapsed whitespace, lowercased."""
    assert canonicalize("  SHOW   Version   1.2  ") == "show version 1.2"
    assert canonicalize("show   version") == "show version"
    assert canonicalize("  Show Version  ") == "show version"


def test_newline_in_command_rejected() -> None:
    """A newline would let one command execute two; it must be impossible."""
    with pytest.raises(ValidationError, match="single line"):
        canonicalize("show version\nreload")
    with pytest.raises(ValidationError, match="single line"):
        canonicalize("show version\n\nreload")


def test_carriage_return_rejected() -> None:
    """Carriage returns are rejected just like newlines."""
    with pytest.raises(ValidationError, match="single line"):
        canonicalize("show version\rreload")
    with pytest.raises(ValidationError, match="single line"):
        canonicalize("show version\r\nreload")


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x07", "\t"])
def test_control_character_rejected(control: str) -> None:
    """Control characters (ord < 32 or == 127) are rejected."""
    with pytest.raises(ValidationError, match="control"):
        canonicalize(f"show version{control}")


def test_del_character_rejected() -> None:
    """DEL (0x7f) is a control character and is rejected."""
    with pytest.raises(ValidationError, match="control"):
        canonicalize("show version\x7f")


def test_empty_command_rejected() -> None:
    """Empty and whitespace-only commands are rejected (spec §33 row)."""
    with pytest.raises(ValidationError, match="empty"):
        canonicalize("")
    with pytest.raises(ValidationError, match="empty"):
        canonicalize("     ")


def test_excessively_long_command_rejected() -> None:
    """Commands longer than MAX_COMMAND_LENGTH are rejected (§33 row)."""
    with pytest.raises(ValidationError, match="exceeds"):
        canonicalize("x" * (MAX_COMMAND_LENGTH + 1))


def test_command_at_length_limit_is_allowed() -> None:
    """A command of exactly MAX_COMMAND_LENGTH characters is accepted."""
    cmd = "x" * MAX_COMMAND_LENGTH
    assert canonicalize(cmd) == cmd


def test_shell_injection_attempt_rejected() -> None:
    """'show version ; rm -rf /' canonicalizes without error.

    Semicolons are legal characters on network device CLIs — devices treat
    them literally, not as shell separators — so the safety boundary here
    is deliberately NOT character-blocking of ';'. It comes from (a) the
    newline/control-character rejection above, which makes it structurally
    impossible to escape the single command line, and (b) the command
    classification layer that consumes canonicalize. This test pins that a
    one-line string containing ';' passes normalization.
    """
    assert canonicalize("show version ; rm -rf /") == "show version ; rm -rf /"


def test_policy_module_does_not_import_nornir() -> None:
    """policy.py must stay pure (stdlib + errors only, no nornir import)."""
    code = (
        "import sys\n"
        "import nornir_mcp.policy\n"
        "imported = [m for m in sys.modules if m == 'nornir' or m.startswith('nornir.')]\n"
        "assert not imported, f'policy.py transitively imports nornir: {imported}'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# classify — command categorization (spec §13/§14 decision table)
# ---------------------------------------------------------------------------


def test_show_version_is_read_only() -> None:
    """Plain show commands are READ_ONLY on every ruleset platform."""
    for platform in ("ios", "eos"):
        assert classify("show version", platform) is CommandCategory.READ_ONLY


def test_show_ip_route_is_read_only() -> None:
    """Read prefixes match after canonicalization (case/space-insensitive)."""
    assert classify("  SHOW   IP   Route  ", "ios") is CommandCategory.READ_ONLY
    assert classify("show ip route", "eos") is CommandCategory.READ_ONLY


def test_ping_is_safe_operational() -> None:
    """Ping/traceroute are SAFE_OPERATIONAL on both rulesets."""
    for platform in ("ios", "eos"):
        assert classify("ping 10.0.0.1", platform) is CommandCategory.SAFE_OPERATIONAL
        assert classify("traceroute 10.0.0.1", platform) is CommandCategory.SAFE_OPERATIONAL


def test_interface_command_is_configuration() -> None:
    """Interface config lines are CONFIGURATION, not read-only."""
    assert classify("interface ethernet1", "ios") is CommandCategory.CONFIGURATION
    assert classify("interface ethernet1", "eos") is CommandCategory.CONFIGURATION
    assert (
        classify("no shutdown", "eos") is CommandCategory.CONFIGURATION
    )  # 'shutdown' does not shadow the read prefix


def test_copy_running_config_is_configuration() -> None:
    """Multi-token configuration patterns match on ios."""
    assert classify("copy running-config startup-config", "ios") is CommandCategory.CONFIGURATION


def test_reload_is_dangerous() -> None:
    """Plain reload is DANGEROUS on both rulesets."""
    for platform in ("ios", "eos"):
        assert classify("reload", platform) is CommandCategory.DANGEROUS


def test_reload_in_5_is_dangerous() -> None:
    """Dangerous prefix matches the first token with args allowed."""
    assert classify("reload in 5", "ios") is CommandCategory.DANGEROUS
    assert classify("reload now", "eos") is CommandCategory.DANGEROUS


def test_write_erase_is_blocked() -> None:
    """write erase is BLOCKED, including with trailing arguments."""
    for platform in ("ios", "eos"):
        assert classify("write erase", platform) is CommandCategory.BLOCKED
    # Canonical multi-token prefix match: trailing args don't escape it.
    assert classify("write erase now", "ios") is CommandCategory.BLOCKED


def test_erase_startup_config_is_blocked() -> None:
    """erase startup-config is BLOCKED on both rulesets."""
    for platform in ("ios", "eos"):
        assert classify("erase startup-config", platform) is CommandCategory.BLOCKED


def test_format_and_delete_are_blocked_on_ios() -> None:
    """format and delete flash: are ios-only blocked entries."""
    assert classify("format flash:", "ios") is CommandCategory.BLOCKED
    assert classify("delete flash:", "ios") is CommandCategory.BLOCKED


def test_zerotouch_disable_is_blocked_on_eos() -> None:
    """zerotouch disable is an eos-only blocked entry."""
    assert classify("zerotouch disable", "eos") is CommandCategory.BLOCKED


def test_unknown_command_is_unknown() -> None:
    """Commands matching no rule are UNKNOWN, not silently allowed."""
    assert classify("frobnicate", "ios") is CommandCategory.UNKNOWN
    assert classify("do something weird", "eos") is CommandCategory.UNKNOWN


def test_unknown_platform_defaults_deny() -> None:
    """Non-ruleset platforms never fall through to ios rules (§13.2/§33)."""
    assert classify("show version", "junos") is CommandCategory.UNKNOWN
    assert classify("reload", "junos") is CommandCategory.UNKNOWN
    assert classify("write erase", "junos") is CommandCategory.UNKNOWN


# ---------------------------------------------------------------------------
# assert_read_allowed — read-tool gate (D1/D2)
# ---------------------------------------------------------------------------


def test_assert_read_allowed_accepts_show() -> None:
    """READ_ONLY commands pass the read-tool gate."""
    assert_read_allowed("show version", "ios")


def test_assert_read_allowed_accepts_safe_operational() -> None:
    """SAFE_OPERATIONAL commands pass the read-tool gate."""
    assert_read_allowed("ping 10.0.0.1", "eos")


def test_assert_read_allowed_rejects_config_command() -> None:
    """CONFIGURATION is rejected with guidance to nornir_apply_config."""
    with pytest.raises(CommandRejectedError, match="nornir_apply_config"):
        assert_read_allowed("interface ethernet1", "ios")


def test_assert_read_allowed_rejects_dangerous() -> None:
    """DANGEROUS is rejected by read tools."""
    with pytest.raises(CommandRejectedError, match="dangerous"):
        assert_read_allowed("reload in 5", "ios")


def test_assert_read_allowed_rejects_blocked() -> None:
    """BLOCKED commands can never run."""
    with pytest.raises(CommandRejectedError, match="blocked"):
        assert_read_allowed("write erase", "eos")


def test_assert_read_allowed_rejects_unknown() -> None:
    """UNKNOWN commands are denied by default (§33)."""
    with pytest.raises(CommandRejectedError, match="denied by default"):
        assert_read_allowed("frobnicate", "ios")
    with pytest.raises(CommandRejectedError):
        assert_read_allowed("show version", "junos")


# ---------------------------------------------------------------------------
# Abbreviation expansion (spec §14.1/§33 — abbreviated forms behave
# identically to their full forms)
# ---------------------------------------------------------------------------


def test_abbreviated_write_erase_is_blocked_same_as_full() -> None:
    """'wr e' expands to 'write erase' and is BLOCKED (§33 row)."""
    for platform in ("ios", "eos"):
        assert classify("wr e", platform) is CommandCategory.BLOCKED
        assert classify("WR   E", platform) is CommandCategory.BLOCKED  # canonicalized
    assert classify("write e", "ios") is CommandCategory.BLOCKED
    # The read-tool gate rejects it too.
    with pytest.raises(CommandRejectedError, match="blocked"):
        assert_read_allowed("wr e", "ios")


def test_conf_t_is_configuration() -> None:
    """'conf t' / 'conf ter' expand to configuration mode entry."""
    assert classify("conf t", "ios") is CommandCategory.CONFIGURATION
    assert classify("conf ter", "ios") is CommandCategory.CONFIGURATION
    assert classify("conf t", "eos") is CommandCategory.CONFIGURATION  # -> 'configure'


def test_rel_is_dangerous() -> None:
    """'rel' expands to 'reload' and is DANGEROUS on ios."""
    assert classify("rel", "ios") is CommandCategory.DANGEROUS


def test_rel_in_5_is_dangerous() -> None:
    """Abbreviation expansion applies before dangerous matching."""
    assert classify("rel in 5", "ios") is CommandCategory.DANGEROUS


def test_abbreviation_expansion_applies_before_dangerous_matching() -> None:
    """Without expansion 'rel in 5' would classify UNKNOWN; it must not."""
    assert classify("rel in 5", "ios") is CommandCategory.DANGEROUS
    assert classify("rel", "ios") is CommandCategory.DANGEROUS
    with pytest.raises(CommandRejectedError, match="dangerous"):
        assert_read_allowed("rel in 5", "ios")


def test_erase_start_abbreviation_is_blocked_on_ios() -> None:
    """'erase start' expands to 'erase startup-config' (ios)."""
    assert classify("erase start", "ios") is CommandCategory.BLOCKED


def test_unknown_abbreviation_stays_unknown() -> None:
    """The table is curated, not a parser: unmapped abbreviations fail closed.

    'sh ver' (a common shorthand for 'show version') is not in the table,
    so it classifies by its literal tokens -> UNKNOWN -> denied. The same
    goes for abbreviations that exist only on another platform (e.g.
    'rel' on eos).
    """
    assert classify("sh ver", "ios") is CommandCategory.UNKNOWN
    assert classify("rel", "eos") is CommandCategory.UNKNOWN
    with pytest.raises(CommandRejectedError, match="denied by default"):
        assert_read_allowed("sh ver", "ios")


def test_config_lines_interface_description_allowed() -> None:
    """CONFIGURATION (and UNKNOWN) lines pass the config gate (D2)."""
    lines = ["interface GigabitEthernet0/1", "description uplink", "no shutdown"]
    assert validate_config_lines(lines, "ios") == []


def test_config_lines_with_reload_rejected() -> None:
    """DANGEROUS lines are vetoed even in config context."""
    lines = ["interface GigabitEthernet0/1", "reload", "description uplink"]
    violations = validate_config_lines(lines, "ios")
    assert len(violations) == 1
    assert violations[0].line == "reload"
    assert violations[0].category == "dangerous"
    assert "line 2" in violations[0].reason


def test_config_lines_with_wr_e_rejected() -> None:
    """Abbreviations expand inside config lists: 'wr e' is BLOCKED."""
    lines = ["interface GigabitEthernet0/1", "wr e"]
    violations = validate_config_lines(lines, "ios")
    assert len(violations) == 1
    assert violations[0].line == "wr e"
    assert violations[0].category == "blocked"
    assert "write erase" in violations[0].reason
    assert "not permitted even in config mode" in violations[0].reason


def test_config_lines_newline_rejected() -> None:
    """A shape error inside a config line becomes a validation violation."""
    violations = validate_config_lines(
        ["interface GigabitEthernet0/1", "show version\nreload"], "ios"
    )
    assert len(violations) == 1
    assert violations[0].category == "validation"
    assert violations[0].line == "show version\nreload"
    assert "line 2" in violations[0].reason


def test_config_lines_unknown_platform_all_violated() -> None:
    """Unknown platform -> every line denied by default (D8)."""
    violations = validate_config_lines(["show version", "interface Gi0/1"], "junos")
    assert len(violations) == 2
    assert all(v.category == "unsupported_operation" for v in violations)
    assert "junos" in violations[0].reason


def test_config_lines_empty_list_violation() -> None:
    """An empty config is itself a violation (nothing to apply)."""
    violations = validate_config_lines([], "ios")
    assert len(violations) == 1
    assert violations[0].category == "validation"
    assert "empty" in violations[0].reason


def test_violations_report_line_numbers_and_reasons() -> None:
    """Each violation pins the 1-based line number and an actionable reason."""
    lines = [
        "interface GigabitEthernet0/1",
        "description ok",
        "wr e",
        "reload",
    ]
    violations = validate_config_lines(lines, "ios")
    assert len(violations) == 2
    assert violations[0].line == "wr e" and "line 3" in violations[0].reason
    assert violations[0].category == "blocked"
    assert violations[1].line == "reload" and "line 4" in violations[1].reason
    assert violations[1].category == "dangerous"


def test_config_lines_control_character_rejected() -> None:
    """Control characters inside a config line are validation violations."""
    violations = validate_config_lines(["interface Gi0/1", "desc\x00ption"], "ios")
    assert len(violations) == 1
    assert violations[0].category == "validation"
    assert "line 2" in violations[0].reason


def test_config_lines_read_only_pass_through() -> None:
    """READ_ONLY lines are not vetoed in config context (only D/B are)."""
    assert validate_config_lines(["show version", "ping 8.8.8.8"], "ios") == []


def test_policy_violation_model_shape() -> None:
    """PolicyViolation carries line/category/reason and is frozen."""
    violation = PolicyViolation(line="reload", category="dangerous", reason="line 1 rejected")
    assert violation.line == "reload"
    assert violation.category == "dangerous"
    assert violation.reason == "line 1 rejected"
    assert PolicyViolation.model_config["frozen"] is True

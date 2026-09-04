"""Re-export for backwards compatibility — moved to core/policy.py."""

from nornir_mcp.core.policy import (
    EOS_RULESET,
    IOS_RULESET,
    MAX_COMMAND_LENGTH,
    CommandCategory,
    PlatformRuleset,
    PolicyViolation,
    assert_read_allowed,
    canonicalize,
    classify,
    validate_config_lines,
)

__all__ = [
    "EOS_RULESET",
    "IOS_RULESET",
    "CommandCategory",
    "MAX_COMMAND_LENGTH",
    "PlatformRuleset",
    "PolicyViolation",
    "assert_read_allowed",
    "canonicalize",
    "classify",
    "validate_config_lines",
]

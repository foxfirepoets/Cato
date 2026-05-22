"""Security regression tests for shell tool and safety scanner."""
import pytest
from cato.auth.token_checker import _DEFAULT_ALLOWED_TOOLS
from cato.safety import _classify_shell, RiskTier
from cato.config import CatoConfig


def test_shell_not_in_default_allowed_tools():
    """shell*, python.execute must not appear in the default-allowed whitelist."""
    banned = {"shell", "shell_execute", "shell.exec", "python.execute"}
    overlap = banned & set(_DEFAULT_ALLOWED_TOOLS)
    assert not overlap, f"Dangerous tools found in whitelist: {overlap}"


@pytest.mark.parametrize("verb", [
    "remove-item", "clear-content", "format-volume",
    "stop-process", "invoke-expression", "iex",
])
def test_powershell_destructive_verbs_blocked(verb):
    """Each PS destructive verb must classify as IRREVERSIBLE."""
    tier = _classify_shell({"command": f"{verb} C:\\temp\\foo"})
    assert tier == RiskTier.IRREVERSIBLE, f"{verb!r} classified as {tier}, expected IRREVERSIBLE"


def test_powershell_full_mode_default_false():
    """CatoConfig default must have powershell_full_mode == False."""
    cfg = CatoConfig()
    assert cfg.powershell_full_mode is False

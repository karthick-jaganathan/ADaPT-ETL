import subprocess
import sys
from unittest.mock import patch

from streamwright.core import cli


def test_cli_help():
    result = subprocess.run([sys.executable, "-m", "streamwright.core.cli", "--help"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    assert result.returncode == 0
    assert "mcp" in result.stdout


def test_cli_mcp_delegation():
    from unittest.mock import MagicMock
    mock_mcp = MagicMock(return_value=0)
    mock_mod = MagicMock(main=mock_mcp)
    with patch.dict(sys.modules, {"streamwright_mcp": MagicMock(), "streamwright_mcp.cli": mock_mod}):
        code = cli.main(["mcp", "--help"])
        assert code == 0
        mock_mcp.assert_called_once_with(["--help"])


def test_cli_mcp_not_installed(capsys):
    with patch.dict(sys.modules, {"streamwright_mcp.cli": None}):
        with patch("builtins.__import__", side_effect=ImportError("No module named 'streamwright_mcp'")):
            code = cli.main(["mcp"])
            assert code == 1
            captured = capsys.readouterr()
            assert "'streamwright-mcp' is not installed" in captured.err

"""Entry point: the two ways startup refuses, and the one way it proceeds."""

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_wiki.__main__ import main

_ENV_PREFIXES = ("WIKI_", "OAUTH_", "REDIS_", "MCP_", "TOOL_")


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory with none of our variables inherited."""
    for key in list(os.environ):
        if key.upper().startswith(_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_env(env_dir: Path, body: str) -> None:
    (env_dir / ".env").write_text(body, encoding="utf-8")


def test_invalid_settings_exit_with_the_validation_error(
    env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_env(env_dir, "WIKI_TOKEN=abc\n")  # no organization

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert "wiki_org_id or wiki_cloud_org_id" in capsys.readouterr().err


def test_misspelled_setting_stops_startup_with_a_suggestion(
    env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Config is otherwise valid, so without this guard the server would come
    # up with every write tool registered and nothing said about it.
    write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nWIKI_READ_ONL=true\n")

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    stderr = capsys.readouterr().err
    assert "WIKI_READ_ONL" in stderr
    assert "did you mean WIKI_READ_ONLY?" in stderr


def test_unrelated_env_file_key_does_not_stop_startup(env_dir: Path) -> None:
    write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nOPENAI_API_KEY=sk-x\n")

    server = MagicMock()
    with patch("mcp_wiki.__main__.create_mcp_server", return_value=server) as create:
        main()

    create.assert_called_once()
    # stdio takes no transport keywords in mcp 2.x — run() is overloaded per
    # transport and rejects the ones it does not know.
    server.run.assert_called_once_with("stdio")


@pytest.mark.parametrize(
    ("env", "expected_auth"),
    [
        ("WIKI_TOKEN=abc\nWIKI_ORG_ID=123\n", "auth=token"),
        ("WIKI_IAM_TOKEN=iam\nWIKI_ORG_ID=123\n", "auth=iam_token"),
        (
            "OAUTH_ENABLED=true\nOAUTH_CLIENT_ID=c\nOAUTH_CLIENT_SECRET=s\n"
            "MCP_SERVER_PUBLIC_URL=https://mcp.test\n",
            "auth=oauth",
        ),
        (
            "OAUTH_ENABLED=true\nOAUTH_CLIENT_ID=c\nOAUTH_CLIENT_SECRET=s\n"
            "MCP_SERVER_PUBLIC_URL=https://mcp.test\nWIKI_TOKEN=abc\n",
            "auth=oauth+token",
        ),
    ],
)
def test_startup_line_names_the_authentication_mode(
    env_dir: Path,
    caplog: pytest.LogCaptureFixture,
    env: str,
    expected_auth: str,
) -> None:
    # The one line an operator has to diagnose a misconfigured deployment:
    # it must say which credential the server actually picked up.
    write_env(env_dir, env)

    with (
        caplog.at_level(logging.INFO, logger="mcp_wiki"),
        patch("mcp_wiki.__main__.create_mcp_server", return_value=MagicMock()),
    ):
        main()

    assert expected_auth in caplog.records[-1].getMessage()

"""Entry point: the two ways startup refuses, and the one way it proceeds."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_wiki.__main__ import main


@pytest.fixture
def env_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env: None
) -> Path:
    """An empty working directory with none of our variables inherited."""
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


def test_a_setting_under_its_former_name_is_flagged(
    env_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_env(
        env_dir,
        "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nOAUTH_CLIENT_SECRET_EXPIRY_SECONDS=3600\n",
    )

    with (
        caplog.at_level(logging.WARNING, logger="mcp_wiki"),
        patch("mcp_wiki.__main__.create_mcp_server", return_value=MagicMock()),
    ):
        main()

    assert (
        "OAUTH_CLIENT_SECRET_EXPIRY_SECONDS is deprecated and still read; "
        "rename it to OAUTH_REGISTRATION_IDLE_SECONDS"
    ) in caplog.text


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


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("", "transport=stdio host=- allowed_hosts=-"),
        (
            "TRANSPORT=streamable-http\n",
            "host=127.0.0.1 allowed_hosts=localhost,127.0.0.1,[::1]",
        ),
        (
            "TRANSPORT=sse\nHOST=0.0.0.0\nMCP_ALLOWED_HOSTS=mcp-wiki\n",
            "host=0.0.0.0 allowed_hosts=mcp-wiki",
        ),
    ],
    ids=["stdio", "http-default", "http-listed"],
)
def test_startup_line_names_the_binding_and_the_host_check(
    env_dir: Path, caplog: pytest.LogCaptureFixture, env: str, expected: str
) -> None:
    # A 421 names the rejected Host in its own warning; this line says what
    # the server accepts, so the two can be compared without reading code.
    write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\n" + env)

    with (
        caplog.at_level(logging.INFO, logger="mcp_wiki"),
        patch("mcp_wiki.__main__.create_mcp_server", return_value=MagicMock()),
    ):
        main()

    assert expected in caplog.records[-1].getMessage()

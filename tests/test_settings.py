import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from mcp_wiki.settings import Settings, suspicious_env_keys

_ENV_PREFIXES = ("WIKI_", "OAUTH_", "REDIS_", "MCP_", "TOOL_")
_ENV_NAMES = {"HOST", "PORT", "TRANSPORT", "LOG_LEVEL"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith(_ENV_PREFIXES) or upper in _ENV_NAMES:
            monkeypatch.delenv(key, raising=False)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # ty: ignore[unknown-argument]


def test_minimal_config_with_wiki_token() -> None:
    settings = make_settings(wiki_token=SecretStr("token"), wiki_org_id="1")

    assert settings.transport == "stdio"
    assert settings.wiki_max_retries == 2
    assert settings.oauth_enabled is False


def test_iam_token_alone_is_enough() -> None:
    settings = make_settings(wiki_iam_token=SecretStr("iam-token"), wiki_org_id="1")
    assert settings.wiki_token is None


def test_requires_some_auth_when_oauth_disabled() -> None:
    with pytest.raises(ValidationError, match="wiki_token or wiki_iam_token"):
        make_settings()


def test_requires_an_org_id_when_oauth_disabled() -> None:
    # Without one the server starts and then fails on the first API call
    # with a bare ValueError out of _build_headers.
    with pytest.raises(ValidationError, match="wiki_org_id or wiki_cloud_org_id"):
        make_settings(wiki_token=SecretStr("token"))


def test_org_id_is_not_required_under_oauth() -> None:
    # Under OAuth the org arrives per request in YandexAuth.
    settings = make_settings(
        oauth_enabled=True,
        oauth_client_id="cid",
        oauth_client_secret=SecretStr("secret"),
        mcp_server_public_url="https://example.test",
    )
    assert settings.wiki_org_id is None


def test_rejects_both_org_ids() -> None:
    with pytest.raises(ValidationError, match="Only one of"):
        make_settings(
            wiki_token=SecretStr("token"),
            wiki_org_id="org-1",
            wiki_cloud_org_id="cloud-org-1",
        )


def test_allows_single_org_id() -> None:
    settings = make_settings(wiki_token=SecretStr("token"), wiki_org_id="org-1")
    assert settings.wiki_org_id == "org-1"


def test_oauth_requires_client_credentials() -> None:
    with pytest.raises(
        ValidationError, match="oauth_client_id and oauth_client_secret"
    ):
        make_settings(oauth_enabled=True)


def test_oauth_requires_public_url() -> None:
    with pytest.raises(ValidationError, match="mcp_server_public_url"):
        make_settings(
            oauth_enabled=True,
            oauth_client_id="client-id",
            oauth_client_secret=SecretStr("client-secret"),
        )


def test_oauth_full_config_needs_no_wiki_token() -> None:
    settings = make_settings(
        oauth_enabled=True,
        oauth_client_id="client-id",
        oauth_client_secret=SecretStr("client-secret"),
        mcp_server_public_url=AnyHttpUrl("https://mcp.example.com"),
    )

    assert settings.oauth_enabled is True
    assert settings.wiki_token is None


def test_max_retries_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        make_settings(wiki_token=SecretStr("token"), wiki_max_retries=-1)


def test_max_retries_zero_disables_retries() -> None:
    settings = make_settings(
        wiki_token=SecretStr("token"), wiki_org_id="1", wiki_max_retries=0
    )
    assert settings.wiki_max_retries == 0


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty working directory, so only what a test writes is read."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_env(env_dir: Path, body: str) -> None:
    (env_dir / ".env").write_text(body, encoding="utf-8")


class TestUnrelatedEnvFileKeys:
    """The env file is shared with every other tool in the directory.

    It is not this server's private config, so a key that belongs to
    something else must not stop the server from starting.
    """

    def test_unrelated_key_does_not_block_startup(self, env_dir: Path) -> None:
        write_env(
            env_dir,
            "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nOPENAI_API_KEY=sk-whatever\n",
        )

        settings = Settings()

        assert settings.wiki_org_id == "123"
        assert suspicious_env_keys() == {}

    def test_unrelated_key_under_a_shared_prefix_is_not_a_typo(
        self, env_dir: Path
    ) -> None:
        # REDIS_ is a namespace we answer to, but REDIS_URL is nobody's typo.
        write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nREDIS_URL=redis://x\n")

        assert Settings().wiki_org_id == "123"
        assert suspicious_env_keys() == {}


class TestTypoDetection:
    """What extra="forbid" used to give, in both channels rather than one."""

    def test_typo_in_the_env_file_is_reported(self, env_dir: Path) -> None:
        write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nWIKI_READ_ONL=true\n")

        assert suspicious_env_keys() == {"wiki_read_onl": "wiki_read_only"}

    def test_typo_in_a_real_environment_variable_is_reported(
        self, env_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This channel was never protected: pydantic-settings only reads the
        # names it knows, so the misspelling vanished without a trace and the
        # server ran with write tools registered.
        monkeypatch.setenv("WIKI_READ_ONL", "true")

        assert suspicious_env_keys() == {"wiki_read_onl": "wiki_read_only"}

    def test_a_correctly_spelled_setting_is_never_reported(
        self, env_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WIKI_READ_ONLY", "true")

        assert suspicious_env_keys() == {}

    def test_missing_env_file_is_fine(self, env_dir: Path) -> None:
        assert not (env_dir / ".env").exists()
        assert suspicious_env_keys() == {}

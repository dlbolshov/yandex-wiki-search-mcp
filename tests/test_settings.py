import os
from typing import Any

import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from mcp_wiki.settings import Settings

_ENV_PREFIXES = ("WIKI_", "OAUTH_", "REDIS_", "MCP_")
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
    settings = make_settings(wiki_token=SecretStr("token"))

    assert settings.transport == "stdio"
    assert settings.wiki_max_retries == 2
    assert settings.oauth_enabled is False


def test_iam_token_alone_is_enough() -> None:
    settings = make_settings(wiki_iam_token=SecretStr("iam-token"))
    assert settings.wiki_token is None


def test_requires_some_auth_when_oauth_disabled() -> None:
    with pytest.raises(ValidationError, match="wiki_token or wiki_iam_token"):
        make_settings()


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
    settings = make_settings(wiki_token=SecretStr("token"), wiki_max_retries=0)
    assert settings.wiki_max_retries == 0

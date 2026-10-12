from pathlib import Path
from typing import Any

import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from mcp_wiki.settings import Settings, renamed_env_keys, suspicious_env_keys

pytestmark = pytest.mark.usefixtures("isolated_env")


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


@pytest.mark.parametrize(
    ("transport", "oauth_enabled", "expected"),
    [
        ("stdio", False, True),
        # Over HTTP the server's filesystem is not the caller's, and without
        # OAuth the endpoint authenticates nobody: every peer that reaches
        # the port would be reading and writing the server's disk.
        ("streamable-http", False, False),
        ("sse", False, False),
        ("streamable-http", True, False),
        ("stdio", True, False),
    ],
)
def test_local_file_tools_are_stdio_only(
    transport: str, oauth_enabled: bool, expected: bool
) -> None:
    settings = make_settings(
        transport=transport,
        oauth_enabled=oauth_enabled,
        wiki_token=SecretStr("token"),
        wiki_org_id="1",
        oauth_client_id="client-id",
        oauth_client_secret=SecretStr("client-secret"),
        mcp_server_public_url=AnyHttpUrl("https://mcp.example.com"),
    )

    assert settings.include_local_uploads is expected


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
    """A misspelled setting is caught in either channel, not just the file."""

    def test_typo_in_the_env_file_is_reported(self, env_dir: Path) -> None:
        write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nWIKI_READ_ONL=true\n")

        assert suspicious_env_keys() == {"wiki_read_onl": "wiki_read_only"}

    def test_typo_in_a_real_environment_variable_is_reported(
        self, env_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pydantic-settings reads only the names it knows, so nothing else
        # notices a misspelling here — without this check the server would
        # come up with write tools registered and say nothing.
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


class TestAllowedHosts:
    """MCP_ALLOWED_HOSTS: which Host names the HTTP endpoint answers to."""

    @staticmethod
    def _settings() -> Settings:
        return make_settings(wiki_token=SecretStr("token"), wiki_org_id="1")

    def test_unset_by_default_and_the_server_listens_on_loopback(self) -> None:
        settings = self._settings()

        assert settings.mcp_allowed_hosts is None
        assert settings.host == "127.0.0.1"

    def test_a_comma_list_is_read_trimmed_and_lowercased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", " Wiki.Example.com , mcp-wiki:8000,, ")

        assert self._settings().mcp_allowed_hosts == [
            "wiki.example.com",
            "mcp-wiki:8000",
        ]

    def test_an_empty_variable_means_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", " ")

        assert self._settings().mcp_allowed_hosts is None

    def test_read_from_the_env_file(self, env_dir: Path) -> None:
        write_env(env_dir, "WIKI_TOKEN=abc\nWIKI_ORG_ID=1\nMCP_ALLOWED_HOSTS=*\n")

        assert Settings().mcp_allowed_hosts == ["*"]

    @pytest.mark.parametrize("value", ["https://wiki.example.com", "host/path"])
    def test_a_url_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # It would never equal a Host header: every request would get a 421.
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", value)

        with pytest.raises(ValidationError, match="takes host names"):
            self._settings()

    def test_a_misspelling_is_caught(
        self, monkeypatch: pytest.MonkeyPatch, env_dir: Path
    ) -> None:
        monkeypatch.setenv("MCP_ALLOWED_HOST", "mcp-wiki")

        assert suspicious_env_keys() == {"mcp_allowed_host": "mcp_allowed_hosts"}


class TestRegistrationIdleWindow:
    """OAUTH_REGISTRATION_IDLE_SECONDS: empty is the documented "off"."""

    @staticmethod
    def _settings() -> Settings:
        return make_settings(wiki_token=SecretStr("token"), wiki_org_id="1")

    def test_defaults_to_thirty_days(self) -> None:
        assert self._settings().oauth_registration_idle_seconds == 30 * 24 * 60 * 60

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_variable_disables_it(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # An environment variable cannot carry None; without the validator
        # this was an unparsable integer and the server would not start.
        monkeypatch.setenv("OAUTH_REGISTRATION_IDLE_SECONDS", value)

        assert self._settings().oauth_registration_idle_seconds is None

    def test_an_empty_env_file_entry_disables_it(self, env_dir: Path) -> None:
        write_env(
            env_dir,
            "WIKI_TOKEN=abc\nWIKI_ORG_ID=123\nOAUTH_REGISTRATION_IDLE_SECONDS=\n",
        )

        assert Settings().oauth_registration_idle_seconds is None

    def test_a_number_is_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OAUTH_REGISTRATION_IDLE_SECONDS", "3600")

        assert self._settings().oauth_registration_idle_seconds == 3600

    @pytest.mark.parametrize("value", ["3600", ""])
    def test_its_former_name_is_still_read(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # OAUTH_CLIENT_SECRET_EXPIRY_SECONDS before secrets stopped expiring;
        # deployments that set it keep their window, or keep it off.
        monkeypatch.setenv("OAUTH_CLIENT_SECRET_EXPIRY_SECONDS", value)

        expected = int(value) if value else None
        assert self._settings().oauth_registration_idle_seconds == expected
        assert renamed_env_keys() == {
            "oauth_client_secret_expiry_seconds": "oauth_registration_idle_seconds"
        }

    def test_the_new_name_wins_over_the_former_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OAUTH_REGISTRATION_IDLE_SECONDS", "100")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET_EXPIRY_SECONDS", "200")

        assert self._settings().oauth_registration_idle_seconds == 100

    def test_the_former_name_is_no_typo(
        self, monkeypatch: pytest.MonkeyPatch, env_dir: Path
    ) -> None:
        monkeypatch.setenv("OAUTH_CLIENT_SECRET_EXPIRY_SECONDS", "3600")

        assert suspicious_env_keys() == {}

    def test_nothing_is_renamed_by_default(self, env_dir: Path) -> None:
        assert renamed_env_keys() == {}

    def test_zero_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "Expire at once" or "never"? Too ambiguous to guess at.
        monkeypatch.setenv("OAUTH_REGISTRATION_IDLE_SECONDS", "0")

        with pytest.raises(ValidationError):
            self._settings()

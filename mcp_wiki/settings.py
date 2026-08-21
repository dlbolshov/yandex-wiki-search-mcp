import difflib
import os
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

# Shared so the server cannot widen it back to str and silently accept a typo.
ToolResultText = Literal["pretty", "compact", "none"]

ENV_FILE = ".env"

# Namespaces this server answers to. A key under one of these that matches no
# field is either someone else's variable or our own typo — see
# suspicious_env_keys().
SETTINGS_PREFIXES = ("wiki_", "oauth_", "redis_", "mcp_", "tool_")

# How close an unknown key must be to a real field to be called a typo rather
# than an unrelated variable. 0.8 keeps WIKI_READ_ONL (0.96 against
# wiki_read_only) and lets REDIS_URL through untouched.
TYPO_SIMILARITY = 0.8


class Settings(BaseSettings):
    # extra="ignore", not the pydantic-settings default of "forbid": the env
    # file is a directory-level convention shared with every other tool, not
    # this server's private config, so an unrelated key there must not stop
    # the server from starting. Misspelled settings are caught by
    # suspicious_env_keys(), which covers environment variables as well —
    # strictness here reaches only the file.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    stateless_http: bool = True
    json_response: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    tool_result_text: ToolResultText = "pretty"

    wiki_api_base_url: str = "https://api.wiki.yandex.net"
    wiki_web_base_url: str = "https://wiki.yandex.ru"
    wiki_token: SecretStr | None = None
    wiki_iam_token: SecretStr | None = None
    wiki_auth_scheme: Literal["OAuth", "Bearer"] = "OAuth"
    wiki_cloud_org_id: str | None = None
    wiki_org_id: str | None = None
    wiki_read_only: bool = False
    wiki_max_retries: int = Field(default=2, ge=0)

    oauth_enabled: bool = False
    oauth_store: Literal["redis", "memory"] = "memory"
    oauth_server_url: AnyHttpUrl = AnyHttpUrl("https://oauth.yandex.ru")
    oauth_use_scopes: bool = True
    oauth_client_id: str | None = None
    oauth_client_secret: SecretStr | None = None
    # Dynamic client registration is unauthenticated by protocol design, so
    # registrations that never expire accumulate without bound — in Redis as
    # well as in memory. The SDK stamps this on the record at /register and
    # rejects an expired client, and both stores drop what it marks dead.
    # None disables the expiry, and registrations then live indefinitely.
    oauth_client_secret_expiry_seconds: int | None = Field(
        default=30 * 24 * 60 * 60, ge=1
    )
    mcp_server_public_url: AnyHttpUrl | None = None
    oauth_encryption_keys: SecretStr | None = None

    redis_endpoint: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: SecretStr | None = None
    redis_pool_max_size: int = 10

    @property
    def include_local_uploads(self) -> bool:
        """Whether the local-filesystem attachment tools may be offered at all.

        Both page_upload_attachment and page_download_attachment name paths on
        the filesystem of the machine running this server, which only matches
        the caller's own files outside multi-user OAuth deployments — upload
        would read the server's files, download would write to its disk. So
        under OAuth their registration, their mention in the server
        instructions, and the pointer to download in page_read_attachment's
        description are all dropped together.
        """
        return not self.oauth_enabled

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.wiki_org_id and self.wiki_cloud_org_id:
            raise ValueError(
                "Only one of wiki_org_id or wiki_cloud_org_id may be configured."
            )

        if self.oauth_enabled:
            if not self.oauth_client_id or not self.oauth_client_secret:
                raise ValueError(
                    "oauth_client_id and oauth_client_secret must be set when oauth_enabled is True"
                )
            if not self.mcp_server_public_url:
                raise ValueError(
                    "mcp_server_public_url must be set when oauth_enabled is True"
                )
        elif not self.wiki_token and not self.wiki_iam_token:
            raise ValueError(
                "wiki_token or wiki_iam_token must be set when oauth_enabled is False"
            )
        elif not self.wiki_org_id and not self.wiki_cloud_org_id:
            # Only outside OAuth: with oauth_enabled the org arrives per
            # request in YandexAuth, and requiring it here would break a
            # legitimate multi-user deployment. Without OAuth there is no
            # other source, so the server would start and then fail on the
            # first API call with a bare ValueError from _build_headers.
            raise ValueError(
                "wiki_org_id or wiki_cloud_org_id must be set when oauth_enabled is False"
            )

        return self


def _configured_keys() -> set[str]:
    """Every setting name the process was actually given, lowercased.

    Both channels, because a typo in either one is equally silent: real
    environment variables (pydantic-settings only ever reads the names it
    knows, so nothing else notices) and the env file (whose unknown keys are
    now ignored).
    """
    keys = {name.lower() for name in os.environ}
    source = DotEnvSettingsSource(
        Settings,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
    )
    keys |= {name.lower() for name in source()}
    return keys


def suspicious_env_keys() -> dict[str, str]:
    """Configured keys that look like a misspelled setting: key -> field.

    Only keys inside this server's namespaces are considered, and only those
    close enough to a real field to be a slip rather than an unrelated
    variable — so a misspelled setting is caught in either channel while a
    REDIS_URL belonging to something else passes untouched.
    """
    known = set(Settings.model_fields)
    candidates = sorted(
        key for key in _configured_keys() - known if key.startswith(SETTINGS_PREFIXES)
    )

    suspects: dict[str, str] = {}
    for key in candidates:
        close = difflib.get_close_matches(key, known, n=1, cutoff=TYPO_SIMILARITY)
        if close:
            suspects[key] = close[0]
    return suspects

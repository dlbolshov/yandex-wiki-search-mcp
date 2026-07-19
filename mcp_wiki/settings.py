from typing import Literal

from pydantic import AnyHttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )

    host: str = "0.0.0.0"
    port: int = 8000
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    wiki_api_base_url: str = "https://api.wiki.yandex.net"
    wiki_web_base_url: str = "https://wiki.yandex.ru"
    wiki_token: SecretStr | None = None
    wiki_iam_token: SecretStr | None = None
    wiki_auth_scheme: Literal["OAuth", "Bearer"] = "OAuth"
    wiki_cloud_org_id: str | None = None
    wiki_org_id: str | None = None
    wiki_read_only: bool = False

    oauth_enabled: bool = False
    oauth_store: Literal["redis", "memory"] = "memory"
    oauth_server_url: AnyHttpUrl = AnyHttpUrl("https://oauth.yandex.ru")
    oauth_use_scopes: bool = True
    oauth_client_id: str | None = None
    oauth_client_secret: SecretStr | None = None
    mcp_server_public_url: AnyHttpUrl | None = None
    oauth_encryption_keys: SecretStr | None = None

    redis_endpoint: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: SecretStr | None = None
    redis_pool_max_size: int = 10

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

        return self

from mcp.server.auth.provider import AuthorizationCode
from pydantic import AnyUrl, BaseModel


class YandexOAuthState(BaseModel):
    redirect_uri: AnyUrl
    code_challenge: str
    scopes: list[str] | None = None
    redirect_uri_provided_explicitly: bool
    client_id: str
    resource: str | None = None  # RFC 8707 resource indicator
    # The client's own `state`, echoed back on the final redirect. Kept as
    # data rather than used as the storage key: the key must be
    # unguessable, and `state` is a CSRF nonce that travels in URLs, browser
    # history and proxy logs (RFC 6749 §10.12) — not a secret. Defaults to
    # None so records written by an older version still validate.
    client_state: str | None = None


class YandexCallbackRequest(BaseModel):
    code: str
    state: str
    cid: str | None = None


class YandexOauthAuthorizationCode(AuthorizationCode):
    yandex_auth_code: str

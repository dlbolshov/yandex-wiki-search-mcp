import json
import os

from mcp.server.auth.provider import AccessToken

from mcp_wiki.mcp.oauth.stores.crypto import FieldEncryptor
from mcp_wiki.mcp.oauth.stores.serializers import (
    EncryptedFieldSerializer,
    PydanticJsonSerializer,
)


def make_access_token() -> AccessToken:
    return AccessToken(
        token="raw-access-token",
        client_id="client-1",
        scopes=["wiki:read"],
        expires_at=1234567890,
    )


def test_pydantic_serializer_dumps_model_to_json() -> None:
    data = json.loads(PydanticJsonSerializer().dumps(make_access_token()))
    assert data["token"] == "raw-access-token"
    assert data["client_id"] == "client-1"


def test_pydantic_serializer_roundtrips_plain_values() -> None:
    serializer = PydanticJsonSerializer()
    dumped = serializer.dumps({"a": 1})
    assert serializer.loads(dumped.decode()) == {"a": 1}


def test_encrypted_serializer_encrypts_token_field() -> None:
    serializer = EncryptedFieldSerializer(FieldEncryptor([os.urandom(32)]))

    raw = serializer.dumps(make_access_token())
    stored = json.loads(raw)
    assert stored["token"] != "raw-access-token"
    assert stored["client_id"] == "client-1"

    restored = serializer.loads(raw.decode())
    assert restored["token"] == "raw-access-token"
    assert restored["client_id"] == "client-1"


def test_encrypted_serializer_encrypts_client_secret_field() -> None:
    serializer = EncryptedFieldSerializer(FieldEncryptor([os.urandom(32)]))

    raw = serializer.dumps({"client_id": "client-1", "client_secret": "s3cret"})
    stored = json.loads(raw)
    assert stored["client_secret"] != "s3cret"

    restored = serializer.loads(raw.decode())
    assert restored["client_secret"] == "s3cret"


def test_encrypted_serializer_skips_none_fields() -> None:
    serializer = EncryptedFieldSerializer(FieldEncryptor([os.urandom(32)]))

    raw = serializer.dumps({"client_id": "client-1", "client_secret": None})
    assert json.loads(raw)["client_secret"] is None
    assert serializer.loads(raw.decode())["client_secret"] is None


def test_encrypted_serializer_without_encryptor_is_plaintext() -> None:
    serializer = EncryptedFieldSerializer(None)

    raw = serializer.dumps(make_access_token())
    assert json.loads(raw)["token"] == "raw-access-token"
    assert serializer.loads(raw.decode())["token"] == "raw-access-token"


def test_encrypted_serializer_passes_through_non_dict_values() -> None:
    serializer = EncryptedFieldSerializer(FieldEncryptor([os.urandom(32)]))
    assert serializer.loads(serializer.dumps("token-hash").decode()) == "token-hash"

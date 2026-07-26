import os

import pytest
from cryptography.fernet import InvalidToken

from mcp_wiki.mcp.oauth.stores.crypto import FieldEncryptor, hash_token


def test_encrypt_decrypt_roundtrip() -> None:
    encryptor = FieldEncryptor([os.urandom(32)])
    assert encryptor.decrypt(encryptor.encrypt("secret-value")) == "secret-value"


def test_ciphertext_differs_from_plaintext() -> None:
    encryptor = FieldEncryptor([os.urandom(32)])
    assert "secret-value" not in encryptor.encrypt("secret-value")


def test_key_rotation_decrypts_old_encrypts_new() -> None:
    old_key = os.urandom(32)
    new_key = os.urandom(32)

    encrypted_with_old = FieldEncryptor([old_key]).encrypt("value")
    rotated = FieldEncryptor([new_key, old_key])
    assert rotated.decrypt(encrypted_with_old) == "value"

    reencrypted = rotated.encrypt("value")
    with pytest.raises(InvalidToken):
        FieldEncryptor([old_key]).decrypt(reencrypted)


def test_wrong_key_raises() -> None:
    encrypted = FieldEncryptor([os.urandom(32)]).encrypt("value")
    with pytest.raises(InvalidToken):
        FieldEncryptor([os.urandom(32)]).decrypt(encrypted)


def test_empty_keys_rejected() -> None:
    with pytest.raises(ValueError, match="At least one encryption key"):
        FieldEncryptor([])


def test_hash_token_is_deterministic_sha256_hex() -> None:
    digest = hash_token("token-a")
    assert digest == hash_token("token-a")
    assert digest != hash_token("token-b")
    assert len(digest) == 64
    int(digest, 16)

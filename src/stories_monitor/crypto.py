"""Password encryption at rest using Fernet, keyed from SECRET_KEY."""

from __future__ import annotations

from cryptography.fernet import Fernet

from .config import get_settings


class SecretBox:
    """Encrypts/decrypts worker account passwords. Key never leaves the environment."""

    def __init__(self, key: str | None = None):
        raw = key or get_settings().secret_key
        if not raw or raw == "REPLACE_WITH_FERNET_KEY":
            raise ValueError(
                "SECRET_KEY is not configured. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(bytes(ciphertext)).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()

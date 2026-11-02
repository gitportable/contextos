"""Fernet encryption for Slack tokens at rest. Supports key rotation: TOKEN_ENCRYPTION_KEY=newkey,oldkey"""
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings


class TokenCryptoError(RuntimeError):
    pass


def _fernet() -> MultiFernet:
    keys = [k.strip() for k in settings.token_encryption_key.split(",") if k.strip()]
    if not keys:
        raise TokenCryptoError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with `python -m app.crypto` and put it in .env."
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception as exc:  # malformed key
        raise TokenCryptoError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(blob: str) -> str:
    if not blob:
        raise TokenCryptoError("No token stored for this connection.")
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken as exc:
        raise TokenCryptoError(
            "Stored token cannot be decrypted (TOKEN_ENCRYPTION_KEY changed?). Reconnect Slack."
        ) from exc


if __name__ == "__main__":
    print(Fernet.generate_key().decode())

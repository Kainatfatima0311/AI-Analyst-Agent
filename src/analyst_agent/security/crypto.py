"""Secrets at rest, and tokens that cannot be read back.

Two separate jobs, deliberately not conflated:

* **Connection configuration is encrypted** — it has to be recovered to open a connection, so it
  is symmetric encryption (Fernet: AES-128-CBC with an HMAC, authenticated so a tampered
  ciphertext fails to decrypt rather than decrypting to something else).
* **API keys and share tokens are hashed** — nothing ever needs to read them back, only to check
  a presented value. Storing them recoverably would mean an operator with database access could
  act as any customer, and that is a different risk from being able to open their warehouse.

**The key lives in the environment, not in the database.** A key stored beside the ciphertext it
protects is obfuscation, not encryption: one backup dump contains both halves. `SECRETS_KEY` is
therefore a deployment concern, and its absence is a hard failure the first time a secret is
written rather than a silent fallback to plaintext.

Redaction is the other half of the promise. Encrypting the column is worthless if the API returns
the plaintext, so :func:`redact` decides what a caller may see and the response models are built
from *its* output rather than from the config.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken

from analyst_agent.config import get_settings
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

# Keys a caller is shown once. The prefix is stored in the clear so a key can be identified in a
# list without being recoverable from it.
KEY_PREFIX: Final = "aak_"
SHARE_PREFIX: Final = "shr_"
PREFIX_KEPT: Final = 12

# Everything that could carry a credential. Matched on the *key* rather than by looking for
# secret-shaped values, because a password that happens to look like a hostname is still a
# password.
SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "key",
        "private_key",
        "sslkey",
        "credentials",
        "dsn",
        "connection_string",
        "uri",
        "url",
    }
)

# What is safe to show, per source type. An allowlist rather than a denylist: a config field added
# next year is hidden until somebody decides it is safe, which is the right default.
SHOWN_BY_TYPE: Final[dict[str, tuple[str, ...]]] = {
    "postgres": ("host", "port", "database", "user", "schema", "sslmode"),
    "csv": ("filename", "delimiter", "encoding", "has_header"),
    "excel": ("filename", "sheet", "header_row"),
}


class SecretsUnavailableError(RuntimeError):
    """No encryption key is configured, and something needs to store a secret."""


def _fernet() -> Fernet:
    settings = get_settings()
    raw = settings.secrets_key.get_secret_value() if settings.secrets_key else ""
    if not raw.strip():
        raise SecretsUnavailableError(
            "SECRETS_KEY is not set, so a data source's credentials cannot be encrypted. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw.strip().encode())
    except (ValueError, TypeError) as exc:
        raise SecretsUnavailableError(
            "SECRETS_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)."
        ) from exc


def encrypt_config(config: dict[str, Any]) -> bytes:
    """Encrypt a connection configuration for storage.

    Serialised with ``json.dumps`` and sorted keys so the same configuration produces the same
    plaintext — which matters only for tests, but a non-deterministic plaintext makes a failing
    test hard to read.
    """
    import json

    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return _fernet().encrypt(payload)


def decrypt_config(blob: bytes) -> dict[str, Any]:
    """Recover a stored configuration.

    A tampered or wrong-key ciphertext raises rather than returning something plausible: Fernet is
    authenticated, and this surfaces that rather than swallowing it.
    """
    import json

    try:
        plaintext = _fernet().decrypt(bytes(blob))
    except InvalidToken as exc:
        raise SecretsUnavailableError(
            "a stored data source could not be decrypted - the SECRETS_KEY has changed, or the "
            "stored value was altered"
        ) from exc
    loaded = json.loads(plaintext)
    return loaded if isinstance(loaded, dict) else {}


def redact(config: dict[str, Any], source_type: str) -> dict[str, Any]:
    """The parts of a configuration a caller may see.

    Allowlisted per type, so a field nobody has classified is withheld. A password is not starred
    out and returned - it is absent, because a masked value in a JSON response is still a
    statement about its length.
    """
    shown = SHOWN_BY_TYPE.get(source_type, ())
    safe = {
        key: value
        for key, value in config.items()
        if key in shown and key.lower() not in SECRET_KEYS
    }
    withheld = sorted(set(config) - set(safe))
    if withheld:
        safe["_withheld"] = withheld
    return safe


def carries_secret(config: dict[str, Any]) -> list[str]:
    """Which keys in a configuration look like credentials.

    Used to keep the `summary` column honest: the summary sits *beside* the encrypted column, so a
    password leaking into it would defeat the encryption entirely.
    """
    return sorted(key for key in config if key.lower() in SECRET_KEYS)


def new_token(prefix: str = KEY_PREFIX) -> tuple[str, str, str]:
    """A fresh secret, its hash, and the prefix kept in the clear.

    Returns ``(token, hash, prefix)``. The token is shown to the caller once and never stored;
    only the hash goes to the database. 32 bytes from ``secrets`` - 256 bits, which is well past
    anything guessable and short enough to paste.
    """
    token = prefix + secrets.token_urlsafe(32)
    return token, hash_token(token), token[:PREFIX_KEPT]


def hash_token(token: str) -> str:
    """SHA-256 of a presented token.

    No salt, deliberately: a salted hash cannot be looked up by value, and these are
    high-entropy random tokens rather than passwords - there is no dictionary to attack.
    """
    return hashlib.sha256(token.strip().encode()).hexdigest()


def token_matches(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison, so a timing signal cannot leak the prefix of a valid key."""
    return hmac.compare_digest(hash_token(presented), stored_hash)

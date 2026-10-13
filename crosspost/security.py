"""Security primitives: password hashing, token encryption, CSRF, IP hashing, rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import RateLimitHit

# --- Passwords (stdlib scrypt; no extra dependency) ------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 12:
        problems.append("Password must be at least 12 characters.")
    if password.lower() == password or password.upper() == password:
        problems.append("Use both upper and lower case letters.")
    return problems


# --- Token encryption at rest ----------------------------------------------
def _fernet() -> Fernet:
    s = get_settings()
    if s.token_encryption_key:
        key = s.token_encryption_key.get_secret_value()
        try:
            return Fernet(key.encode())
        except ValueError:
            # Not a Fernet key (e.g. a platform-generated random secret): derive one from it.
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"token-key:" + key.encode()).digest()))
    if s.is_production:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be configured in production")
    # Development fallback: derive a key from SECRET_KEY so tokens are never stored in plaintext.
    derived = hashlib.sha256(b"token-key:" + s.secret_key.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


# --- Misc helpers -------------------------------------------------------------
def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    salt = get_settings().ip_hash_salt.get_secret_value().encode()
    return hmac.new(salt, ip.encode(), hashlib.sha256).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def rate_limited(db: Session, bucket: str, key: str, limit: int, window_seconds: int, record: bool = True) -> bool:
    """Sliding-window rate limit stored in the database (works across processes).

    Returns True when the caller is over the limit. When not limited and
    ``record`` is True, the hit is recorded.
    """
    key_hash = hashlib.sha256(f"{bucket}:{key}".encode()).hexdigest()
    since = utcnow() - timedelta(seconds=window_seconds)
    count = db.scalar(
        select(func.count()).select_from(RateLimitHit).where(
            RateLimitHit.bucket == bucket, RateLimitHit.key_hash == key_hash, RateLimitHit.created_at >= since
        )
    ) or 0
    if count >= limit:
        return True
    if record:
        db.add(RateLimitHit(bucket=bucket, key_hash=key_hash))
        db.flush()
    return False


def purge_rate_limit_hits(db: Session, older_than_seconds: int = 86400) -> int:
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    return db.execute(delete(RateLimitHit).where(RateLimitHit.created_at < cutoff)).rowcount or 0

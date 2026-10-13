"""認証まわり: 管理者パスワード、承認トークン、CSRF。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITER = 390_000


def hash_password(password: str, *, iterations: int = PBKDF2_ITER) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(it))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def new_token() -> tuple[str, str]:
    """(平文トークン, 保存用ハッシュ)。平文は通知リンクにのみ含め、DB には保存しない。"""
    raw = secrets.token_urlsafe(32)
    return raw, token_hash(raw)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf_token(session_secret: str, session_id: str) -> str:
    return hmac.new(session_secret.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def verify_chatwork_signature(webhook_token: str, body: bytes, signature: str) -> bool:
    """Chatwork Webhook 署名（HMAC-SHA256、鍵はトークンを base64 デコードしたもの）。"""
    if not webhook_token or not signature:
        return False
    key = base64.b64decode(webhook_token)
    expected = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)

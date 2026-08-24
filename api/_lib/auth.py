"""Shared account helpers: bcrypt password hashing + JWT sessions.
Mirrors the retired JS api/_lib/auth.js one-for-one."""
import os
import re
import datetime

import bcrypt
import jwt

TOKEN_TTL_DAYS = 30
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def sign_token(user_id, email):
    secret = os.environ["JWT_SECRET"]
    payload = {
        "uid": user_id,
        "email": email,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def get_authed_payload(handler):
    """Returns the decoded {uid, email} payload, or None if missing/invalid/expired."""
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        return None
    auth_header = handler.headers.get("Authorization", "") or ""
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def is_valid_email(email):
    return isinstance(email, str) and bool(EMAIL_RE.match(email)) and len(email) <= 200


def public_user(row):
    """row: (id, email, display_name)"""
    return {"id": row[0], "email": row[1], "displayName": row[2]}

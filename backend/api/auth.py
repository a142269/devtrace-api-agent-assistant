"""Authenticated demo identities; credentials are configured outside the repository."""
import hashlib
import hmac
import json
import os
import time

import jwt
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
ITERATIONS = 310_000


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or len(salt) != 32 or len(expected) != 64:
            return False
        return hmac.compare_digest(password_hash(password, salt), expected)
    except (ValueError, TypeError):
        return False


def settings() -> tuple[str, dict]:
    secret = os.getenv('DEVTRACE_JWT_SECRET', '')
    try:
        users = json.loads(os.getenv('DEVTRACE_USERS_JSON', '{}'))
    except json.JSONDecodeError as exc:
        raise RuntimeError('DEVTRACE_USERS_JSON must be valid JSON') from exc
    if len(secret.encode()) < 32 or not isinstance(users, dict) or not users:
        raise RuntimeError('Set DEVTRACE_JWT_SECRET (at least 32 bytes) and DEVTRACE_USERS_JSON')
    if any(not isinstance(k, str) or not k or len(k) > 120 or not isinstance(v, str) for k, v in users.items()):
        raise RuntimeError('Invalid configured users')
    return secret, users


def issue_token(user_id: str, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({'sub': user_id, 'iat': now, 'exp': now + 3600, 'iss': 'devtrace'}, secret, algorithm='HS256')


def authenticated_user(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials or credentials.scheme.lower() != 'bearer':
        raise HTTPException(401, '请先登录', headers={'WWW-Authenticate': 'Bearer'})
    try:
        payload = jwt.decode(credentials.credentials, request.app.state.jwt_secret, algorithms=['HS256'],
                             issuer='devtrace', options={'require': ['sub', 'iat', 'exp', 'iss']})
        user = payload['sub']
        if not isinstance(user, str) or user not in request.app.state.users:
            raise ValueError('unknown user')
        return user
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, '登录凭证无效或已过期', headers={'WWW-Authenticate': 'Bearer'})

"""Authenticated demo identities; credentials are configured outside the repository."""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
ITERATIONS = 310_000
DEFAULT_WORKSPACE = 'default'
_WORKSPACE_NAME = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
logger = logging.getLogger(__name__)


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


def parse_user_config(raw: dict) -> tuple[dict, dict]:
    """Split the configured users map into (username -> password hash, username -> workspace).

    Both formats are supported: the legacy ``{"alice": "pbkdf2_sha256$..."}`` and the
    workspace-aware ``{"alice": {"password": "pbkdf2_sha256$...", "workspace": "acme"}}``.
    Workspaces are resolved server-side only; they never travel inside the JWT.
    """
    users: dict = {}
    workspaces: dict = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or len(name) > 120:
            raise RuntimeError('Invalid configured users')
        if isinstance(value, str):
            users[name] = value
            workspaces[name] = DEFAULT_WORKSPACE
            continue
        if isinstance(value, dict):
            if not set(value) <= {'password', 'workspace'} or not isinstance(value.get('password'), str):
                raise RuntimeError('Invalid configured users')
            workspace = value.get('workspace') or DEFAULT_WORKSPACE
            if not isinstance(workspace, str) or not _WORKSPACE_NAME.fullmatch(workspace):
                raise RuntimeError('Invalid configured users')
            users[name] = value['password']
            workspaces[name] = workspace
            continue
        raise RuntimeError('Invalid configured users')
    return users, workspaces


def settings() -> tuple[str, dict, dict]:
    secret = os.getenv('DEVTRACE_JWT_SECRET', '')
    try:
        raw = json.loads(os.getenv('DEVTRACE_USERS_JSON', '{}'))
    except json.JSONDecodeError as exc:
        raise RuntimeError('DEVTRACE_USERS_JSON must be valid JSON') from exc
    if len(secret.encode()) < 32 or not isinstance(raw, dict) or not raw:
        raise RuntimeError('Set DEVTRACE_JWT_SECRET (at least 32 bytes) and DEVTRACE_USERS_JSON')
    users, workspaces = parse_user_config(raw)
    return secret, users, workspaces


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


def workspace_for(workspaces: dict, user: str) -> str:
    """Server-side workspace mapping; users missing from the map fall back to `default`."""
    return workspaces.get(user) or DEFAULT_WORKSPACE


def admin_names(users: dict) -> set:
    """Server-side role: DEVTRACE_ADMIN_USERS ∩ configured users.

    Names that are not real users are warned about and ignored; the role never
    travels inside the JWT, so a token cannot claim admin by itself.
    """
    raw = os.getenv('DEVTRACE_ADMIN_USERS', '')
    names = {item.strip() for item in raw.split(',') if item.strip()}
    unknown = names - set(users)
    if unknown:
        logger.warning('DEVTRACE_ADMIN_USERS 中的未知用户已忽略: %s', ', '.join(sorted(unknown)))
    return names & set(users)


def require_admin(request: Request,
                  credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> str:
    user = authenticated_user(request, credentials)
    if user not in admin_names(request.app.state.users):
        raise HTTPException(403, '需要管理员权限')
    return user

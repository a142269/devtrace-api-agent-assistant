"""Create an ignored .env with a local demo login; never prints secrets."""
import getpass
import hashlib
import json
import secrets
from pathlib import Path

target = Path(__file__).resolve().parent / '.env'
template = target.with_name('.env.example')
if target.exists():
    raise SystemExit('.env 已存在，未覆盖。请参照 .env.example 手动配置。')
user = input('本地演示用户名: ').strip()
password = getpass.getpass('设置本地演示密码（至少 12 位）: ')
confirmation = getpass.getpass('再次输入密码: ')
if not user or len(user) > 120 or len(password) < 12 or password != confirmation:
    raise SystemExit('用户名或密码无效，未创建配置。')
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310_000).hex()
users = json.dumps({user: f'pbkdf2_sha256${salt}${digest}'}, ensure_ascii=True, separators=(',', ':'))
base = template.read_text(encoding='utf-8')
base += f"\nDEVTRACE_JWT_SECRET={secrets.token_urlsafe(48)}\nDEVTRACE_USERS_JSON='{users}'\n"
target.write_text(base, encoding='utf-8')
print('已创建本地 .env。请填写自己的模型 API Key；规则模式可不填。')

import bcrypt
import io
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from captcha.image import ImageCaptcha

from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, CAPTCHA_TTL, CAPTCHA_MAX_ERRORS, CAPTCHA_LOCK_MINUTES
from database import SessionLocal, User
from utils.token_renewal import origin_auth_claims
from utils.token_revocation import is_token_revoked

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

captcha_store: Dict[str, Dict] = {}
ip_attempts: Dict[str, List] = {}


def _cleanup_expired_captchas():
    now = time.time()
    expired = [k for k, v in captcha_store.items() if v["expires_at"] < now]
    for k in expired:
        del captcha_store[k]


def generate_captcha():
    _cleanup_expired_captchas()
    code = str(random.randint(1000, 9999))
    captcha_id = uuid.uuid4().hex
    captcha_store[captcha_id] = {
        "code": code,
        "expires_at": time.time() + CAPTCHA_TTL,
        "used": False,
    }
    image = ImageCaptcha()
    data = image.generate(code)
    return captcha_id, data


def verify_captcha(captcha_id: str, user_code: str) -> bool:
    _cleanup_expired_captchas()
    entry = captcha_store.get(captcha_id)
    if not entry:
        return False
    if entry["used"]:
        return False
    if entry["expires_at"] < time.time():
        del captcha_store[captcha_id]
        return False
    if entry["code"] != user_code.strip():
        return False
    entry["used"] = True
    del captcha_store[captcha_id]
    return True


def check_ip_lock(client_ip: str) -> Optional[str]:
    now = time.time()
    attempts = ip_attempts.get(client_ip, [])
    recent = [t for t in attempts if now - t < CAPTCHA_LOCK_MINUTES * 60]
    ip_attempts[client_ip] = recent
    if len(recent) >= CAPTCHA_MAX_ERRORS:
        return f"操作过于频繁，请{CAPTCHA_LOCK_MINUTES}分钟后再试"
    return None


def record_ip_failure(client_ip: str):
    now = time.time()
    ip_attempts.setdefault(client_ip, []).append(now)


def clear_ip_record(client_ip: str):
    ip_attempts.pop(client_ip, None)


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


# ---------------------------------------------------------------------------
# T-08 / FR-1.1 + FR-1.5：密码强度校验的**单一来源**
#
# 修复前的问题：
#   - 注册（auth_router.py）**完全不做密码强度校验**，只判空 ——
#     绕过前端直接构造请求即可注册弱密码（前端规则形同装饰）
#   - 改密（user.py）与管理员重置（admin.py）各自写 `len < 8`，
#     与注册口径不一致，且前端改密写的是 `>= 6` → 用户输 6-7 位时
#     前端放行、后端报错
#
# 现在三处统一调用本函数；规则取自前端注册页（口径最完整的一套）：
#   1) 长度 8-16
#   2) 字母/数字/符号 至少 2 类
#   3) 不含 6 位以上连续重复字符（如 aaaaaa）
#   4) 不含 6 位升序序列（如 123456、abcdef）
# ---------------------------------------------------------------------------

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 16

_PWD_HAS_LETTER = re.compile(r"[A-Za-z]")
_PWD_HAS_DIGIT = re.compile(r"\d")
_PWD_HAS_SYMBOL = re.compile(r"""[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>/?]""")
_PWD_SIX_REPEAT = re.compile(r"(.)\1{5,}")
_PWD_ASCENDING_RUN = re.compile(
    r"012345|123456|234567|345678|456789|567890"
    r"|abcdef|bcdefg|cdefgh|defghi|efghij|fghijk"
)


def validate_password(password: str) -> Optional[str]:
    """校验密码强度。

    返回 None 表示通过；否则返回**可直接展示给用户**的中文错误信息。
    调用方统一以 HTTP 400 + 该信息响应。
    """
    if not password:
        return "密码不能为空"
    if not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        return "密码长度应为%d-%d位" % (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH)

    kinds = sum(
        bool(p.search(password))
        for p in (_PWD_HAS_LETTER, _PWD_HAS_DIGIT, _PWD_HAS_SYMBOL)
    )
    if kinds < 2:
        return "密码必须包含字母、数字、符号中至少2种"
    if _PWD_SIX_REPEAT.search(password) or _PWD_ASCENDING_RUN.search(password):
        return "请勿输入连续、重复6位以上字母或数字"
    return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if hashed_password.startswith("$2b$") or hashed_password.startswith("$2a$"):
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    import hashlib
    legacy_hash = hashlib.sha256(plain_password.encode()).hexdigest()
    return legacy_hash == hashed_password


def migrate_password_if_needed(user, plain_password: str, db) -> None:
    if not user.hashed_password.startswith("$2b$") and not user.hashed_password.startswith("$2a$"):
        user.hashed_password = get_password_hash(plain_password)
        db.commit()


def authenticate_user(db: Session, username: str, password: str):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return False
    if not user.is_active:
        return False
    return user


def create_access_token(data: dict, expires_delta: timedelta = None):
    """签发访问令牌。

    T-50 / ADR-016：默认有效期改为读 `ACCESS_TOKEN_EXPIRE_MINUTES` 这个**单一来源**
    （修复前函数内写死 15 分钟，而配置里声明 30 分钟 —— 两者不一致，
    导致"令牌寿命"无法从一个地方解释，登录端点只能靠显式传参绕开）。
    同时并入 `jti` 与 `auth_time`：滑动续期要按它们判定绝对上限、并沿用同一条续期链。
    """
    to_encode = data.copy()
    ttl = expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    now = datetime.utcnow()
    to_encode.update(origin_auth_claims())
    to_encode.update({"iat": now, "exp": now + ttl})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(status_code=401, detail="无效的认证凭证")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # T-51 / ADR-003 选 B：**真登出**。
    #
    # 修复前"登出"只是前端把 localStorage 里的令牌删掉 —— 令牌本身仍然有效，
    # 被抄走就还能用；T-50 把续期链拉到 8 小时之后，这个洞被显著放大。
    # 现在登出会把 jti 写进 token_blacklist，这里在**鉴权链最底层**统一拦截
    # （get_current_admin_user / require_user 都依赖本函数 —— 一处修改即全覆盖）。
    #
    # 为什么放在这里而不是只放中间件：中间件在 ASGI 层、按路径前缀跳过公开端点，
    # 而"令牌是否被吊销"是**身份问题**，必须跟身份校验在同一层，
    # 否则将来新增一条绕过中间件的调用路径（直接调依赖、后台任务）就会漏。
    if is_token_revoked(payload):
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception

    # T-06 / FR-2.6：令牌有效 ≠ 可以继续使用。
    # 账号被管理员禁用后，此前签发的令牌在有效期内仍会被放行
    # （JWT 无状态，默认 30 分钟；ADR-016 引入滑动续期后窗口更长），
    # 因此必须在鉴权链**最底层**统一拦截。
    # get_current_admin_user 与 require_user 都依赖本函数 —— 一处修改即全覆盖。
    #
    # 状态码选型：返回 401 而非 403。理由：
    #   1. 前端 services/api.ts 只把 401 视为"需重新登录"，会清理本地状态；
    #      返回 403 会让被禁用用户停在"看起来已登录但什么都做不了"的状态；
    #   2. 语义上该账号已不再是有效主体，与"登录失败"一致
    #      （authenticate_user 对 is_active=False 同样拒绝）。
    # 01-spec.md FR-2.6 的验收标准为"401/403 任一即可"，此处取 401。
    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已被禁用，请联系管理员")

    return user


def get_current_admin_user(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


def require_user(current_user: User = Depends(get_current_user)):
    if current_user.role == "admin":
        raise HTTPException(status_code=403, detail="管理员不能进行面试")
    return current_user

import bcrypt
import io
import random
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
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
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
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user


def get_current_admin_user(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


def require_user(current_user: User = Depends(get_current_user)):
    if current_user.role == "admin":
        raise HTTPException(status_code=403, detail="管理员不能进行面试")
    return current_user

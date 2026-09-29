import json
from typing import Dict

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from auth import (
    get_db, verify_captcha, check_ip_lock, record_ip_failure,
    clear_ip_record, get_password_hash, authenticate_user,
    migrate_password_if_needed, create_access_token, validate_password
)
from config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from utils.token_renewal_middleware import extract_bearer_token
from utils.token_revocation import revoke_token

import re
from datetime import timedelta

from database import User

router = APIRouter(prefix="/api", tags=["auth"])

EMAIL_REGEX = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'


@router.post("/register")
async def register(request: Request, db: Session = Depends(get_db)):
    body = await request.form()
    username = body.get("username")
    password = body.get("password")
    email = body.get("email")
    captcha_id = body.get("captcha_id")
    captcha_code = body.get("captcha_code")

    client_ip = request.client.host if request.client else "unknown"
    lock_msg = check_ip_lock(client_ip)
    if lock_msg:
        raise HTTPException(status_code=429, detail=lock_msg)

    if not captcha_id or not captcha_code:
        raise HTTPException(status_code=400, detail="请输入验证码")
    if not verify_captcha(captcha_id, captcha_code):
        record_ip_failure(client_ip)
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新重试")

    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    # T-08 / FR-1.1：密码强度必须由**后端**强制。
    # 修复前此处只判空，绕过前端直接构造请求即可注册任意弱密码。
    # 校验放在验证码之后，以免削弱验证码这道防机器人闸门。
    password_error = validate_password(password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)

    username_pattern = re.compile(r'^[\u4e00-\u9fa5a-zA-Z0-9_]{3,16}$')
    if not username_pattern.match(username):
        raise HTTPException(status_code=400, detail="用户名必须为3-16位字母、数字、下划线或中文")
    if not email:
        raise HTTPException(status_code=400, detail="邮箱不能为空")
    if not re.match(EMAIL_REGEX, email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="用户名已存在")
    existing_email = db.query(User).filter(User.email == email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail="邮箱已被注册")
    hashed = get_password_hash(password)
    user = User(username=username, hashed_password=hashed, email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"msg": "注册成功", "user_id": user.id}


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    body = await request.form()
    username = body.get("username")
    password = body.get("password")
    captcha_id = body.get("captcha_id")
    captcha_code = body.get("captcha_code")

    client_ip = request.client.host if request.client else "unknown"
    lock_msg = check_ip_lock(client_ip)
    if lock_msg:
        raise HTTPException(status_code=429, detail=lock_msg)

    if not captcha_id or not captcha_code:
        raise HTTPException(status_code=400, detail="请输入验证码")
    if not verify_captcha(captcha_id, captcha_code):
        record_ip_failure(client_ip)
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新重试")

    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    user = authenticate_user(db, username, password)
    if not user:
        record_ip_failure(client_ip)
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    clear_ip_record(client_ip)
    migrate_password_if_needed(user, password, db)
    # T-50：把 user_id 一并签进令牌（续期时逐字复制身份，不再回查库拼装），
    # jti / auth_time 由 create_access_token 统一并入（绝对上限的起算点）。
    access_token = create_access_token(
        data={"sub": user.username, "user_id": user.id, "role": user.role},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user_id": user.id, "role": user.role}


@router.post("/logout")
async def logout(request: Request):
    """T-51 / ADR-003 选 B：**服务端吊销**令牌（真登出）。

    修复前"退出登录"只有前端动作：删掉 localStorage 里的令牌。
    令牌本身仍然有效 —— 被抄走（共享电脑、浏览器同步、日志泄漏）就还能用到 expiry，
    而 T-50 之后这个窗口最长 8 小时。现在把 `jti` 写进 `token_blacklist`，
    此后**任何**带这枚令牌的请求都会在 `get_current_user` 里被拒。

    ## 刻意不要求"令牌必须有效"

    不过 `Depends(get_current_user)`：用户可能拿着**已过期**的令牌来登出
    （页面挂了很久再点退出），要求有效会让登出 401、前端却只当网络错误。
    这里**只要签名正确**就吊销 —— 吊销过期令牌是无害的（那条记录按 8 小时下限留痕，
    而令牌本身早就无效），但"登出必须成功"是硬要求。

    ## 返回体

    永远 **200**。三种情况分别给出可解释的字段（前端不需要为此分支出错路径）：
      * 正常带 `jti` → `revoked: true`；
      * 老令牌（无 `jti`，T-50 之前签发）→ `revoked: false, reason: "no_jti"`，
        提示需重新登录才能完成吊销；
      * 压根没带 / 带坏令牌 → `revoked: false, reason: "no_token"`（登出依然成功）。
    """
    token = extract_bearer_token(request.headers.get("authorization"))
    if not token:
        return {"revoked": False, "reason": "no_token"}

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
    except JWTError:
        return {"revoked": False, "reason": "invalid_token"}

    expires_at = revoke_token(payload)
    if expires_at is None:
        return {"revoked": False, "reason": "no_jti"}

    return {"revoked": True, "expires_at": expires_at}

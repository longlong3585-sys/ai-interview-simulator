import json
from typing import Dict

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import (
    get_db, verify_captcha, check_ip_lock, record_ip_failure,
    clear_ip_record, get_password_hash, authenticate_user,
    migrate_password_if_needed, create_access_token
)
from config import ACCESS_TOKEN_EXPIRE_MINUTES

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
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user_id": user.id, "role": user.role}

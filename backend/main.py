import io
import logging
import os
import json
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from auth import generate_captcha, get_password_hash, get_db
from database import SessionLocal, User
from routers import auth_router, interview, admin, user
from utils.ai_helpers import load_question_bank
from utils.log_setup import configure_logging

configure_logging()
logger = logging.getLogger("app")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Captcha-Id"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.include_router(auth_router.router)
app.include_router(interview.router)
app.include_router(admin.router)
app.include_router(user.router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """T-12 / NFR-2：未捕获异常必须**留下堆栈**，但对外只给通用消息。

    修复前这里既不记录日志也不带任何上下文 —— 异常被完全吞掉，
    线上出问题只能看到一句"服务器内部错误"，无从排障。

    现在：
      - 服务端：ERROR 级别 + 完整 traceback + 请求方法/路径/客户端 IP
      - 对外：通用消息（不含异常类型、消息与堆栈，避免信息泄露）
      - 附带 error_id，便于用户报障时与日志对上
    """
    error_id = uuid.uuid4().hex[:12]
    client = request.client.host if request.client else "unknown"
    logger.error(
        "未捕获异常 [error_id=%s] %s %s client=%s",
        error_id,
        request.method,
        request.url.path,
        client,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误，请稍后重试",
            "error_id": error_id,
        },
    )


@app.on_event("startup")
def init_admin():
    db = SessionLocal()
    admin_user = db.query(User).filter(User.username == "admin").first()
    if not admin_user:
        admin_user = User(
            username="admin",
            hashed_password=get_password_hash("admin123"),
            role="admin",
            email="admin@system.local",
            is_active=True
        )
        db.add(admin_user)
        db.commit()
        print("管理员账号创建成功：用户名 admin，密码 admin123")
    db.close()


@app.get("/api/captcha")
def get_captcha():
    captcha_id, image_data = generate_captcha()
    return StreamingResponse(
        io.BytesIO(image_data.getvalue()),
        media_type="image/png",
        headers={"X-Captcha-Id": captcha_id}
    )


@app.get("/api/question_bank")
def get_question_bank():
    return load_question_bank()


@app.get("/")
def root():
    return {"message": "Hello World from AI Interview Backend"}

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
from utils.token_renewal_middleware import TokenRenewalMiddleware

configure_logging()
logger = logging.getLogger("app")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # T-50 / ADR-016 第 2 点：**必须**把这两个头列进来，否则跨域下浏览器
    # 不会把它们交给前端 JS —— 续期令牌"后端发了但前端读不到"，滑动续期静默失效。
    # 注意：CORS 来源不得配成通配 `*`（credentials 模式下浏览器不允许读通配来源的响应头）；
    # 本项目已选同源托管（见 docs/33 §5），这里保留显式白名单即可。
    # T-51 追加 `X-Token-Revoked`（真登出的标记头）。
    expose_headers=["X-Captcha-Id", "X-Refreshed-Token", "X-Token-Expired", "X-Token-Revoked"],
)

# T-50 / ADR-016：滑动续期 + 8 小时绝对上限。
#
# 位置：注册在 CORS **之后** ⇒ Starlette 里"后注册者在外层" ⇒ 实际执行顺序是
# `ServerError → 续期中间件 → CORS → 路由`。
# 因此本中间件必须自己处理两件事（否则会绕过 CORS）：
#   · `OPTIONS` 预检**直接放行**给内层 CORS（预检不带 Authorization，本来也无从续期）；
#   · 401（绝对上限）**直接返回**，不走内层 —— 这是刻意的：
#     `X-Token-Expired` 与 `X-Refreshed-Token` 都由 CORS 的 `expose_headers` 暴露，
#     同源托管下前端读得到；将来若改成分域，需要把本中间件改到 CORS 内层
#     或显式补 CORS 响应头（`docs/03-tasks.md` 的 T-35 备选）。
app.add_middleware(TokenRenewalMiddleware)

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

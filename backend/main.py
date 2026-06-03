import io
import os
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from auth import generate_captcha, get_password_hash, get_db
from database import SessionLocal, User
from routers import auth_router, interview, admin, user
from utils.ai_helpers import load_question_bank

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
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试"}
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

"""测试共用工具（T-05 抽取）。

`test_chat_auth` / `test_resume_upload_auth` 等都需要"造用户 + 签令牌 + 清理"，
抽到此处避免重复。后续 T-06/T-07/T-08 继续复用。

注意：所有写操作都落在 tests/__init__.py 建立的**临时测试库**上。
"""

from datetime import timedelta

from auth import create_access_token
from database import SessionLocal, User


def create_test_user(username, role="user", is_active=True):
    """在测试库创建用户，返回其 id。"""
    db = SessionLocal()
    try:
        user = User(
            username=username,
            hashed_password="not-a-real-hash",
            email="%s@test.local" % username,
            role=role,
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def mint_token(username, role="user", minutes=30):
    """签发测试用 JWT（与生产同一函数、同一密钥）。"""
    return create_access_token(
        {"sub": username, "role": role},
        expires_delta=timedelta(minutes=minutes),
    )


def bearer(token):
    """构造 Authorization 头；token 为 None 时返回空 dict。"""
    return {"Authorization": "Bearer %s" % token} if token else {}


def delete_users(usernames):
    """清理测试用户，保证用例无副作用、可重复运行。"""
    db = SessionLocal()
    try:
        db.query(User).filter(User.username.in_(list(usernames))).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def set_user_active(username, active):
    """切换用户的 is_active（模拟管理员启用/禁用）。返回是否找到了该用户。"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            return False
        user.is_active = bool(active)
        db.commit()
        return True
    finally:
        db.close()


def get_user_active(username):
    """读取用户当前的 is_active；用户不存在返回 None。"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        return None if user is None else bool(user.is_active)
    finally:
        db.close()


def make_docx_bytes(lines):
    """内存中生成一份真实 DOCX，用于上传测试（不落盘）。"""
    import io

    from docx import Document

    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"

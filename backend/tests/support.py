"""测试共用工具（T-05 抽取）。

`test_chat_auth` / `test_resume_upload_auth` 等都需要"造用户 + 签令牌 + 清理"，
抽到此处避免重复。后续 T-06/T-07/T-08 继续复用。

注意：所有写操作都落在 tests/__init__.py 建立的**临时测试库**上。
"""

import os
import shutil
import time

from datetime import timedelta

from jose import jwt

from auth import create_access_token
from config import ALGORITHM, SECRET_KEY
from database import SessionLocal, User

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")


def versions_dir_up_to(revision, tmp_root):
    """造一个只含 `001..<revision>` 的临时迁移目录，返回其路径。

    为什么需要：迁移测试应当**只验证自己那一版**的影响范围。
    若一律跑完整链条，后续版本对同一张表的合法改动会把先前版本的用例弄红
    —— T-17 就踩过一次（003 落地后，002 的"不得改动既有表"用例失败），
    T-19 加 004 时又会重演。按版本切片是最省事且最不容易误判的做法。
    """
    target = os.path.join(tmp_root, "versions_up_to_%s" % revision)
    os.makedirs(target, exist_ok=True)
    copied = []
    for name in sorted(os.listdir(VERSIONS_DIR)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        if name[:3] > revision:
            continue
        shutil.copy(os.path.join(VERSIONS_DIR, name), os.path.join(target, name))
        copied.append(name[:3])
    if revision not in copied:
        raise AssertionError(
            "版本目录里没有 %s（找到 %s）—— 用例写错版本号了" % (revision, copied)
        )
    return target


def build_engine(db_path):
    """按 **T-15 的 engine 契约**建一个指向 `db_path` 的独立 engine。

    刻意复用 `database._apply_sqlite_pragmas` 与 `SQLITE_CONNECT_ARGS`，
    而不是在测试里重写一份 PRAGMA —— 重写一份就会**慢慢漂移**，
    最终测的是一套与生产不同的引擎契约。
    """
    from sqlalchemy import create_engine, event

    import database

    engine = create_engine(
        "sqlite:///" + str(db_path).replace("\\", "/"),
        connect_args=dict(database.SQLITE_CONNECT_ARGS),
        pool_pre_ping=True,
        isolation_level=None,
    )
    event.listen(engine, "connect", database._apply_sqlite_pragmas)
    # ⚠️ T-15 修订：**不再**注册全局 `begin` -> `BEGIN IMMEDIATE` 监听器。
    # 写路径由 `services/stores/_sqlite_tx.begin_write` 显式取锁。
    # 这里若还留着全局监听器，测试就会跑在一套与生产不同的并发语义上，
    # 并且会**掩盖** T-23 遇到的那个"同一请求内自锁"问题。
    return engine


def build_temp_db(db_path):
    """建一个**跑完全部迁移**的临时库，返回带 T-15 契约的 engine。"""
    from migrations.runner import run

    run(db_path, backup=False, log=lambda *a, **k: None)
    return build_engine(db_path)


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
    """签发测试用 JWT（与生产同一函数、同一密钥）。

    说明：`create_access_token` 自 T-50 起会并入 `jti` / `auth_time`，
    因此这里签出的令牌**默认就会自动续期**（30 分钟寿命，签发即剩满额，
    但测试里若把 `minutes` 调小，跨过半衰点就会带上续期响应头）。
    绝大多数用例只需要"一个能通过鉴权的令牌"，续期与否无关紧要；
    需要**精确控制续期行为**的用例请用 `mint_renewable_token`。
    """
    return create_access_token(
        {"sub": username, "role": role},
        expires_delta=timedelta(minutes=minutes),
    )


def mint_renewable_token(username, role="user", minutes=30, age_minutes=0, user_id=None, jti=None):
    """T-50：签一个**可续期**的令牌，并显式控制"它已经活了多久 / 还能活多久"。

    这是本任务唯一能被测试的办法 —— "剩余有效期 < 50%" 与 "auth_time 超过 8 小时"
    都需要把签发时刻往回调，而真实等待 8 小时显然不可行。

    参数：
      * `age_minutes` —— 已经过去的分钟数：`iat = auth_time = now - age`；
      * `minutes`     —— **总寿命**：`exp = iat + minutes`。
        因此"剩余时间 = minutes - age_minutes"。要让令牌**仍然有效**，必须
        `age_minutes < minutes`；`age_minutes > minutes` 就是一枚已过期的令牌。

    例：
      * 刚签发、剩 29/30 → `minutes=30, age_minutes=1`
      * 走到半衰点之后（剩 9/30）→ `minutes=30, age_minutes=21`
      * **超过 8 小时绝对上限但令牌本身仍有效** → `minutes=600, age_minutes=540`
        （寿命 10 小时、已活 9 小时、还剩 1 小时）
    """
    now = int(time.time())
    issued_at = now - int(age_minutes * 60)
    payload = {"sub": username, "role": role}
    if user_id is not None:
        payload["user_id"] = user_id
    payload["iat"] = issued_at
    payload["exp"] = issued_at + int(minutes * 60)
    payload["auth_time"] = issued_at
    payload["jti"] = jti or ("t50-%s-%s" % (username, issued_at))
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


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

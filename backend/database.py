import os
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Date
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./interview.db")
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    role = Column(String, default="user")
    email = Column(String, unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    nickname = Column(String, nullable=True)
    avatar = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    gender = Column(String, nullable=True)
    birthday = Column(Date, nullable=True)

class InterviewRecord(Base):
    __tablename__ = "interview_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String)
    messages = Column(Text)  # 存 JSON 字符串
    report = Column(Text)    # 存 JSON 字符串
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String, default="pending")  # pending / approved / rejected
    admin_comment = Column(Text, nullable=True)  # 管理员备注

    user = relationship("User", back_populates="records")

User.records = relationship("InterviewRecord", back_populates="user")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    type = Column(String, default="system")  # interview_approved / interview_rejected / new_comment / system
    message = Column(Text)
    target_type = Column(String, nullable=True)   # interview_record
    target_id = Column(Integer, nullable=True)     # 对应 interview_records.id
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="notifications")

User.notifications = relationship("Notification", back_populates="user")

# ---------------------------------------------------------------------------
# T-14：建表与结构变更已全部交由 backend/migrations/ 管理。
#
# 原先这里在 **import 期**执行 `Base.metadata.create_all()` 外加一段手写
# `ALTER TABLE notifications ADD COLUMN ...`。问题：
#   1. 没有版本记录 —— 无法知道某个库处于哪一版结构；
#   2. import 有副作用 —— 任何 `import database` 都可能改动线上库；
#   3. 手写 ALTER 无法表达"删除列/改类型"等变更，只能一路加列
#      （`notifications.link_url` 这个死列就是这么来的）。
#
# 现在：结构由迁移脚本管理，入口是 `python backend/scripts/migrate.py`。
# 应用启动**不再自动建表** —— 这是有意的：启动即改结构是危险的默认行为。
# 部署流程应显式执行迁移（见 docs/03-tasks.md 的 runbook）。
# ---------------------------------------------------------------------------

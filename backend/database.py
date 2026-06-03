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

# 创建所有表
Base.metadata.create_all(bind=engine)

# SQLite 迁移：为已有 notifications 表添加新字段
if DATABASE_URL.startswith("sqlite"):
    import sqlite3
    conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
    cursor = conn.cursor()
    existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(notifications)").fetchall()]
    if "type" not in existing_cols:
        cursor.execute("ALTER TABLE notifications ADD COLUMN type VARCHAR DEFAULT 'system'")
    if "link_url" not in existing_cols:
        cursor.execute("ALTER TABLE notifications ADD COLUMN link_url VARCHAR")
    if "target_type" not in existing_cols:
        cursor.execute("ALTER TABLE notifications ADD COLUMN target_type VARCHAR")
    if "target_id" not in existing_cols:
        cursor.execute("ALTER TABLE notifications ADD COLUMN target_id INTEGER")
    conn.commit()
    conn.close()
import datetime
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, event, Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Date
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

logger = logging.getLogger("app.db")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./interview.db")
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# ---------------------------------------------------------------------------
# T-15 / ADR-002：SQLite 运行契约
#
# 修复前的 engine 只设了 `check_same_thread=False`，缺了 WAL、busy_timeout 与
# 外键开关，后果：
#   1. 默认 journal 模式下读写互斥，多 worker 极易 database is locked；
#   2. **PRAGMA foreign_keys 默认为 0** —— 模型里声明的 ForeignKey 形同虚设，
#      删除用户不会级联、插入孤儿行不会报错；
#   3. 未显式指定 isolation_level 时 SQLAlchemy 发的是 "BEGIN (implicit)"，
#      **永远不会发 BEGIN IMMEDIATE** —— 而 ADR-004 的并发控制正依赖它：
#      读事务升级为写时若期间他人已提交，SQLite 返回 SQLITE_BUSY 且
#      **busy handler 不被调用**，busy_timeout 完全失效。
#
# 取值依据（ADR-002）：busy_timeout 与驱动层 timeout 必须**同一个值**，
# 否则两者不一致会造成"以为等了 15 秒其实 5 秒就失败"的错觉。
# ---------------------------------------------------------------------------
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "15000"))


def _apply_sqlite_pragmas(dbapi_conn, _connection_record):
    """在**每个物理连接**建立时设置 PRAGMA，并关闭 pysqlite 的隐式事务管理。"""
    # ------------------------------------------------------------------
    # 关闭 pysqlite 自身的隐式事务管理（SQLAlchemy 官方推荐配方的一环）。
    #
    # 实测教训：只在 create_engine 上传 `isolation_level=None` **是不够的** ——
    # 那只把 SQLAlchemy 层置为 autocommit，底层 DBAPI 仍是 legacy 模式
    # （实测 `dbapi_connection.isolation_level` 依然是 ''）。
    # legacy 模式的两个副作用会与手工发的 BEGIN IMMEDIATE 冲突：
    #   1. 在 DML 前自行隐式 BEGIN
    #   2. **在 DDL 前自动 COMMIT** —— 会破坏显式事务的原子性
    # 置为 None 后，事务控制完全由 `begin` 事件负责。
    #
    # 安全性：pysqlite 的 commit() 内部会查 sqlite3_get_autocommit() 再发 COMMIT，
    # 不依赖自身簿记，所以置 None 后 SQLAlchemy 的 commit/rollback 依然有效
    # （由 BeginImmediateTests 的用例覆盖）。
    # ------------------------------------------------------------------
    dbapi_conn.isolation_level = None

    cursor = dbapi_conn.cursor()
    try:
        # WAL：允许"多读 + 单写"并发。它写入库文件、只需设置一次，
        # 但每连接设置是幂等的，且能保证新库也生效。
        cursor.execute("PRAGMA journal_mode=WAL")
        row = cursor.fetchone()
        mode = (row[0] if row else "") or ""
        if str(mode).lower() != "wal":
            # 忙时可能静默失败（例如同时有其它连接持锁），必须留下痕迹
            logger.warning("journal_mode 未能设为 WAL，实际为 %r —— 并发写入可能受限", mode)

        # 写锁等待时间。与 connect_args['timeout'] 保持同值。
        cursor.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
        # WAL 下 NORMAL 是安全与性能的平衡点（FULL 每次提交都 fsync）
        cursor.execute("PRAGMA synchronous=NORMAL")
        # **这条最关键**：SQLite 默认关闭外键，模型里的 ForeignKey 之前根本没生效
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


if _IS_SQLITE:
    # 提为模块常量，便于测试断言"busy_timeout 与驱动层 timeout 同值"（单一来源）
    SQLITE_CONNECT_ARGS = {
        "check_same_thread": False,
        "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000.0,
    }
    engine = create_engine(
        DATABASE_URL,
        connect_args=SQLITE_CONNECT_ARGS,
        # 注意：对本地 SQLite 而言 pool_pre_ping 意义有限（无网络中断），
        # 保留是为了将来切到 PostgreSQL 时行为一致。
        pool_pre_ping=True,
        # **必须**：交出事务控制权给下面的 begin 事件，否则发不出 BEGIN IMMEDIATE
        isolation_level=None,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, connection_record):
        _apply_sqlite_pragmas(dbapi_conn, connection_record)

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        """让每个事务都以 BEGIN IMMEDIATE 开始，立即取得写锁。

        取舍（如实记录）：这会让**只读事务也持写锁**，从而串行化全部数据库访问。
        换来的是：读事务升级为写时不会出现 SQLITE_BUSY 且 busy_timeout 失效的情况。
        本项目单机、低并发、事务都是毫秒级，串行化代价可接受（ADR-002 的裁决）。
        若将来读多写多，应改为"仅写路径显式 BEGIN IMMEDIATE"。
        """
        conn.exec_driver_sql("BEGIN IMMEDIATE")
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

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

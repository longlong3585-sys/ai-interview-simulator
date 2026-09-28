"""T-14 基线迁移：如实反映**当前实库**的结构。

内容来源：从 interview.db 的 sqlite_master 原样导出（见 docs/03-tasks.md 的勘察记录），
**不是**从 ORM 模型推断 —— 因为两者存在漂移：

    notifications.link_url   只存在于实库，ORM 不认识（死列）

基线必须反映"现实"，否则对新库与既有库会得到两套不同结构。
`link_url` 的移除留给 T-18 的独立迁移（SQLite ≥3.35 支持 DROP COLUMN）。

安全说明：
  - 全部语句为 CREATE ... IF NOT EXISTS，**没有任何 DROP / DELETE / UPDATE**
  - 对既有库，runner 会走 stamp 分支，**这些语句根本不会执行**
  - 对空库，才真正执行建表
"""

REVISION = "001"
DESCRIPTION = "baseline: users / interview_records / notifications（如实反映既有结构）"
DOWN_REVISION = None

UPGRADE_STATEMENTS = [
    # ---------- users ----------
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER NOT NULL,
        username VARCHAR,
        hashed_password VARCHAR,
        created_at DATETIME,
        role VARCHAR,
        email VARCHAR NOT NULL,
        is_active BOOLEAN,
        nickname VARCHAR,
        avatar VARCHAR,
        bio TEXT,
        gender VARCHAR,
        birthday DATE,
        PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_users_id ON users (id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",

    # ---------- interview_records ----------
    """
    CREATE TABLE IF NOT EXISTS interview_records (
        id INTEGER NOT NULL,
        user_id INTEGER,
        role VARCHAR,
        messages TEXT,
        report TEXT,
        created_at DATETIME,
        status VARCHAR,
        admin_comment TEXT,
        PRIMARY KEY (id),
        FOREIGN KEY(user_id) REFERENCES users (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_interview_records_id ON interview_records (id)",

    # ---------- notifications ----------
    # 注意：link_url 是历史遗留的死列（ORM 中不存在），基线如实保留，
    # 由 T-18 的迁移显式移除。
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER NOT NULL,
        user_id INTEGER,
        type VARCHAR,
        message TEXT,
        target_type VARCHAR,
        target_id INTEGER,
        is_read BOOLEAN,
        created_at DATETIME,
        link_url VARCHAR,
        PRIMARY KEY (id),
        FOREIGN KEY(user_id) REFERENCES users (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notifications_id ON notifications (id)",
]

# 本项目只前进不回退；如需回滚请使用 T-01 的备份恢复。
DOWNGRADE_STATEMENTS = []

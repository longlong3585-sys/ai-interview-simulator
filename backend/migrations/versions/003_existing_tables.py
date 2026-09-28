"""T-18 迁移 003：改动**既有表**结构。

三处变更（依据 docs/02-architecture.md §6.1 与 ADR-009/ADR-017/ADR-023）：

  1. `users` **加** `must_change_password BOOLEAN NOT NULL DEFAULT 0`（ADR-017）
  2. `interview_records` **加** `client_token`，并要求**唯一**（ADR-023 幂等键）
  3. `notifications` **删**死列 `link_url`（v1 遗留，ORM 里根本不存在）

为什么 1 不需要重建、2 和 3 需要
--------------------------------
本文件里的做法全部是**实测结论**（见 docs/18-manual-migration.md 的实测记录），
不是照本宣科：

| 变更 | 做法 | 实测依据 |
|---|---|---|
| `users` 加列 | **原生 `ADD COLUMN`** | 可行。`NOT NULL DEFAULT 0` 被允许，既有行自动填 0 而非 NULL；且在事务内、外键强制开启时都成立。**刻意不重建 `users`** —— 它是三张表的父表，重建会 DROP 父表，外键开启时直接 `FOREIGN KEY constraint failed`（已实测），外键关闭时虽能过但依赖隐式前提，没必要冒这个险 |
| `interview_records` 加唯一键 | **重建表** | `ALTER TABLE ... ADD COLUMN ... UNIQUE` 被 SQLite 明确拒绝（实测报 `Cannot add a UNIQUE column`）。ADR-009 要求走重建，故按重建做 |
| `notifications` 删列 | **重建表** | 按 ADR-009 的统一要求走重建，使"结构变更"只有一种形态、只有一处需要审计 |

重建采用的步骤（SQLite 官方 12 步法的可行子集）：

    CREATE TABLE x_new (...)          -- 目标结构
    INSERT INTO x_new (列...) SELECT 列... FROM x   -- **两侧都显式列名**
    DROP TABLE x                       -- 旧表
    ALTER TABLE x_new RENAME TO x      -- 改名
    CREATE INDEX ...                   -- 重建该表原有的索引

**刻意不做**的两步，附实测理由：

* **不切 `PRAGMA foreign_keys`。** 官方 12 步法的第 1/12 步是关掉再打开外键。
  实测两点：① 本项目的 runner 用裸 `sqlite3.connect()`，迁移期间外键本来就是
  关闭的；② `PRAGMA foreign_keys` 在事务内是**真正的 no-op**（实测：先设为 ON，
  再 `BEGIN`，再设 OFF，PRAGMA 仍报 1，且插入孤儿行依然被拒）——
  所以官方那两步在本框架的事务结构里**根本无法表达**。
  本迁移重建的两张表**没有任何子表引用它们**（实测确认），因此不需要关外键。
* **不用 `ALTER TABLE ... DROP COLUMN`。** SQLite 3.35.5 支持它且实测可用，
  但既然 ADR-009 要求统一走重建，就统一走重建。

安全设计（框架层闸门，见 migrations/runner.py）
---------------------------------------------
本迁移声明 `ALLOWS_TABLE_REBUILD = True`（脚本里会出现 `DROP TABLE`），
理由写在 `REBUILD_REASON` 里。相应地，框架会为本迁移额外执行三道闸，
任何一道不通过都会**整体回滚**，不会留下半成品：

  6. **行数不得减少** —— 重建最危险的失败模式是 `INSERT ... SELECT` 少拷了行，
     随后旧表被 DROP，数据永久丢失且迁移"成功"返回。
  7. **声明式自检**（下面的 `VERIFY_STATEMENTS`）—— 断言列数、关键列的存亡、
     唯一索引确实覆盖了 `client_token`、没有残留 `_new` 表。
  8. **`foreign_key_check` 必须为空** —— 补上"迁移期间外键强制关闭"这个缺口。

幂等性
------
`CREATE TABLE x_new` **刻意不加** `IF NOT EXISTS`：若 `_new` 已存在，说明状态
异常，应当**立刻报错**而不是继续。真正的幂等由 runner 的版本记录保证
（003 已记录则整段跳过）。索引重建用 `IF NOT EXISTS`（与 001 一致）。
"""

REVISION = "003"
DESCRIPTION = "既有表变更：users.must_change_password / interview_records.client_token(UNIQUE) / 去掉 notifications.link_url"
DOWN_REVISION = "002"

#: 允许脚本里出现 DROP TABLE —— 重建表的固有步骤，必须显式声明。
ALLOWS_TABLE_REBUILD = True

REBUILD_REASON = (
    "SQLite 不支持 ALTER TABLE ... ADD COLUMN ... UNIQUE（实测报 'Cannot add a "
    "UNIQUE column'），ADR-009 规定此类变更走重建；notifications 删列随同统一走"
    "重建。被重建的两张表均无子表引用，故无需切换 PRAGMA foreign_keys。"
)

UPGRADE_STATEMENTS = [
    # =====================================================================
    # 变更 1 —— users 加列（原生 ADD COLUMN，不重建）
    # =====================================================================
    "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0",

    # =====================================================================
    # 变更 2 —— interview_records 加 client_token 并要求唯一：重建
    # =====================================================================
    """
    CREATE TABLE interview_records_new (
        id            INTEGER NOT NULL,
        user_id       INTEGER,
        role          VARCHAR,
        messages      TEXT,
        report        TEXT,
        created_at    DATETIME,
        status        VARCHAR,
        admin_comment TEXT,
        client_token  VARCHAR,
        PRIMARY KEY (id),
        FOREIGN KEY(user_id) REFERENCES users (id),
        UNIQUE (client_token)
    )
    """,
    # 两侧都写全列名：不依赖列顺序，将来加列也不会错位
    "INSERT INTO interview_records_new "
    "(id, user_id, role, messages, report, created_at, status, admin_comment) "
    "SELECT id, user_id, role, messages, report, created_at, status, admin_comment "
    "FROM interview_records",
    "DROP TABLE interview_records",
    "ALTER TABLE interview_records_new RENAME TO interview_records",
    # 重建该表原有的索引（DROP TABLE 会一并删掉）
    "CREATE INDEX IF NOT EXISTS ix_interview_records_id ON interview_records (id)",

    # =====================================================================
    # 变更 3 —— notifications 删死列 link_url：重建
    # =====================================================================
    """
    CREATE TABLE notifications_new (
        id          INTEGER NOT NULL,
        user_id     INTEGER,
        type        VARCHAR,
        message     TEXT,
        target_type VARCHAR,
        target_id   INTEGER,
        is_read     BOOLEAN,
        created_at  DATETIME,
        PRIMARY KEY (id),
        FOREIGN KEY(user_id) REFERENCES users (id)
    )
    """,
    "INSERT INTO notifications_new "
    "(id, user_id, type, message, target_type, target_id, is_read, created_at) "
    "SELECT id, user_id, type, message, target_type, target_id, is_read, created_at "
    "FROM notifications",
    "DROP TABLE notifications",
    "ALTER TABLE notifications_new RENAME TO notifications",
    "CREATE INDEX IF NOT EXISTS ix_notifications_id ON notifications (id)",
]

#: 安全闸 7：在 COMMIT 之前逐条比对。任何一条不匹配 -> 整体回滚。
VERIFY_STATEMENTS = [
    # --- users：加了一列，且既有行被填成 0 而不是 NULL ---
    ("SELECT count(*) FROM pragma_table_info('users')", 13),
    ("SELECT count(*) FROM pragma_table_info('users') "
     "WHERE name='must_change_password'", 1),
    ("SELECT count(*) FROM users WHERE must_change_password IS NULL", 0),

    # --- interview_records：加了一列，且唯一约束**真的**覆盖了它 ---
    ("SELECT count(*) FROM pragma_table_info('interview_records')", 9),
    ("SELECT count(*) FROM pragma_table_info('interview_records') "
     "WHERE name='client_token'", 1),
    # 表级 UNIQUE 约束产生的是 sqlite_autoindex（sql 为 NULL），
    # 所以不能用 sqlite_master.sql LIKE 判断，必须查 pragma_index_* 
    ("SELECT count(*) FROM pragma_index_list('interview_records') il "
     "JOIN pragma_index_info(il.name) ii "
     "WHERE il.[unique]=1 AND ii.name='client_token'", 1),
    # 外键子句必须在重建中存活
    ("SELECT count(*) FROM pragma_foreign_key_list('interview_records')", 1),
    # 原有索引必须被重建回来
    ("SELECT count(*) FROM sqlite_master WHERE type='index' "
     "AND name='ix_interview_records_id'", 1),

    # --- notifications：死列已消失，外键与索引存活 ---
    ("SELECT count(*) FROM pragma_table_info('notifications')", 8),
    ("SELECT count(*) FROM pragma_table_info('notifications') "
     "WHERE name='link_url'", 0),
    ("SELECT count(*) FROM pragma_foreign_key_list('notifications')", 1),
    ("SELECT count(*) FROM sqlite_master WHERE type='index' "
     "AND name='ix_notifications_id'", 1),

    # --- 不得残留重建用的临时表 ---
    ("SELECT count(*) FROM sqlite_master WHERE type='table' "
     "AND name LIKE '%\\_new' ESCAPE '\\'", 0),

    # --- 002 建的四张表必须原样还在（本迁移不该碰它们）---
    ("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
     "('interview_sessions','captcha_store','auth_attempts','token_blacklist')", 4),
]

# 本项目只前进不回退；如需回滚请使用 T-01 的备份恢复。
DOWNGRADE_STATEMENTS = []

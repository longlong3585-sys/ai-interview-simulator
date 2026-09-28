"""T-17 迁移 002：会话 / 验证码 / 限流 / 令牌黑名单 四张新表。

依据：docs/02-architecture.md §6.2（DDL 已实测可执行）+ §4（`ended_reason` 列）。

安全说明
--------
- 全部语句为 `CREATE TABLE / INDEX IF NOT EXISTS`，**没有任何
  DROP / DELETE / UPDATE**（框架安全闸 4，由测试逐条断言）。
- 幂等：重复执行不会报错、不会改动既有数据。
- **不触碰任何既有表**：只新增对象，`users` / `interview_records` /
  `notifications` 的结构与数据一行不动。

关于 `interview_sessions.ended_reason`
------------------------------------
架构 §4 写的是 `ALTER TABLE interview_sessions ADD COLUMN ended_reason VARCHAR`，
那是**假设该表已由 v2.1 建好**。实际上本表在 T-17 才**首次创建**
（已核实真库只有 `users` / `interview_records` / `notifications` /
`schema_migrations`，不存在 `interview_sessions`），
因此把该列直接写进 `CREATE TABLE` —— **结果结构与 §4 完全一致**，
且省掉一次 ALTER。

> ⚠️ 已知的**唯一**风险场景（本项目中不存在，记录备查）：若某个环境
> 曾经手工建过 v2.1 版本的 `interview_sessions`（不含 `ended_reason`），
> `CREATE TABLE IF NOT EXISTS` 会跳过建表 → 该环境会缺这一列。
> 兜底手段有两条：① `tests/test_migration_002.py` 里有一条断言
> "建成的列集合必须恰好等于 `SessionSnapshot` 的字段集合"，
> 在这种环境下会立刻失败而不是拖到运行时；
> ② T-19 落地时若真有该环境，补一个 003 迁移做 ALTER 即可
> （SQLite 3.35 支持 `ADD COLUMN`）。

关于部分唯一索引
----------------
`UNIQUE(user_id) WHERE status='active'` 是把"同一用户重复开面试"从
**静默覆盖**（v1 用 `dict[user_id]` 的写法）改成**显式 409** 的关键（ADR-023）。
SQLite 的部分索引自 3.8.0 起支持，本机 3.35.5 已实测可用。
**任何非 active 状态都不占用该索引** —— 这是"用户永远能开新面试"的不变量
（ADR-022R），因此 `finished` / `abandoned` 行可以并存多条。
"""

REVISION = "002"
DESCRIPTION = "interview_sessions / captcha_store / auth_attempts / token_blacklist"
DOWN_REVISION = "001"

UPGRADE_STATEMENTS = [
    # =====================================================================
    # 面试会话（ADR-004 并发控制 + ADR-022 生命周期 + ADR-023 幂等）
    # =====================================================================
    """
    CREATE TABLE IF NOT EXISTS interview_sessions (
        session_id      VARCHAR PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role            VARCHAR NOT NULL,
        questions       TEXT    NOT NULL,
        question_status TEXT    NOT NULL,
        user_answers    TEXT    NOT NULL,
        current_index   INTEGER NOT NULL DEFAULT 0,
        last_seq        INTEGER NOT NULL DEFAULT 0,
        last_reply      TEXT,
        version         INTEGER NOT NULL DEFAULT 0,
        status          VARCHAR NOT NULL DEFAULT 'active',
        created_at      DATETIME NOT NULL,
        updated_at      DATETIME NOT NULL,
        expires_at      DATETIME NOT NULL,
        ended_reason    VARCHAR
    )
    """,
    # 按用户查"我的活跃会话"用（GET /api/interview/session）
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON interview_sessions(user_id)",
    # 部分唯一索引：同一用户**同时只能有一个 active 会话**；终态不占锁
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_active "
    "ON interview_sessions(user_id) WHERE status = 'active'",

    # =====================================================================
    # 验证码（300s TTL，一次性；ADR-014）
    # =====================================================================
    """
    CREATE TABLE IF NOT EXISTS captcha_store (
        captcha_id VARCHAR PRIMARY KEY,
        code       VARCHAR NOT NULL,
        expires_at DATETIME NOT NULL,
        used       BOOLEAN NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_captcha_expires ON captcha_store(expires_at)",

    # =====================================================================
    # 登录失败限流（滑动窗口 10min / 5 次；**只写失败**）
    # =====================================================================
    """
    CREATE TABLE IF NOT EXISTS auth_attempts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ip           VARCHAR NOT NULL,
        attempted_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_ip_time ON auth_attempts(ip, attempted_at)",

    # =====================================================================
    # 令牌黑名单（ADR-003 选 B 时启用）
    # TTL 必须 ≥ 8 小时（续期绝对上限），否则被吊销令牌会"复活"（ADR-016）
    # 本迁移只建表；协议与实现待 ADR-003-B 开工时补（见 docs/03-tasks.md T-16 注）
    # =====================================================================
    """
    CREATE TABLE IF NOT EXISTS token_blacklist (
        jti        VARCHAR PRIMARY KEY,
        expires_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blacklist_expires ON token_blacklist(expires_at)",
]

# 本项目只前进不回退；如需回滚请使用 T-01 的备份恢复。
DOWNGRADE_STATEMENTS = []

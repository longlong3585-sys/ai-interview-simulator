"""T-19 迁移 004：给 `interview_sessions` 补上 `report` 列。

## 为什么需要这次迁移（发现 ADR 与 DDL 互相矛盾）

`docs/02-architecture.md` 的 ADR-004「报告生成路径」明确写的是：

    generate_report 在事务外调 AI 评分
      → 短事务内 UPDATE interview_sessions
            SET report=:json, status='finished', version=version+1
          WHERE session_id=:sid AND version=:snapshot_version AND status='active'
      → rowcount==0 返回 409

而同文件 §6.2 给出的 `interview_sessions` 建表语句里**根本没有 `report` 列**
（v2 增量文档 §4 也只补了 `ended_reason`）。两处直接对不上。

T-16 的存储协议已经按 ADR-004 声明了
`SessionStore.finish(session_id, expected_version, report_json, ended_reason, now)`，
因此在实现 T-19 时这个矛盾无法再回避：报告**必须**有落点。

**取舍：以 ADR-004 为准，补列。** 理由：

1. ADR-004 是**行为契约**（"报告落库与置 finished 同事务"），§6.2 是**实现细节**；
   二者冲突时，契约优先。
2. 报告必须在 `generate_report` 与后续归档之间**服务端持久化**：
   ADR-024R ⑭ 要求 `generate_report` 不再接收前端传来的 `messages`、
   改以服务端会话为准 —— 若报告不落在会话上，那条改造就无处可依。
3. ADR-023 规定 `interview_records.client_token` 幂等键属于 `save_interview`
   （归档那一步），因此**不能**靠"在 finish 时就插记录"来绕过（那会改变
   归档语义并与既有 `/api/save_interview` 流程打架）。
4. 成本极低：`ALTER TABLE ... ADD COLUMN` 是**纯加列**，SQLite 原生支持、
   无需重建表、不搬运任何数据 —— 风险远低于 T-18 的重建表。

## 安全性

- 单一语句：`ALTER TABLE interview_sessions ADD COLUMN report TEXT`
- 新列**可为 NULL、无默认值** → 既有行不受影响（当前该表为空，但即使非空也安全）
- 不涉及 DROP / DELETE / UPDATE，不重建表
- 框架闸门照常生效：行数不得减少、声明式自检、`foreign_key_check` 必须为空
"""

REVISION = "004"
DESCRIPTION = "interview_sessions 补 report 列（ADR-004 报告路径所需，§6.2 DDL 遗漏）"
DOWN_REVISION = "003"

UPGRADE_STATEMENTS = [
    "ALTER TABLE interview_sessions ADD COLUMN report TEXT",
]

VERIFY_STATEMENTS = [
    # 列加上了（16 = T-18 之后的 15 + report）
    ("SELECT count(*) FROM pragma_table_info('interview_sessions')", 16),
    ("SELECT count(*) FROM pragma_table_info('interview_sessions') "
     "WHERE name='report'", 1),
    # 该列可为 NULL：既有行（若有）不得被塞进默认值
    ("SELECT count(*) FROM pragma_table_info('interview_sessions') "
     "WHERE name='report' AND [notnull]=0", 1),
    # 部分唯一索引必须还在（ALTER 不该动索引）
    ("SELECT count(*) FROM sqlite_master WHERE type='index' "
     "AND name='idx_sessions_active'", 1),
    # 外键子句必须还在
    ("SELECT count(*) FROM pragma_foreign_key_list('interview_sessions')", 1),
    # 004 不该碰其它表
    ("SELECT count(*) FROM pragma_table_info('users')", 13),
    ("SELECT count(*) FROM pragma_table_info('interview_records')", 9),
    ("SELECT count(*) FROM pragma_table_info('notifications')", 8),
    # 不得残留临时表
    ("SELECT count(*) FROM sqlite_master WHERE type='table' "
     "AND name LIKE '%\\_new' ESCAPE '\\'", 0),
]

# 本项目只前进不回退；如需回滚请使用 T-01 的备份恢复。
DOWNGRADE_STATEMENTS = []

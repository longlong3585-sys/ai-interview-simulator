# T-17 迁移执行记录（取证材料）

> **任务**：迁移 002 —— 新增 `interview_sessions` / `captcha_store` / `auth_attempts` / `token_blacklist`
> **工具**：自建迁移框架（ADR-009R，非 Alembic —— PyPI 不可达，见 `docs/02-architecture-v2.md` §ADR-009R）
> **执行时间**：2026-09-29 03:41:20
> **执行人**：项目总控 Agent
> **结果**：✅ 成功。真库 `interview.db` 由 `001` 升级到 `001, 002`，既有数据零丢失。

本文件是**原始日志的逐字复制**（含中文原样），可直接作为交付凭证。

---

## 0. 环境与方法

| 项 | 值 |
|---|---|
| 目标库 | `backend/interview.db`（118784 字节 → 172032 字节） |
| 迁移前版本 | `001`（managed） |
| 迁移前 journal_mode | `wal`（你验收 T-15 时启动过后端，已转换，属预期） |
| Python / SQLite | 3.8.10 / 3.35.5 |
| 执行命令 | `python scripts/migrate.py` |

**框架的五道安全闸**（T-14 建立，本次全部生效）：

1. 迁移前**强制备份**，备份失败即中止；
2. **只读预检**：结构不符合预期则中止，不猜测；
3. `BEGIN IMMEDIATE` **事务包裹**：单次迁移失败整体回滚，不留中间态；
4. 只执行迁移脚本里显式写出的语句，**框架自身绝不 DROP/DELETE**；
5. **幂等**：已记录的版本直接跳过。

---

## 1. 迁移前：只读状态检查

```
$ python scripts/migrate.py --status
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  state    : managed
  detail   : 已应用：001
  applied  : 001
  pending  : 002
```

---

## 2. 迁移前：dry-run（只报告，不改动任何数据）

```
$ python scripts/migrate.py --dry-run
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  状态      : managed（已应用：001）
  已应用    : 001
  待应用    : 002
  [dry-run] 未做任何修改
```

---

## 3. 副本库：首次迁移 + 再次迁移（幂等性证据）

先在**副本**上跑，确认无误后再动真库。

```
$ python scripts/migrate.py --db <副本> --skip-backup
  状态      : managed（已应用：001）
  已应用    : 001
  待应用    : 002
  应用 002 : interview_sessions / captcha_store / auth_attempts / token_blacklist
  完成，应用了 1 个迁移

$ python scripts/migrate.py --db <副本> --skip-backup     # 第二次
  状态      : managed（已应用：001, 002）
  已应用    : 001, 002
  待应用    : （无，已是最新）
  完成，应用了 0 个迁移

$ python scripts/migrate.py --db <副本> --status
  state    : managed
  detail   : 已应用：001, 002
  applied  : 001, 002
  pending  : (none)
```

**要点**：第二次运行应用了 **0** 个迁移 —— 幂等性成立。

---

## 4. 真库迁移（正式执行）⭐

```
$ python scripts/migrate.py
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  [备份] 迁移前备份 -> pre-migration_20260929_034120.db
  [备份] 完成，integrity_check=ok，行数={'interview_records': 5, 'notifications': 0, 'schema_migrations': 1, 'users': 3}
  状态      : managed（已应用：001）
  已应用    : 001
  待应用    : 002
  应用 002 : interview_sessions / captcha_store / auth_attempts / token_blacklist
  完成，应用了 1 个迁移
```

**退出码 0。** 备份文件：`backup/pre-migration_20260929_034120.db`
（118784 字节，`integrity_check=ok`，1 行 `schema_migrations` = 迁移前的状态）。

---

## 5. 迁移后核对（`scripts/verify_t17.py`，纯只读）

```
$ python scripts/verify_t17.py interview.db
库: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
模式: 只读

========================================================================
1. 迁移版本
========================================================================
  已应用: 001, 002
  [PASS] 002 已应用  -> revisions=['001', '002']

========================================================================
2. 四张新表与索引
========================================================================
  [PASS] 表 interview_sessions 存在
  [PASS] 表 captcha_store 存在
  [PASS] 表 auth_attempts 存在
  [PASS] 表 token_blacklist 存在
  [PASS] 表 interview_sessions 的列与规格一致  -> session_id, user_id, role, questions, question_status, user_answers, current_index, last_seq, last_reply, version, status, created_at, updated_at, expires_at, ended_reason
  [PASS] 表 captcha_store 的列与规格一致  -> captcha_id, code, expires_at, used
  [PASS] 表 auth_attempts 的列与规格一致  -> id, ip, attempted_at
  [PASS] 表 token_blacklist 的列与规格一致  -> jti, expires_at
  [PASS] 索引 idx_sessions_user 存在
  [PASS] 索引 idx_sessions_active 存在
  [PASS] 索引 idx_captcha_expires 存在
  [PASS] 索引 idx_attempts_ip_time 存在
  [PASS] 索引 idx_blacklist_expires 存在
  [PASS] idx_sessions_active 是 UNIQUE + 部分索引  -> CREATE UNIQUE INDEX idx_sessions_active ON interview_sessions(user_id) WHERE status = 'active'

========================================================================
3. 既有表未被 002 改动
========================================================================
  [PASS] users 未被加列（无 client_token）
  [PASS] users 未被加列（无 must_change_password）
  [PASS] notifications 仍保留 link_url（属 T-18 范围）
  [PASS] 四张新表迁移后为空  -> auth_attempts=0, captcha_store=0, interview_sessions=0, token_blacklist=0

========================================================================
4. 既有数据与完整性
========================================================================
  既有表行数: interview_records=5, notifications=0, users=3
  [PASS] integrity_check = ok  -> ok
  [PASS] foreign_key_check 无违规  -> violations=0

========================================================================
汇总
========================================================================
  共 21 项，通过 21 项，失败 0 项

  结论: 全部通过 [ALL PASS]
```

---

## 6. 迁移后：应用可正常启动并服务（真库，只读冒烟）

在后端指向**真库**的情况下启动，跑 13 项只读冒烟：

```
$ python -m uvicorn main:app --host 127.0.0.1 --port 8012
INFO:     Started server process [12532]
INFO:     Application startup complete.

$ python scripts/smoke_t15.py --base http://127.0.0.1:8012 --db interview.db
  [PASS] 接口 total_interviews == 库中 interview_records 行数  -> 接口=5 库=5
  [PASS] GET /api/history（管理员）-> 403 管理员不能进行面试  -> code=403
  共 13 项，通过 13 项，失败 0 项
```

后端日志检查（全文检索）：

| 检查 | 结果 |
|---|---|
| `database is locked` | **0 次** |
| `journal_mode 未能设为 WAL` WARNING | **0 次** |

---

## 7. 副本库：行为验证（25 项）

```
$ python scripts/verify_t17.py <副本> --behavioral
...
========================================================================
5. 行为验证（部分唯一索引 / 级联删除）
========================================================================
  [PASS] 同用户第二个 active 会话被拒  -> UNIQUE constraint failed: interview_sessions.user_id
  [PASS] 置 abandoned 后释放唯一锁，可立刻重开  -> active 行数=1
  [PASS] 多条终态会话可并存（不占锁）  -> active=1 terminal=3 total=4（期望 1/3/4）
  [PASS] 删除用户级联删除其会话  -> 剩余 0 条

  共 25 项，通过 25 项，失败 0 项
```

**安全护栏已验证**：对真库执行 `--behavioral` 被**拒绝**（退出码 1）：

```
$ python scripts/verify_t17.py interview.db --behavioral
拒绝执行：--behavioral 会写入并删除数据，不能在 interview.db 上运行。

请先做副本，例如：
  python -c "import sqlite3; s=sqlite3.connect('interview.db'); d=sqlite3.connect('copy.db'); s.backup(d); d.close(); s.close()"
然后：python scripts/verify_t17.py copy.db --behavioral
```

> 这道护栏是刻意加的：T-09 曾因用**真实账号**做破坏性验证而删掉了用户头像。
> 凡是会写数据的验收工具，都应该在真库上主动拒绝运行，而不是靠人记得。

---

## 8. 迁移后数据核对

| 项 | 迁移前 | 迁移后 | 判定 |
|---|---|---|---|
| `users` 行数 | 3 | **3** | 不变 ✅ |
| `interview_records` 行数 | 5 | **5** | 不变 ✅ |
| `notifications` 行数 | 0 | **0** | 不变 ✅ |
| 表数量 | 4 | **8**（+4 新表） | 只增不减 ✅ |
| `schema_migrations` | `001` | **`001, 002`** | 版本已记录 ✅ |
| `users` 列 | — | **无 `client_token`** | 未被 002 越界改动 ✅ |
| `notifications` 列 | 含 `link_url` | **仍含 `link_url`** | 留给 T-18 ✅ |
| `integrity_check` | ok | **ok** | ✅ |
| `foreign_key_check` | 无违规 | **无违规** | ✅ |
| 文件大小 | 118784 B | 172032 B | 只增（新增 4 张空表）✅ |

---

## 9. 回滚方法（若需要）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统'

# 1) 停掉后端（确认 8000 端口无监听者）
netstat -ano | Select-String ":8000\s+.*LISTENING"

# 2) 校验备份可用
.\backend\venv\Scripts\python.exe backend\scripts\verify_backup.py --db backup\pre-migration_20260929_034120.db
#    -> VERIFY_OK

# 3) 用备份覆盖（先自己留一份当前的）
Copy-Item backend\interview.db backend\interview.db.before-rollback
Copy-Item backup\pre-migration_20260929_034120.db backend\interview.db -Force

# 4) 确认回到 001
.\backend\venv\Scripts\python.exe backend\scripts\migrate.py --status   # applied: 001
```

备份已核验：`VERIFY_OK backup\pre-migration_20260929_034120.db`，
`tables = interview_records, notifications, schema_migrations, users`，
`row_counts = {"interview_records": 5, "notifications": 0, "schema_migrations": 1, "users": 3}`，
`integrity = ok`。

---

## 10. 本次执行中发现并修复的 3 个附带问题

不是 T-17 的主体工作，但都是"只有真正跑一次才会暴露"的问题，一并记录。

### 10.1 迁移日志乱码 —— `scripts/migrate.py`

**现象**：`--dry-run` 的中文日志在**被管道/文件捕获**时全部乱码。

**根因**：Python 在输出被重定向时按 `locale.getpreferredencoding()`（本机 cp936/GBK）
编码，**无视控制台的 `chcp 65001`**。

**为什么必须修**：迁移日志是本项目的取证材料（谁在何时把库升到了哪一版），
乱码等于取证失效。

**修法**：在 CLI 入口把 stdout/stderr 固定为 UTF-8（`_force_utf8_output()`）。

### 10.2 `journal_mode.py` 报告与实际不符 —— 附属文件统计时序错误

**现象**：命令报告 `-wal: （不存在）`，但命令跑完 `-wal` 就出现了 —— 自相矛盾。

**根因**：`status()` 在**打开连接之前**统计 `-wal`/`-shm` 大小，
而"打开连接"这个动作本身就会创建它们。

**修法**：把统计挪到连接**关闭之后**；并补上 `-wal` 为 0 字节的说明文案
（0 字节 = 没有未 checkpoint 的内容，**不是**故障）。

### 10.3 备份产物不是单文件 —— `scripts/make_backup.py`（影响最大）

**现象**：本次迁移的备份在 `backup/` 里留下了
`pre-migration_20260929_034120.db-shm`（32768 B）与 `.db-wal`（0 B）。

**根因**：`src.backup(dst)` 会把源库**文件头（含 WAL 标志）**一并复制，
于是备份库也是 WAL 模式；紧接着 `inspect_db()` 以 `mode=ro` 打开它时，
SQLite 会（重新）创建 `-wal`/`-shm`，而只读连接无权删除。

**数据风险**：**无**。实测把 `.db` 单独复制到干净目录后
`integrity_check=ok`、行数一致（因为 `-wal` 是 0 字节）。

**为什么仍然要修**：这是**恢复时的陷阱** —— 拿到备份的人会不确定
"到底要不要一起拷那两个文件"。备份是本项目最后一道防线
（T-01 / T-09 的事故都是靠它挽回的），不能留这种含糊。

**修法**：`backup_database()` 里显式 `PRAGMA journal_mode=DELETE`
（该语句会顺带完成 checkpoint，把 WAL 内容并回主文件），
并兜底清理残留附属文件；返回值不等于 `delete` 时**直接报错**而不是静默放过。

**回归测试**：`tests/test_backup_tools.py` 新增 5 项
（源库确实是 WAL / 备份无附属文件 / 备份不是 WAL 模式 /
`inspect_db` 之后仍无附属文件 / 单独复制后数据完整）。

**存量清理**：对已产生的那份备份做了同样的归化，并用项目自带的
`verify_backup.py` 复核通过（`VERIFY_OK`）。现在 `backup/` 里每个备份都是单文件。

---

## 11. 复现命令清单

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 状态 / 预演 / 执行
.\venv\Scripts\python.exe scripts\migrate.py --status
.\venv\Scripts\python.exe scripts\migrate.py --dry-run
.\venv\Scripts\python.exe scripts\migrate.py

# 迁移后核对（只读，可安全用于真库）
.\venv\Scripts\python.exe scripts\verify_t17.py interview.db

# 行为验证（仅副本；对真库会被拒绝）
.\venv\Scripts\python.exe scripts\verify_t17.py <副本.db> --behavioral

# 测试
.\venv\Scripts\python.exe -m unittest tests.test_migrations tests.test_migration_002 -v
.\venv\Scripts\python.exe -m unittest discover -s tests -t .

# 破坏性验证（对迁移脚本注入 11 个缺陷）
.\venv\Scripts\python.exe scripts\probes\probe_t17_migration.py
```

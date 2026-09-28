# T-18 真实数据库人工执行与回滚指令

> **任务**：迁移 003 —— 改动**既有表**结构（加 `client_token` / 加 `must_change_password` / 删死列 `link_url`）
> **状态**：代码与测试已完成（commit 见 `docs/03-tasks.md`）；**真库尚未执行**，当前仍为 `001, 002`，`003` 处于 pending。
> **本文件用途**：由你亲手按下这次结构变更的按钮，并逐项校验。
>
> 本文档里的**每一条命令、每一段预期输出，我都已在真库副本上实跑过**（§6 的输入就是真实输出）。

---

## 0. 先看这个：变更全貌与安全性论证

### 0.1 三处变更

| # | 表 | 操作 | 做法 | 为什么 |
|---|---|---|---|---|
| 1 | `users` | **加列** `must_change_password BOOLEAN NOT NULL DEFAULT 0` | 原生 `ALTER TABLE ADD COLUMN` | 实测可行：`NOT NULL DEFAULT` 被允许，既有行自动填 `0` 而非 `NULL`。**刻意不重建 `users`** —— 它是三张表的父表，重建时 `DROP TABLE` 会因外键约束失败（已实测） |
| 2 | `interview_records` | **加列** `client_token VARCHAR` + **唯一约束** | **重建表** | SQLite 明确拒绝 `ADD COLUMN ... UNIQUE`（实测报 `Cannot add a UNIQUE column`），ADR-009 规定此类变更走重建 |
| 3 | `notifications` | **删列** `link_url` | **重建表** | 死列（v1 遗留，ORM 里不存在）。按 ADR-009 要求与第 2 项统一走重建，使"结构变更"只有一种形态、只有一处需要审计 |

### 0.2 为什么这次执行是安全的（三条硬证据）

**证据一：`link_url` 在全项目中零引用。** 已 grep 全部业务代码
（`routers/`、`models/`、`utils/`、`auth.py`、`main.py`、`config.py`、`database.py`）
与前端 `src/`：**只有一句注释提到它，没有任何一行代码读写它**。
删掉一个没有任何代码引用的列，不可能影响运行。

**证据二：新加的两列目前没有任何代码使用。** `client_token` / `must_change_password`
是给 T-19 之后的会话持久化与首次改密流程用的。因此**单独执行 003 之后，
现有应用照常工作**，不需要同时上线新代码。

**证据三：两道框架闸门兜住"数据被弄坏"。** 重建表最危险的失败模式
（`INSERT ... SELECT` 少拷了行 / 列错位）在提交后是**静默**的。为此
`migrations/runner.py` 新增三道闸，全部在 `COMMIT` **之前**执行，
任何一条不通过即**整体回滚**：

| 闸 | 内容 | 拦住什么 |
|---|---|---|
| 6 | **行数不得减少**（逐表比对前后） | 重建少拷了行 |
| 7 | **声明式自检 14 项**（列数/关键列存亡/唯一索引/索引补回/无残留 `_new`） | 结构没达到预期 |
| 8 | **`foreign_key_check` 必须为空** | 写入外键违约数据（迁移期间外键强制是关闭的，必须靠它补上） |

### 0.3 关于"表被重建"的心理准备

`interview_records` 与 `notifications` 会被 `DROP TABLE` 后以新结构重建，
数据通过 `INSERT INTO ... SELECT` 搬运。**这个过程在同一个事务里**，
失败即回滚；且上面三道闸会在提交前复核。你不需要为此额外做什么，
但下面的校验步骤会**逐行**比对新旧数据（不只比行数）。

---

## 1. 执行前检查（4 项，约 1 分钟）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
```

### 1.1 确认后端已停止（避免连接占用导致备份/迁移异常）

```powershell
netstat -ano | Select-String ":8000\s+.*LISTENING"
```

**期望**：无输出。若有输出，记下 PID 并 `Stop-Process -Id <PID> -Force`，然后复查端口已空。

### 1.2 记下当前状态

```powershell
.\venv\Scripts\python.exe scripts\migrate.py --status
```

**期望输出**

```
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  state    : managed
  detail   : 已应用：001, 002
  applied  : 001, 002
  pending  : 003
```

> ⚠️ 若 `applied` 不是 `001, 002`，**先停下来告诉我**。

### 1.3 记下当前数据（回滚时的比对基准）

```powershell
.\venv\Scripts\python.exe scripts\verify_t17.py interview.db
```

**期望**：`共 21 项，通过 21 项`。请把其中这一行抄下来备用：

```
既有表行数: interview_records=5, notifications=0, users=3
```

### 1.4 预先确认验收工具会"正确地判不通过"

```powershell
.\venv\Scripts\python.exe scripts\verify_t18.py interview.db
```

**期望**：**失败**（退出码 1），11 项 FAIL，其中包括：
`[FAIL] 003 已应用  -> revisions=['001', '002']`。

> 这一步是**故意的**：如果它在迁移前就报"全部通过"，说明这个校验工具没有判别力，
> 那么迁移后它的"通过"也就不足为信。

---

## 2. 手动备份（双保险，约 30 秒）

迁移框架会**自动**强制备份，但既然是你亲手操作，再单独做一份：

```powershell
cd 'D:\AI 驱动的智能面试准备与模拟系统'
.\backend\venv\Scripts\python.exe backend\scripts\make_backup.py --label before-t18
```

**期望输出（节选）**

```
[1/4] backing up database ...
      -> interview_20260929_XXXXXX_before-t18.db (168.0 KB)
      integrity_check = ok
      row_counts       = {"auth_attempts": 0, "captcha_store": 0, "interview_records": 5,
                          "interview_sessions": 0, "notifications": 0,
                          "schema_migrations": 2, "token_blacklist": 0, "users": 3}
...
BACKUP_OK dir=...\backup db=interview_..._before-t18.db
```

**请检查两件事**：

1. `integrity_check = ok`
2. `backup\` 目录里**只有** `.db` 与 `manifest_*.json`，**不应出现** `.db-wal` / `.db-shm`

```powershell
Get-ChildItem backup | Where-Object { $_.Name -like '*before-t18*' } | Select-Object Name, Length
```

> 第 2 条是 T-17 修复过的那个隐患（备份产物必须是单文件）；这里顺便复核它仍然有效。

---

## 3. 预演：看清将要做什么（不改任何数据）

```powershell
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe scripts\migrate.py --dry-run
```

**期望输出（逐字）**

```
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  状态      : managed（已应用：001, 002）
  已应用    : 001, 002
  待应用    : 003
  [dry-run] 未做任何修改
```

**请确认**：`待应用` 只有 `003` 一项；最后一行是 `未做任何修改`。

---

## 4. 正式执行（**你要按的按钮**）

```powershell
.\venv\Scripts\python.exe scripts\migrate.py
```

**期望输出（逐字）**

```
database: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
  [备份] 迁移前备份 -> pre-migration_20260929_XXXXXX.db
  [备份] 完成，integrity_check=ok，行数={'interview_records': 5, 'notifications': 0, 'schema_migrations': 2, 'users': 3}
  状态      : managed（已应用：001, 002）
  已应用    : 001, 002
  待应用    : 003
  应用 003 : 既有表变更：users.must_change_password / interview_records.client_token(UNIQUE) / 去掉 notifications.link_url
     自检: 声明式 14 项通过 | 行数无减少（8 张表）| foreign_key_check 无违规
  完成，应用了 1 个迁移
```

**三个关键确认点**

1. `[备份] 完成，integrity_check=ok` —— 迁移前备份成功（这是能回滚的前提）
2. `自检: 声明式 14 项通过 | 行数无减少（8 张表）| foreign_key_check 无违规` —— 三道闸全过
3. 最后一行 `完成，应用了 1 个迁移`，**退出码 0**

> 如果这一行没出现，或者进程非 0 退出 —— **不要继续**，直接跳到 §7。
> 框架会整体回滚，库里不会留半成品；把完整输出发我。

---

## 5. 校验（只读，约 1 分钟）

### 5.1 结构校验（26 项）

```powershell
.\venv\Scripts\python.exe scripts\verify_t18.py interview.db
echo $LASTEXITCODE
```

**期望**：`共 26 项，通过 26 项，失败 0 项` / `结论: 全部通过 [ALL PASS]` / 退出码 `0`

### 5.2 手工复核关键事实（不依赖判定的原始数字）

```powershell
.\venv\Scripts\python.exe scripts\verify_t18.py interview.db --facts
```

`--facts` 只把事实摊开、不做通过/失败判定，便于你自己对照。
**期望输出（逐字）**

```
库: D:\AI 驱动的智能面试准备与模拟系统\backend\interview.db
模式: 只读

========================================================================
1. 迁移版本
========================================================================
  已应用: 001, 002, 003

  原始事实
    revisions           : ['001', '002', '003']
    users 列数          : 13
    users 含新列        : True
    interview_records 列数 : 9
    interview_records 含 client_token : True
    notifications 列数  : 8
    notifications 含 link_url : False
    覆盖 client_token 的唯一索引数 : 1
    client_token 为 NULL 的行数    : 5
    must_change_password 为 NULL 的行数 : 0
    must_change_password 取值集合  : [0]
    行数                : interview_records=5, notifications=0, users=3
    integrity_check     : ok
    foreign_key_check   : 无违规
```

**请逐项对照这 6 个关键值**

| 项 | 期望 | 为什么关键 |
|---|---|---|
| `revisions` | `['001','002','003']` | 003 确实被记录 |
| `users 列数` | `13`（原 12 + 1） | 加列生效 |
| `interview_records 列数` | `9`（原 8 + 1） | 重建后列数正确 |
| `notifications 列数` | `8`（原 9 − 1） | 死列确实被删 |
| `覆盖 client_token 的唯一索引数` | `1` | **幂等键的整个意义所在** |
| `行数` | `users=3, interview_records=5, notifications=0` | **一行都没丢** |

> 迁移前这几个值分别是 `12 / 8 / 9 / 0`，且 `含 link_url: True`、`取值集合: None`
> —— 你可以在 §1.4 时先跑一次 `--facts` 存档，迁移后直接对 diff。
>
> 这一项**刻意做成脚本参数**而不是内联命令：本项目的日志里已多次记录
> PowerShell 会吃掉 `python -c "..."` 里的引号，把好好的校验变成语法错误。

### 5.3 确认迁移前备份可读（可选但推荐）

```powershell
cd 'D:\AI 驱动的智能面试准备与模拟系统'
.\backend\venv\Scripts\python.exe backend\scripts\verify_backup.py --db backup\pre-migration_20260929_XXXXXX.db
```

**期望**：`VERIFY_OK`，且 `row_counts` 与迁移前一致（`users=3, interview_records=5, notifications=0`）。

那份备份是你**回滚的唯一依靠**，值得先确认它可读 —— 别等到需要回滚时才发现它坏了。

---

## 6. 应用级验证（确认应用照常工作，只读）

```powershell
# 终端 1
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

```powershell
# 终端 2
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe scripts\smoke_t15.py --db interview.db
```

**期望**：`共 13 项，通过 13 项`，其中
`接口 total_interviews == 库中 interview_records 行数 -> 接口=5 库=5`。

这一项同时证明两件事：**应用没被结构变更弄坏**，且**重建后的表读写正常**
（跨层一致性核对要求接口返回的数字等于直接读库的数字）。

收尾：

```powershell
netstat -ano | Select-String ":8000\s+.*LISTENING"
Stop-Process -Id <PID> -Force
```

---

## 7. 回滚

### 7.1 什么时候需要回滚

| 现象 | 是否需要回滚 |
|---|---|
| `migrate.py` 报错退出（任一道闸不通过） | **不需要** —— 框架已整体回滚，库里仍是 `001, 002` |
| 迁移成功，但你可疑数据不对 | **需要**，用 §7.3 |
| 迁移成功且校验全绿 | 不需要 |

判断"库里到底在哪个版本"：

```powershell
.\venv\Scripts\python.exe scripts\migrate.py --status
```

### 7.2 回滚前必须知道的两件事

1. **本项目没有 downgrade 机制**（ADR-009R：只前进不回退），
   回滚 = **用迁移前备份恢复**。
2. **回滚会丢什么**：`003` 之后写入的 `client_token` 值会一并丢失。
   目前应用代码**还没有**写入它（T-19 才接入），所以现在回滚是**无损**的。
   一旦 T-19 上线并开始写 `client_token`，回滚就会丢掉幂等键 ——
   到那时回滚要更谨慎。

### 7.3 回滚步骤（逐条执行）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统'

# ① 确认后端已停止
netstat -ano | Select-String ":8000\s+.*LISTENING"      # 期望无输出

# ② 校验要用来回滚的那份备份是好的
.\backend\venv\Scripts\python.exe backend\scripts\verify_backup.py --db backup\pre-migration_20260929_XXXXXX.db
#    期望 VERIFY_OK。若不是，改用第 2 步做的 interview_..._before-t18.db

# ③ 把当前的（已迁移）库另存一份 —— 万一回滚后想再前进，不用重跑
Copy-Item backend\interview.db backend\interview.db.after-003

# ④ 用备份覆盖（覆盖前确认没有 -wal 未落盘内容）
Get-ChildItem backend\interview.db* | Select-Object Name, Length
#    若存在 interview.db-wal 且**非 0 字节**，说明有未落盘数据，先停掉一切连接再操作

Copy-Item backup\pre-migration_20260929_XXXXXX.db backend\interview.db -Force

# ⑤ 确认已回到 001, 002
.\backend\venv\Scripts\python.exe backend\scripts\migrate.py --status
#    期望 applied: 001, 002 / pending: 003

# ⑥ 确认数据与结构都回到迁移前
.\backend\venv\Scripts\python.exe backend\scripts\verify_t17.py backend\interview.db
#    期望 21/21 通过（此时 link_url 应重新出现、must_change_password 应消失）
```

> 注意第 ④ 步的文件名：`pre-migration_*.db` 是**迁移框架自动**做的备份
> （时间戳是执行 `migrate.py` 的那一刻）；`interview_*_before-t18.db` 是你在
> §2 手动做的那份。两份都可以用来回滚，优先用**验证过 VERIFY_OK** 的那份。

### 7.4 回滚后的收尾

```powershell
Remove-Item backend\interview.db.after-003      # 确认不再需要后再删
```

---

## 8. 如果出问题：失败长什么样，怎么把信息给我

框架的失败信息都写在 `MIGRATION_ABORTED` 之前，并且**库已整体回滚**。
三种典型信息：

| 失败信息关键字 | 含义 | 处置 |
|---|---|---|
| `行数减少` | 重建少拷了行 | 库已回滚。把输出发我 |
| `自检未通过：期望 X，实得 Y` | 结构没达到预期 | 库已回滚。把输出发我 |
| `外键违规` | 写入了违约数据 | 库已回滚。把输出发我 |
| `结构不完整，拒绝自动处理` | 库的表结构与预期不符（例如被手工改过） | **未做任何改动**。先告诉我你的库经历过什么 |

请把这三样发我：

1. **完整输出**（不要只贴一行）
2. `cd backend; git rev-parse HEAD` 的结果
3. `cd backend; .\venv\Scripts\python.exe scripts\migrate.py --status` 的结果

---

## 9. 附录：本次的技术实测记录（为什么不按官方 12 步法照抄）

T-18 是第一次重建表，动手前做了 13 组实测（脚手架脚本未入库，结论如下）。
这些结论直接决定了迁移脚本的写法：

| 问题 | 实测结论 | 对实现的影响 |
|---|---|---|
| `ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0` 可行？ | **可行**，既有行填 `0` 而非 `NULL` | `users` **不重建** |
| `ALTER TABLE ... ADD COLUMN ... UNIQUE` 可行？ | **不可行**：`Cannot add a UNIQUE column` | `interview_records` 必须重建 |
| 「普通列 + 唯一索引」能否替代重建？ | **可以**（多个 NULL 允许、重复非 NULL 被拒） | 作为**备选方案**记录，本次按 ADR 要求走重建 |
| 原生 `DROP COLUMN link_url` 可行？ | **可行**（SQLite 3.35.5） | 同上，本次仍走重建以求形态统一 |
| 事务内、外键开启时重建 `interview_records` 可行？ | **可行**：数据保住、外键子句存活、其它表 SQL 未被改写 | 无需切外键 |
| 重建有子表指向的 `users` 会怎样？ | **失败**：`FOREIGN KEY constraint failed`（`DROP` 父表触发隐式删除） | 进一步确认 `users` 不能重建 |
| 事务内 `PRAGMA foreign_keys=OFF` 有效吗？ | **无效**（真 no-op）：PRAGMA 报告值不变，孤儿行仍被拒 | 官方 12 步法的第 1/12 步在本框架下**无法表达**，故不采用 |
| 迁移期间外键强制开着吗？ | **关着**（runner 用裸 `sqlite3.connect()`，默认 0） | 因此**必须**加闸门 8（`foreign_key_check`） |
| `foreign_key_check` 在 `foreign_keys=OFF` 时有效吗？ | **有效**（与开关无关） | 闸门 8 可成立 |
| `pragma_table_info` / `pragma_index_list` 表值函数可用？ | **可用** | 声明式自检 14 项据此编写 |
| 重建后 `sqlite_master.sql` 文本会一致吗？ | **不一定**：`ADD COLUMN` 是**就地拼接**，拼接点取决于原排版 | 「两路径收敛」断言改为比较**结构事实**（列/索引/外键），不比文本 |

> **一处自我更正**：首轮实测我曾记录"事务内 `PRAGMA foreign_keys=OFF` 竟然生效"。
> 那是**测量错误** —— 我在一条本来就 `foreign_keys=0` 的连接上"设为 OFF"，
> 读到 0 毫无意义。第二轮把连接先设为 ON、再进事务、再设 OFF，才得到正确结论：
> **PRAGMA 报告值不变、强制仍然有效**，与官方文档一致。
> 记录在此，因为"看起来被验证过"的错误结论比没验证更危险。

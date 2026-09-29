# 归档：历史验收 / 探针脚本（**不影响生产**）

本目录下的脚本是 T-01 ~ T-51 各任务在**开发与验收阶段**使用的一次性工具。
它们的历史使命已经完成，保留在这里只为**回溯测试**与"当时到底验了什么"的可追溯性。

> **它们不参与生产运行。** 生产链路上的一切都不依赖本目录：
> systemd 单元、`migrations/runner.py`、`tests/` 里的任何用例都没有引用这里的文件
> （整理时逐个 grep 核对过）。

## 目录内容

| 路径 | 是什么 |
|---|---|
| `verify_tXX.py` | 早期的人工验收工具（独立复算，零风险；默认在真库**副本**上跑） |
| `verify_tXX_manual.py` | 后期的人工验收工具：一条命令、离线、无需手动复制 Token（含"判别力自检"） |
| `probe_t15r.py`、`smoke_t15.py` | T-15 engine 契约的探针与真机只读冒烟 |
| `probes/probe_tXX_*.py` | **破坏性探针**：故意给被测代码注入缺陷，确认护栏会红，再 `try/finally` 还原 |

`probes/` 里那些脚本的价值不在于"能跑通"，而在于**证明护栏有判别力** ——
它们一律先注入缺陷、确认失败、再还原并校验 sha256。这也是本项目的验收惯例。

## 用法（需要时）

归档只是换了位置，**仍然可以运行**。整理时给每个脚本加了一行字面量路径
（`_ARCHIVE_REPO` = 四层 `dirname(__file__)` = 仓库根），
所以无论在归档位置还是原来位置运行，根目录都算得对：

```powershell
cd backend
.\venv\Scripts\python.exe scripts\archive\verify_t51_manual.py        # 最新一条认证链验收
.\venv\Scripts\python.exe scripts\archive\verify_t50_manual.py
.\venv\Scripts\python.exe scripts\archive\probes\probe_t22_cleanup.py # 破坏性探针（会临时改文件）
```

整理后实测（两条路径都试过）：

| 脚本 | 从 `backend/` 运行 | 从仓库根运行 |
|---|---|---|
| `verify_t50_manual.py` | 退出码 0 | 退出码 0 |
| `verify_t51_manual.py` | 退出码 0 | 退出码 0 |
| `verify_t21.py`（早期脚本） | — | 退出码 0（19/19 通过） |

⚠️ `probes/` 下的脚本会**临时修改**被测文件（然后还原）。跑之前请确保工作区干净
（`git status` 无未提交改动），否则一旦中途失败，还原可能与你的改动混淆。

## 为什么这几个"verify"仍留在 `scripts/` 根目录

判据不是"名字里有没有 verify"，而是**"有没有被生产代码或测试引用/import"**。
归档的底线是"移走之后一切都还能跑"，所以被 import 的一律留在原地：

| 脚本 | 不能归档的原因 |
|---|---|
| `verify_backup.py` | `tests/test_backup_tools.py` **直接 import** 它的 `verify()` —— 移走 = 测试断链 |
| `make_backup.py` | `migrations/runner.py` 直接 import；迁移前自动备份靠它 |
| `cleanup.py` | systemd `cleanup.service` 的 `ExecStart` 指向它；`tests/test_cleanup.py` 也 import 它 |
| `migrate.py` | 迁移入口（`migrations/runner.py` 的 CLI） |
| `db_health.py` | 运维体检工具（`--save` / `--compare`），T-52 备份体系会用 |
| `journal_mode.py` | 运维工具：查看 / 切换 `journal_mode`（WAL ↔ DELETE），排障要用 |

## 历史文档

对应的验收文档在 `docs/archive/`（每份都记了当时的改动、验证方式、真实输出与已知边界）。
两个目录合起来构成完整的"当时怎么做的"证据链，核心结论已回写到
`docs/03-tasks.md` 的任务行里。

## 整理本身的记录

整理是**纯文件系统操作**（移动 + 每个归档脚本改一行路径），**没有改任何业务逻辑**。
整理后回归（实测）：后端 `Ran 597 tests OK`、前端 `117 pass / 0 fail`、`tsc -b` exit 0，
且 `verify_t50_manual.py` / `verify_t51_manual.py` 仍可直接调用（退出码 0）。

> 过程留档：本次整理本身踩了八次坑（定位插入点的方式换一种坏一种，
> 包括把文件**截断**却仍返回 0 的静默破坏）。最终用的是最笨也最稳的写法 ——
> **锚在要改的那一行字面量上，并断言"还原后与原文逐字节相同"**，不满足就不写盘。
> 教训记在 `docs/03-tasks.md` 的"发布前整理"一节。

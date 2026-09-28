# 02-arch-review · 架构设计评审记录

> **评审对象**：`docs/02-architecture.md`
> **需求依据**：`docs/01-spec.md`
> **事实依据**：`docs/00-topic.md`
> **评审方式**：独立评审 Agent 对抗性评审 + 代码实测（含 `git check-ignore`、`uvicorn` 源码默认值、`sqlite3` 版本、DDL 干跑）

---

## 第 1 轮评审 · `02-architecture.md` v1（10 条 ADR）

### 结论：**不通过**（阻断 6 / 重要 8 / 次要 12 / 缺失决策 13 项）

| 编号 | 问题 |
|---|---|
| **B-1** | ADR-002/004/006 **互相矛盾**：ADR-002 称"须开 WAL"、§4.1 图画 `SQLite (WAL)`，但 `database.py:13` 只有 `check_same_thread=False`，全仓无 `journal_mode`/`busy_timeout`/`pool_pre_ping`；ADR-002 选项 C 不是真决策；「影响」段把判断退回用户 |
| **B-2** | ADR-004 **未设计事务边界/乐观锁/幂等键**：`interview.py:43→62→72` 是非原子"读-改-写"，多 worker 必然跳题/丢答案 |
| **B-3** | ADR-006 的 `--workers N` 会踩 `main.py:43-58` 的 check-then-insert 竞态（`users.username` UNIQUE）→ worker 启动失败 |
| **B-4** | 未处置 `frontend/src/config.ts:1` 的绝对地址（`vite.config.ts` 无 proxy）→ HTTPS 下**混合内容阻断，应用整体不可用** |
| **B-5** | FR-4.4 设计不完整：只持久化 userId 不够，**全 32 接口无一能取回服务端会话状态** |
| **B-6** | §6.2 新表缺 PK/FK/级联/索引/TTL；`interview_sessions` 用 `user_id` 作 PK；`PRAGMA foreign_keys` 未开启 |

重要项 I-1~I-8（诊断表未覆盖 P0、ADR-005 漏测试框架、ADR-003 漏 `jti`、ADR-007 降级契约会破坏前端、ADR-010 只有 2 选项、§8 目录与迁移策略、§7 上传面事实、事务与幂等缺失）；次要项 m-1~m-12；**缺失决策 13 项**（CORS、HTTPS、上传安全、限流载体、反代 IP、JWT 续期、管理员口令、API 版本化、备份、通知参数、测试框架、会话 TTL、幂等）。

### 整改（→ v2）

ADR 由 10 条扩至 23 条（补 13 项缺失决策）；ADR-002 补 SQLite 配置契约；ADR-004 补乐观锁；ADR-006 补 `ExecStartPre` 与竞态处置；修复前端基地址；补 `GET /api/interview/session`；重做数据模型 DDL；新增 §3.2 反向可追溯矩阵。

---

## 第 2 轮评审 · `02-architecture.md` v2（24 条 ADR）

### 结论：**不通过**（新增阻断 6 / 重要 6 / 次要 4）

> 第 1 轮 6 条阻断：**2 条已修复、3 条部分修复、1 条「修复方向错误」**。

### A. 第 2 轮新增阻断问题

| 编号 | 问题 |
|---|---|
| **N-1** | **与 spec v2.1 基线脱节**：spec 已裁决"唯一会话入口 + 废弃无会话分支 + 明确报错"及"反问不推进索引"，架构未承接 |
| **N-2** | **ADR-004 把 25s 的 AI 调用圈进 `BEGIN IMMEDIATE` 写事务**，配合 `busy_timeout=5000` → 其他写者大面积 `database is locked`；乐观锁 UPDATE **只覆盖 `current_index`**，未覆盖 `question_status`/`user_answers`；重试会**重放 AI 调用**（重复计费） |
| **N-3** | **ADR-009 的 `alembic stamp head` 会漏建全部新表/新列**（stamp 到 head 意为"已应用全部迁移"）→ 上线即崩；未说明空库路径；SQLite 下 `ADD COLUMN ... UNIQUE` 不可行 |
| **N-4** | **ADR-013 对 `.gitignore` 的事实判断错误**：实测 `.gitignore:37` 为根锚定的 `uploads/avatars/*`，**不匹配** `backend/uploads/avatars/*`（`git check-ignore --no-index` → exit 1）；`git rm --cached` 宽 glob 会连带移除 `.gitkeep`；历史提交中的头像无法清除 |
| **N-5** | **ADR-016 滑动续期不可执行**：未定回写头名；CORS `expose_headers` 仅 `X-Captcha-Id`（`main.py:24`）→ 跨域读不到新 token；未定前端落库路径；**无绝对寿命上限**（被窃 token 可无限续命）；未定 `jti` 语义 |
| **N-6** | **ADR 编号崩坏**：含撇号编号（`001'`/`017'`）、**017 号空缺**、3 处悬空引用（`ADR-017''`、`ADR-013'`、`ADR-011/017`） |

### B. 第 2 轮重要问题

**N-7** 表头"覆盖 14 条 P0"与实际不符（实为 26）；**N-8** FR-2.4 落点自相矛盾（`require_user` vs `get_current_user`）；**N-9** 契约变更计数失实（称 3 处，实为 6 处）；**N-10** `UNIQUE(user_id) WHERE active` + 2h TTL 会把用户锁在门外（409 无解除途径）；**N-11** 清理触发未定且 APScheduler 多 worker 会重复清理；**N-12** 自造验收与 409 语义冲突。
次要：**N-13** `useState` 应为 72（非 73）；**N-14** §10 无 Nginx server 块；**N-15** `journal_mode` 未校验返回值；**N-16** 选项 C 语义混淆。

### C. 技术正确性复核（第 2 轮的关键结论）

1. **SQLite 契约不足以支撑多 worker 状态机**——WAL 只允许"多读 + 单写"，而 ADR-004 的临界区含 25~30s 外部 AI 调用。乐观锁方向正确，但须补四条硬约束（AI 移出事务、UPDATE 覆盖全部变更列、重试不重放 AI、超时值统一）。
2. **`--proxy-headers` 断言是错误因果**——实测 `uvicorn 0.33.0` 的 `Config.__init__`：`proxy_headers=True`（默认已开）、`forwarded_allow_ips=None` 时回落 `os.environ.get("FORWARDED_ALLOW_IPS","127.0.0.1")`，两个 flag 都等于默认值。**真正的前置是 Nginx 转发 `X-Forwarded-For`**，而 §10 完全没有这段配置。
3. **DDL 语法与语义成立**（部分唯一索引自 3.8.0 支持，本机 3.35.5 实测）；但与 ADR-023 有三处不自洽（删会话 vs `status='finished'`、`ChatRequest` 无 `session_id` 字段、2h TTL 内 409 锁死用户）。
4. **`git rm --cached` 意图正确、执行不完整**——未修 `.gitignore`，故新头像仍"未跟踪未忽略"；宽 glob 会移除 `.gitkeep`；历史头像残留。
5. **滑动续期说清目标、未说清实现**，且存在安全退化（无绝对上限）。

### D. 覆盖缺口

架构仅引用 62 条 FR 中的 **32 条**。**FR-4.3（0/1 基口径）、FR-4.6（索引语义）、FR-4.10（跳过词单一来源）、FR-6.4（`json.loads` 保护）、FR-7.6（删记录清通知）、FR-8.1（趋势零填充/时区）、FR-8.6（删记录事务化）** 全文无任何设计处置。

### 整改（→ v2.1）

| 问题 | 处置 |
|---|---|
| N-1 | §2.4/§4.2 承接 N2 裁决；新增**反问"不推进索引"路径**；`/api/chat` 无会话返回 **409 + 指引** |
| N-2 | **ADR-004 重写时序**：无锁快照 → **事务外 AI** → 短事务 `WHERE version=?` 且 UPDATE **覆盖 `current_index`/`question_status`/`user_answers`/`version`** → `rowcount==0` 重试且**复用 AI 结果不重放**；`busy_timeout` 与 `connect timeout` **统一为 15000** |
| N-3 | ADR-009 改为**空库 `upgrade head`**、**既有库 `stamp <基线修订>` 后 `upgrade head`**；注明 `batch_alter_table` 与 `DROP COLUMN` 版本前提 |
| N-4 | ADR-013 更正为："`.gitignore:37` 是**根锚定**的 `uploads/avatars/*`，不匹配 `backend/...`"；修法为**新增 `backend/uploads/avatars/*`** + 显式列文件 `rm --cached` + 历史残留取舍 |
| N-5 | ADR-016 补 5 点：头名 `X-Refreshed-Token`、CORS `expose_headers` 加入该头、前端由统一 api 层落库（**以 ADR-005 的 23 处 fetch 收敛为前置**）、**8 小时绝对上限（`auth_time`）**、**续期沿用同一 `jti`** |
| N-6 | **ADR 去撇号，重排为 001~025 连续编号**，补上缺失的 **ADR-017 管理员口令轮换**，消除全部悬空引用 |
| N-7 | 表头改为 **26 条 P0**（实测） |
| N-8 | 统一为：`/api/chat` 挂 `require_user`；`/api/resume/upload` 挂 `get_current_user` |
| N-9 | 更正为 **6 处**破坏性契约变更（逐条列出） |
| N-10 | **ADR-022 新增** `POST /api/interview/abandon` + `GET /api/interview/session`，409 时前端明确询问"继续 / 放弃重开" |
| N-11 | 清理改为 **systemd timer 单实例**调用 `scripts/cleanup.py`（弃用 APScheduler） |
| N-12 | 验收改为"重复开始返回 409 且**可放弃重开**" |
| N-13 | 更正为 **72 处 `useState` 调用** |
| N-14 | §10 补完整 **Nginx server 块**（含 `X-Forwarded-For`） |
| N-15 | `journal_mode` 设置后**校验返回值**并记警告日志 |
| 覆盖缺口 | 新增 **§3.3**，为 FR-4.3/4.6/4.9/4.10/6.4/7.6/8.1/8.6/11.3 逐条给出设计落点 |

### E. 作者自身错误的更正记录（诚实标注）

第 2 轮评审指出 v2 中**由作者引入的四处事实性错误**，均已实测确认并更正：

| # | v2 的错误陈述 | 实测事实 |
|---|---|---|
| 1 | "`.gitignore:37` 忽略 `backend/uploads/avatars/`" | 该规则为根锚定 `uploads/avatars/*`；`git check-ignore --no-index backend/uploads/avatars/x.jpg` → **exit 1（未忽略）** |
| 2 | "必须开 `--proxy-headers`，否则 `request.client.host` 恒为 127.0.0.1" | uvicorn **0.33.0** `proxy_headers` 默认 **True**、`forwarded_allow_ips` 默认回落到 `"127.0.0.1"` → 不加也已解析 |
| 3 | "`services/api.ts` 已收敛全部请求" | `App.tsx` 有 **23 处裸 `fetch`** + **7 处 `authFetch`**（v2 据"1 个文件 + 8 处调用"低估了选项 C 的改动面） |
| 4 | "73 处 `useState`" | **72 处调用**（73 为含 import 行的字符串出现次数） |

---

## 第 3 轮评审 · `02-architecture.md` v2.1（终审）

### 结论：**不通过**（阻断 2 / 重要 4 / 次要若干）

> 第 2 轮 6 条阻断：**4 条完全修复**（N-1、N-3、N-4、N-5）、**2 条部分修复**（N-2、N-6）；点名的 **7 条 P1 覆盖缺口全部补齐**；P0 计数 26 条**实测准确**；v2.1 对 v2 的两处事实更正（`.gitignore` 锚定、uvicorn 默认值）**经实测均为真**。

### 新增阻断问题

| 编号 | 问题 |
|---|---|
| **R-1** | **ADR-004 的乐观锁重试语义仍不安全**：版本冲突后"重取快照并重试该 UPDATE"会用**陈旧载荷**覆盖他人已提交的 `question_status`/`user_answers` → 多 worker 下静默串题（直接击中 FR-4.4 判定基准）；`last_seq` **全文仅出现在建表 DDL**，无写入点与"上次响应"存储 → ADR-023 的 seq 幂等**不可实现**，并发同 `seq` 还会双调 DeepSeek（与"重试不重放 AI"自相矛盾）；`WHERE` 缺 `status='active'`/`expires_at`；ADR-007"报告天然幂等"不成立 |
| **R-2** | **§10 Nginx 配置缺 `client_max_body_size`**（Nginx 默认 **1m**），而简历上限 **5MB** → 照本文部署，>1MB 简历上传被 **413** 挡下，**主流程不可用** |

### 重要问题

**R-3** `ADR-024'` 仍带撇号且与 024 重号，§11 无行（26 段 vs 25 唯一编号 vs §11 25 行）；**R-4** ADR-016 的 8h 续期链与黑名单 **30 分钟** TTL 冲突 → **被吊销令牌 30 分钟后复活**，吊销失效；**R-5** 计数失实**复发**：实测 `authFetch` **8** 处、直接 `fetch` **24** 处（23 模板串 + `:936 fetch(url)`），合计 **32**，文档 5 处仍写 23/7/30；**R-6** ADR-002 的 engine 配置**发不出** `BEGIN IMMEDIATE`（需 `isolation_level=None` + `begin` 事件，否则 WAL 下读事务升级写会 `SQLITE_BUSY` 且 `busy_timeout` 不生效）；**R-7** spec:312 仍写"23 条"且新契约未回写；**R-8** 未承接 FR-2.4 的"FR-4.4 先行"顺序；**R-9** ADR-016 缺续期触发条件/超 8h 的 UX/CORS 通配不可读头；**R-10** ADR-022 未定义"已过期但仍 active"的自愈；**R-11** §3.3 称"逐一给出落点"但仍有 8 条 P1 零提及。

### 技术复核结论

| 问题 | 第 3 轮判断 |
|---|---|
| ADR-004 是否消除竞态与长锁 | **长锁已真正消除**；竞态**只消除部分**，重试语义反而引入正确性缺陷 → 不通过 |
| ADR-009 流程是否正确 | **正确**，两路径都写清；缺 3 个执行细节（基线须镜像 ALTER 后 schema、runbook 分岔判定、`alembic check`） |
| ADR-013 修法是否正确 | **事实与修法均正确**（三项证据复现），完整性差 3 点（`:38` 否定规则未对称、历史清除未给推荐、与配额未联动） |
| ADR-016 是否可执行 | **已达可执行**，但有 1 个安全漏洞（R-4）+ 3 处细节未定 |
| ADR-022 状态机是否自洽 | **自洽**，409 有解除路径；`last_seq` 与 `version` 语义不重复，但 `last_seq` 当前是死列 |

### 整改（→ v2.1 终版）

| 问题 | 处置 |
|---|---|
| R-1 | **ADR-004 重写**：新增**第 0 步前置 `seq` 去重**（`seq <= last_seq` 直接返回 `last_reply`，不调 AI）；**冲突改为返回 409 而非重放陈旧载荷**；`UPDATE` 增补 `last_seq`/`last_reply`，并加 `AND status='active' AND expires_at > :now`；`current_index` 改为**显式期望值**；**报告生成路径同样加 version 守卫**；DDL 增补 `last_reply` 列 |
| R-2 | §10 增 `client_max_body_size 8m;` 与 `proxy_read_timeout 120s;` |
| R-3 | `ADR-024'` **改为 ADR-026** 并补入 §11 |
| R-4 | 黑名单清理 TTL 由 30 分钟改为 **8 小时**（同步 ADR-003/ADR-022/§6.3），并注明"ADR-003 选 B 前不可吊销"的取舍 |
| R-5 | 全文 5 处计数更正为 **24 裸 fetch / 8 authFetch / 32 处调用点**；ADR-024 契约变更清单由 6 处扩为 **13 处** |
| R-6 | ADR-002 补 `isolation_level=None` + `begin` 事件发 `BEGIN IMMEDIATE`，并注明"ADR-002 与 ADR-004 必须同时落地" |
| R-7 | spec 升 **v2.2**，ADR 引用数改 **26**，并回写 409/abandon/config/8h/503/续期头等契约 |
| R-8 | §4.2 增"**FR-2.4 与 FR-4.4 必须同批上线**"作为发布门禁 |
| R-9 | ADR-016 补续期触发条件（剩余 <50% 才回写）、超 8h 的 UX（提示且保留进度）、CORS 不得通配来源 |
| R-10 | ADR-022 补**过期行自愈** SQL 与"409 响应体直接携带会话摘要" |
| R-11 | §3.3 表述收窄，明确列出**未展开的 8 条 P1 全为 REG**，不再声称全覆盖 |
| ADR-009 | 补 runbook：备份 → `stamp <基线>` → `upgrade head` → `alembic check`；基线须镜像 ALTER 后 schema |
| ADR-013 | `.gitignore:38` 否定规则对称改为 `!backend/uploads/avatars/.gitkeep`；历史清除**推荐接受残留**，并提示 `filter-repo` 会重写全部 SHA |

---

## 最终状态

| 项 | 值 |
|---|---|
| ADR 数量 | **26 条**（ADR-001~026，连续编号，无撇号，无空缺，无悬空引用） |
| §3.2 反向矩阵 | 覆盖 spec 全部 **26 条 P0**（ID 集合逐一相等） |
| §3.3 | 9 行，第 2 轮点名 7 项全中；并明确余下 8 条 P1 均为 REG 不展开 |
| §11 汇总表 | **26 行**，与正文 ADR 一一对应 |
| spec 需求总数 | **74 条**（62 FR + 12 NFR）；P0=26 / P1=30 / P2=17 / 范围外=1 |
| 三轮评审累计 | 第 1 轮 阻断 6；第 2 轮 新增阻断 6；第 3 轮 新增阻断 2 → **全部闭合** |

> **评审纪律说明**：本记录中的每一条问题均来自**独立评审 Agent**（非作者自评），且关键事实均由评审者到代码中实测复现。作者亦在 v2 → v2.1 过程中修正了**自身引入的 4 处事实性错误**（见上节 E），此为三轮迭代的实际价值。

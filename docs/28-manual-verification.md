# T-28 人工验收：后端超时兜底（服务端自己掌握超时时刻）

> 对应任务：`docs/03-tasks.md` 的 **T-28**（FR-4.12 / Bug 3B）
> 交付物：`backend/tests/test_interview_timeout_guard.py`、`backend/scripts/probes/probe_t28_timeout.py`、`backend/scripts/verify_t28_manual.py`

## 1. 这次到底改了什么

要修的是 **Bug 3B**：FR-4.12 的"15 分钟归零后必须闭环"原先**只有一半**。

| # | FR-4.12 的要求 | 修复前 |
|---|---|---|
| ① | 前端立刻打断主流程、锁定 UI | 前端有（`setTimeLeft(15 * 60)` + `setIsFinished`），T-42/T-43 继续收口 |
| ② | **服务端必须同步兜底**（否则锁定可被手工请求绕过） | **完全没有** —— 超时后再发一次 `POST /api/chat`，服务端照样把这一轮记进会话 |
| ③ | 给用户**明确反馈**"因超时已自动结束" | 服务端没有任何"因超时结束"的事实可下发；会话仍是 `active` |
| ④ | 超时后**释放唯一锁**，允许立刻重开 | 会话仍 `active` → 唯一锁被占着，重开要等 **2 小时 TTL** |

第 ② 条是根因：**"15 分钟"只是一个前端变量**。手工请求、脚本刷接口、标签页被挂起再恢复，
都能让它形同虚设。

修法：把死线搬到服务端。

* **死线 = 会话行 `created_at`（服务端 `start_interview` 写入，客户端无法伪造）
  + `INTERVIEW_DURATION_SECONDS`**（`backend/config.py`，默认 `15 * 60`，
  可用环境变量覆盖 —— 验收脚本靠它把一刻钟压成几秒）。
* 判定放在**数据模型**层：`SessionSnapshot.interview_deadline()` / `is_timed_out()`
  （`services/stores/base.py`）。**存储层零改动、无需迁移。**
* **惰性兜底** `_enforce_interview_timeout()`（`routers/interview.py`）：
  到点且仍 `active` → `store.abandon(reason='timeout')` → 唯一锁随之释放。
  与 T-22 的定时清理、T-25 的过期自愈同一套术语与思路。

### ⚠️ 两个刻意的设计决定

1. **死线不复用 `expires_at`（2 小时 TTL）**。那是 ADR-022 的**锁卫生**上限，
   回答"这行数据还值不值得当成活跃会话"；业务死线回答"这场面试还允许答题吗"。
   把两者压成一个字段，必然二选一地制造事故：要么允许用户答 2 小时，
   要么一次刷新/断网就让 15 分钟的面试作废。
2. **兜底必须覆盖每一个会话入口**，而不是只拦 `/api/chat`：

   | 入口 | 为什么要拦 |
   |---|---|
   | `POST /api/chat` | 主现场（Bug 3B 的原始形态） |
   | `POST /api/skip_question` | 另一条写路径，漏了它照样能推进会话 |
   | `GET /api/interview/session` | 刷新页面是"意识到超时"的最早时机，顺手结算 |
   | `POST /api/interview/abandon` | 到点后才点"放弃"的，真实原因是**超时**，`ended_reason` 必须如实写 `timeout`（T-27 的评分口径依赖它） |
   | `POST /api/generate_report` | **必须在判定 `ended_reason` 之前**跑，否则一场超时的面试会被报告路径判成 `completed` 并把状态写成 `finished`（正是 T-27 要消除的误导） |
   | `POST /api/start_interview` | 插入前释放业务死线，兑现 ADR-022R"**不得把用户锁死在门外**" |

### 接口变化（都是附加，无破坏性）

| 位置 | 新增 | 用途 |
|---|---|---|
| `POST /api/chat` / `skip_question`（到点后） | **409** + `code="interview_timeout"`、`ended_reason="timeout"`、`actions`、`session` 摘要 | 与 `no_active_session` **明确区分**（一个是"时间到了"，一个是"你根本没在面试"），前端据此给出 FR-4.12 要求的明确反馈 |
| `GET /api/interview/session` | `last_ended`（`session_id`/`status`/`ended_reason`/`has_report`…） | 超时后 `session` 必然是 `null`；没有它前端只能显示"你没有任何面试"，用户会以为进度丢了 |
| `GET /api/interview/config` | `duration_seconds` | 前端不再自己硬编码 15 分钟，**服务端是唯一来源** |
| `GET /api/interview/session` 的 `session` | `duration_seconds` / `deadline_at` / `interview_remaining_seconds` | 前端倒计时照服务端死线走（原 `remaining_seconds` 是 2 小时 TTL，语义未变、保留） |

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t28_manual.py
```

脚本自己做完所有事，**不需要你手工配合任何一步**：

1. 起一个**本机假 AI**（回环，永不联网），把收到的 prompt 原样记下来当证据；
2. 用 `OPENAI_BASE_URL` 指向它，另起一个 uvicorn（真实数据库），并把面试时长压成
   `--duration` 秒（默认 **20**）；
3. 自己往库里造一个**临时用户**并**自己签令牌**（`create_access_token`）——
   所以不用登录、不用从浏览器里复制 Token。

**关键点：C 段是"真的等时间走过去"** —— 不修改数据库、不伪造任何字段，
只 sleep 到 `created_at + duration + 1.5s`，再看服务端是否自己兜底。
这正是"服务端自己掌握超时时刻"最直接的证据。
（F2 段为了省掉第二次等待，才用 `age_session()` 改 `created_at` 造出同一个库状态，
脚本里已注明理由。）

### 它逐条证明什么

| 段 | 证明 | 期望 |
|---|---|---|
| A | `GET /api/interview/config` 下发的 `duration_seconds` | = 本次启动时设定的值（服务端是时长唯一来源）；会话带 `deadline_at` / `interview_remaining_seconds` |
| B | **不误伤**：没到点先答一题 | `/api/chat` **200**，库里 `status=active` |
| C | **等真实时间到点** | `/api/chat` → **409** `interview_timeout`；`detail` 是字符串（前端直接渲染）；库里 `status=abandoned` + `ended_reason=timeout`（**不是** `finished`） |
| C′ | 到点后再发一次 | 仍然 **409**（超时后不可能再答上题） |
| D | `GET /api/interview/session` | `session=null` 且 `last_ended.status=abandoned`、`ended_reason=timeout`（前端能说清"因超时已自动结束"） |
| E | **唯一锁已释放** | 超时后**立刻** `POST /api/start_interview` → **200**，且是一个**新** `session_id`（ADR-022R） |
| F | 超时也出报告（T-27 不回归） | `generate_report` → 200、`ended_reason=timeout`、状态**保持** `abandoned`、报告落库；计数由服务端裁决（假 AI 自报 7 / 99 被覆盖）；prompt 里写明"因超时自动结束"；**已答过题就不会被误贴「未及作答」** |
| F2 | **零作答**超时 | 同样被拦（409 `interview_timeout`）；报告 `answered_count=0`、`ended_reason=timeout`，且即使假 AI 故意返回一份"看起来全错"的报告，服务端也必须补上「**未及作答，无法评分**」 |
| G | 已出过报告再请求 | 409，且**不会再调一次 AI**（不重复计费、不覆盖用户看过的结论） |
| H | **客户端无法伪造** | 请求体里塞 `ended_reason=timeout` 被忽略（会话仍 `active`）；没到点出报告仍是 `completed` + `finished`（不误伤正常路径） |

结束后临时用户及其会话全部删除（脚本会再查一次库确认无残留）。

### 最近一次实跑结果

```
✅ T-28 人工验收通过：服务端自己掌握超时时刻 ——
   到点后 /api/chat 返回 409（interview_timeout）；
   会话状态 abandoned + ended_reason=timeout；
   唯一锁已释放（可立刻重开）；超时报告口径与 T-27 一致。
（临时用户与其 3 场会话已全部删除）
```

典型证据行（`--duration 8`）：

```
A  GET /api/interview/config -> HTTP 200  duration_seconds=8
B  服务端下发的死线: deadline_at=2026-09-29T17:10:51  剩余=7 秒；POST /api/chat -> 200
C  等待 8.3 秒（不修改数据库、不伪造任何字段）...
   POST /api/chat -> HTTP 409  code=interview_timeout  ended_reason=timeout
   库里 = status:abandoned ended_reason:timeout
D  GET /api/interview/session -> session=None
   last_ended={..., 'status': 'abandoned', 'ended_reason': 'timeout', 'has_report': False}
F2 details='未及作答，无法评分：本次面试因超时自动结束，在收到任何回答之前就已结束…'
E  POST /api/start_interview -> HTTP 200  new_session=8f28affd…
```

## 3. 判别力验证（证明测试真的抓得住）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\probes\probe_t28_timeout.py
```

11 个探针，逐个注入"服务端不再掌握超时时刻"的不同退化形态
（测试模块：`tests.test_interview_timeout_guard` + `tests.test_report_timeout`）：

| 探针 | 注入的缺陷 | 结果 |
|---|---|---|
| P1 | 兜底整体失效（超时后仍能答题 —— Bug 3B 原形） | CAUGHT（8 个用例红） |
| P2 | 结束原因写成 `manual`（超时说成主动放弃） | CAUGHT（6） |
| P3 | `/api/chat` 不再兜底（主流程仍可被绕过） | CAUGHT（3） |
| P4 | `/api/generate_report` 不再兜底（超时说成 `completed`） | CAUGHT（1） |
| P5 | `start_interview` 不再释放业务死线（用户被锁在门外） | CAUGHT（1） |
| P6 | 死线改用 `expires_at`（业务规则与 2 小时 TTL 混为一谈） | CAUGHT（9） |
| P7 | 超时响应丢掉 `interview_timeout` 标识（明确反馈没了） | CAUGHT（2） |
| P8 | `/api/interview/abandon` 不再先兜底 | CAUGHT（1） |
| P9 | `GET /api/interview/session` 不再自愈 | CAUGHT（1） |
| P10 | `/api/skip_question` 不再兜底（跳过也是写路径） | CAUGHT（1） |
| P11 | `/api/interview/config` 不再下发时长 | CAUGHT（1） |

探针运行前后对 `routers/interview.py` 与 `services/stores/base.py` 做 sha256 校验（一致），
`BASELINE` 与 `RESTORED` 两次复跑均为 `exit=0 OK`。

## 4. 已知边界（刻意如此，非遗漏）

* **前端锁定与文案仍属 T-42 / T-43**。本任务只保证服务端**有事实可依**
  （`code=interview_timeout`、`ended_reason=timeout`、`last_ended`）；
  "销毁输入区 / 遮罩 / Toast 文案"由前端做。
* **前端目前仍硬编码 15 分钟**：`duration_seconds` 已经下发，等 T-42 改用它。
  在此之前只要服务端时长没被环境变量改小，两边表现一致。
* **第二次请求返回的是 `no_active_session`**（不是 `interview_timeout`）：
  第一次请求已经把会话结算掉了，此时"没有进行中的会话"才是准确描述。
  前端在第一次 409（或刷新时的 `last_ended`）就能给出"因超时已自动结束"。
* **`INTERVIEW_DURATION_SECONDS` 只在进程启动时读一次**（`config.py` import 期）——
  验收脚本因此是自己起一个进程来压缩时长，而不是运行中改环境变量
  （这条教训来自 T-22 的 `--db` 失效事故）。
* **定时清理（T-22 `cleanup.py`）仍然只管 2 小时 TTL**，不处理 15 分钟业务死线：
  业务死线要求"用户一点下去就立刻被拦住"，那是惰性兜底的职责；
  定时器最小粒度是分钟级，等它兜底意味着超时后还能再答一小时。

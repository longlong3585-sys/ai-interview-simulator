# T-27 人工验收：超时报告评分口径（误导性 0 分）

> 对应任务：`docs/03-tasks.md` 的 **T-27**（FR-4.5 / FR-5.2 / ADR-007R）
> 交付物：`backend/tests/test_report_timeout.py`、`backend/scripts/probes/probe_t27_report.py`、`backend/scripts/verify_t27_manual.py`

## 1. 这次到底改了什么

要修的是 **FR-5.2「误导性 0 分」**：一个因为**超时**一道题都没答上的候选人，
拿到的报告却和"全部答错"一模一样 —— 全 0 分、没有任何说明。
用户看到的是"我很差"，事实是"面试根本没进行下去"。

旧提示词里那句 **"如果候选人对你提出的问题一个都没有给出有效回答…则所有分数均为 0"**
（`interview.py` 原第 205 行）是无条件生效的，正是这个 Bug 的源头。

按 ADR-007R 的裁决，拆成三件事：

| # | 要求 | 修复前 |
|---|---|---|
| 1 | **超时也要出报告** | 超时后会话已是 `abandoned`，`get_active()` 取不到 → `generate_report` **直接 409**，用户什么都拿不到 |
| 2 | **`ended_reason` 进报告**（`completed`/`timeout`/`manual`） | 报告里没有这个字段，前端无从区分"正常完成"与"超时结束" |
| 3 | **未及作答 ≠ 答不上** | `pending` 的题被当成无效回答，还参与了"全 0"判定 |

改动落点：

* **存储层（T-16 协议第 2 次修正，第 1 次见 T-19）**
  * `get_last_ended(user_id)`：取最近结束的一场。超时后 `get_active()` 永远返回 `None`，
    这就是"超时不出报告"在存储层的根因。
  * `attach_report(session_id, expected_version, report_json, now)`：**只写报告、不动状态**。
  * 于是 **`abandoned` + `report` 非空**成为一个合法组合 —— 报告与状态是**两个正交的事实**
    （`status` 说明怎么结束的，`report` 说明评分算过没有）。
    这样 ADR-022R（超时必须是 `abandoned`，不是 `finished`）与 ADR-007R（超时仍要出报告）
    才能**同时**成立。**不用 `finish()`** 就是为了不让报告路径污染状态语义。
* **路由器**：`_report_target()`（决定评哪一场 + 结束原因）、`_answer_counts()`、
  `_ended_reason_note()`、`_stamp_server_facts()`、`_persist_report()`，以及提示词重写。
* **`ended_reason` 与两个计数由服务端裁决**：模型输出会被覆盖，客户端自报一律无效。
  否则前端只要说一句"我超时了"，就能换到一份按宽松口径打的分数。

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t27_manual.py
```

脚本自己起一个**本机假 AI**（回环，永不联网）并把它记下的 prompt 留作证据，
再用 `OPENAI_BASE_URL` 指向它另起一个 uvicorn（真实数据库 + **临时用户**）。

**关键点：超时状态是用真实生产路径造出来的** ——
先把 `expires_at` 改到过去（造出"已过期但仍 active"的行，就是 TTL 到期后、
清理任务还没跑到时库里的真实样子），再跑 `scripts/cleanup.py`
（**就是 systemd timer 每 15 分钟执行的那个清理任务**）。

### 它逐条证明什么

| 段 | 证明 | 期望 |
|---|---|---|
| A | `POST /api/generate_report` 仍无 requestBody 且需鉴权 | T-26 不回归 |
| B | 正常答完 → `ended_reason = completed` | 库里 `status=finished` + 报告落库 |
| C | 答 1 / 跳 1 / 留 1 未答，然后超时 | `GET session -> null`，但 `generate_report` **仍然 200** 且 `ended_reason=timeout` |
| C' | 落库后的行 | `status=abandoned`（**不是 finished**）、`ended_reason=timeout`、`report` 非空 |
| D | 提示词证据 | 含 `ended_reason = timeout`、三题状态 `answered/skipped/pending`、"真正问过"的收窄口径；**未及作答那条规则**写有"不得计入扣分"、**主动跳过那条规则**写有"照常计分"；旧的无条件全 0 规则已删除 |
| E | 零作答超时（一道没答） | 假 AI **故意**返回一份"看起来全错、无任何说明"的报告 → 服务端必须补上「**未及作答，无法评分**」，且报告里 `answered_count=0 / total=3`（假 AI 编的是 7 / 99，被服务端覆盖），标注也**落库** |
| F | 全部**主动跳过**后超时 | **不得**出现「未及作答，无法评分」—— 那是候选人明确拒绝回答，不是没机会 |
| G | 已出过报告再请求 | 409，且**不会再调一次 AI**（不重复计费、不覆盖用户看过的结论） |
| H | 超时出报告后 | 能**立刻**开新一场面试（唯一锁已释放） |

结束后临时用户及其会话全部删除（脚本会再查一次库确认无残留）。

### 最近一次实跑结果

```
✅ T-27 人工验收通过：超时也能出报告且状态保持 abandoned、
   未及作答不计入扣分、零作答超时有明确标注、主动跳过不被误标
（临时用户与其 5 场会话已全部删除）
```

## 3. 判别力验证（证明测试真的抓得住）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\probes\probe_t27_report.py
```

10 个探针，逐个注入"未及作答被当成答不上"的不同退化形态：

| 探针 | 注入的缺陷 | 结果 |
|---|---|---|
| P1 | 超时后不再回退找会话 | CAUGHT |
| P2 | 写报告时顺手把状态改成 `finished`（ADR-022R 被违反） | CAUGHT |
| P3 | 去掉 `report IS NULL` 防覆盖守卫 | CAUGHT（见下方说明） |
| P4 | 去掉零作答超时的标注兜底（退回"只靠提示词"） | CAUGHT |
| P5 | 标注条件放宽成 `answered==0`（反向误导） | CAUGHT |
| P6 | `ended_reason` 不再由服务端裁决 | CAUGHT |
| P7 | 计数不再由服务端裁决 | CAUGHT |
| P8 | 提示词退回旧的无条件"全 0"规则 | CAUGHT |
| P9 | "未及作答"那条规则改成"一律按 0 分计入" | CAUGHT |
| P10 | "主动跳过"那条规则改成"不计入扣分" | CAUGHT |

探针运行前后对 `routers/interview.py` 与 `services/stores/sqlite_store.py` 做 sha256 校验（一致）。

> **两处"探针报错其实是探针自己的问题"值得记下来**：
> ① P3 首轮 **MISSED** —— 因为探针只跑了路由层测试模块，而"防覆盖守卫"的判别用例
> 在存储层测试里。**"漏网"的第一嫌疑应当永远是探针没选对目标**，不能当成"缺陷没被抓到"。
> ② P9/P10 首轮 **INVALID（针命中 0 次）** —— 缩进写成 4 个空格，实际是 2 个。
> 探针内置的"针必须唯一命中"校验把它们挡在了结论之外，没有污染 SUMMARY。

## 4. 已知边界（留给后续任务）

* **服务端的 15 分钟超时判定属 T-28**。本任务只保证"**会话一旦处于
  `abandoned(timeout)`，报告就是 `timeout` 口径**"。在 T-28 接线前，
  如果前端 15 分钟计时器先到点、而服务端会话仍是 `active`，
  那份报告仍会被标成 `completed`（服务端并不知道客户端已经开始计时）。
  这不是本任务可以单方面解决的 —— 需要服务端自己掌握超时时刻。
* **前端文案属 T-43**：本任务只保证响应里有 `ended_reason` 这个**机器可读**字段；
  "因超时自动结束，仅基于已答部分评分"这句界面文案由前端按 ADR-007R 渲染。
* **`manual` 口径可达**：手动放弃后若前端请求报告，会得到 `ended_reason=manual`
  且同样不惩罚未及作答的题；前端不会主动这么做（T-42/T-43）。
* **非超时但零作答**的情况（例如刚开场就点生成报告）**没有**服务端兜底标注 ——
  ADR-007R 的兜底只针对 `timeout`；这类情况由提示词约束（分数字段仍会给数值）。

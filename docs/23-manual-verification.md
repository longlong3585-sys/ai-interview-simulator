# T-23 本地人工验证指南

> **任务**：会话键统一（内存字典 → 持久化 `session_id`）+ 无会话返回 **409 + 指引**（Bug 1 收口）
> **前置**：真库已在 `001~004`（阶段 2 已完成），无需新的迁移。

---

## 0. 这次改了什么

| 修复前 | 现在 |
|---|---|
| `interview_sessions: Dict[int, Dict]` —— 进程内字典，以 `user_id` 为键 | 持久化 `SessionStore`，以 **UUID `session_id`** 为键；`user_id` 只用来找"该用户活跃的会话" |
| 同一用户再 `start_interview` → **静默覆盖**上一场 | `UNIQUE(user_id) WHERE status='active'` 拒绝 → **409 `active_session_exists`** |
| 多 worker 各持一份字典 → 刷新即丢进度 | 落库，跨 worker / 跨重启可见 |
| 无会话时 `/api/chat` **静默走通用 AI 对话**（登录用户可白嫖额度） | **409 `no_active_session` + 指引** |
| 无会话时 `/api/skip_question` 返回 **400** | **409 + 指引**（状态冲突不是参数错误） |
| `/api/chat` 无并发保护（多 worker 下串题） | 乐观锁 `version`；冲突 **409 + 最新进度** |
| 生成报告后 `pop(user_id)` 释放锁 | `finish()` 置 `status='finished'`（**ADR-004 报告路径**，同事务落库报告） |

**前端契约本次不变**：路由仍按"当前登录用户的活跃会话"定位，前端不需要传 `session_id`。
`start_interview` 的响应**新增** `session_id` 字段（向后兼容），供 T-24/T-25 使用。

---

## 1. 起服务

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

```powershell
# 另一个终端
cd 'D:\AI 驱动的智能面试准备与模拟系统\frontend'
npm run dev      # 打开 http://localhost:5173/
```

**取一个普通用户（非 admin）的令牌**（admin 会被 `require_user` 以 403 拒绝）：

```powershell
$body = '{"username":"123","password":"<你记得的密码>"}'
# 注意：/api/login 需要图形验证码，脚本无法识别 ——
# 最省事的办法是在浏览器里登录，然后从 DevTools → Application → localStorage
# 复制 token 出来，粘到下面的 $tok。
$tok = "<粘贴 token>"
$h = @{ Authorization = "Bearer $tok" }
```

---

## 2. 验「无会话 → 409 + 指引」

**先确保该用户没有活跃会话**（要么新用户，要么上一场已结束）：

```powershell
# 查一下（真库只读）
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:interview.db?mode=ro',uri=True);print(c.execute('select session_id,user_id,status,current_index from interview_sessions').fetchall());c.close()"
```

然后**不调 start_interview**，直接打 chat：

```powershell
try {
  Invoke-RestMethod -Uri http://127.0.0.1:8000/api/chat -Method Post `
    -Headers $h -ContentType 'application/json' -Body '{"message":"你好"}'
} catch {
  $r = $_.Exception.Response
  Write-Output "status = $($r.StatusCode.value__)   （期望 409）"
  $sr = New-Object System.IO.StreamReader($r.GetResponseStream())
  $sr.ReadToEnd()
}
```

**期望**：`409`，响应体形态如下（**`detail` 是字符串**，因为前端直接渲染它）：

```json
{
  "detail": "当前没有进行中的面试会话，请先开始面试",
  "code": "no_active_session",
  "hint": "请先调用 POST /api/start_interview 开始一场面试。...",
  "actions": ["start_interview"]
}
```

同样地，`POST /api/skip_question` 无会话时也应返回 `409 no_active_session`
（修复前是 `400`，且不带任何指引）。

**修复前**：`/api/chat` 会返回 `200` 并给出一个通用 AI 回复 —— 这就是"白嫖额度"的路径。

---

## 3. 验「同一用户第二场 → 409，且不覆盖第一场」

```powershell
# 1) 开始第一场
$r1 = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/start_interview -Method Post -Headers $h `
      -ContentType 'application/x-www-form-urlencoded' `
      -Body 'role=后端开发&questions_json=["Q1","Q2","Q3"]'
Write-Output "第一场 session_id = $($r1.session_id)"

# 2) 立刻再开一场 —— 期望 409
try {
  Invoke-RestMethod -Uri http://127.0.0.1:8000/api/start_interview -Method Post -Headers $h `
    -ContentType 'application/x-www-form-urlencoded' -Body 'role=前端开发&questions_json=["X"]'
} catch {
  $r = $_.Exception.Response
  Write-Output "第二次 start status = $($r.StatusCode.value__)   （期望 409）"
  (New-Object System.IO.StreamReader($r.GetResponseStream())).ReadToEnd()
}
```

**期望**：第二次 `409`，body 里 `"code":"active_session_exists"`。
**修复前**：第二次会 `200`，并且**第一场的进度被无声丢弃**。

---

## 4. 验「会话真的落库了」—— 你要的"在数据库里查看到新会话"

```powershell
.\venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:interview.db?mode=ro',uri=True);[print(r) for r in c.execute('select session_id,user_id,role,current_index,last_seq,version,status,ended_reason,expires_at from interview_sessions order by created_at desc limit 5')];c.close()"
```

**期望**：能看到第 3 步创建的会话，`status='active'`、`version=0`、`current_index=0`。

再打一轮对话，`current_index` 与 `version` 应当各 +1：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/chat -Method Post -Headers $h `
  -ContentType 'application/json' -Body '{"message":"我的回答"}' | ConvertTo-Json
```

```powershell
# 复查：current_index 应为 1，version 应为 1
.\venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:interview.db?mode=ro',uri=True);print(c.execute('select session_id,current_index,version,last_seq,status from interview_sessions order by created_at desc limit 1').fetchone());c.close()"
```

**这一条就是"多 worker / 重启不丢进度"的证据**：数据在库里，不在进程内存里。

### 4.1 顺手确认「重启不再丢会话」

1. 保持上面那条 `active` 会话；
2. `Ctrl+C` 停掉 uvicorn，重新起；
3. 用同一个 token 再打一次 `/api/chat`。

**期望**：仍然 `200`（拿到了磁盘上的会话），`current_index` 继续递增。
**修复前**：重启后内存字典清空 → 会掉进通用 AI 对话分支（返回一段无关的 AI 回复）。

---

## 5. 验「报告生成后释放锁」

```powershell
# 题目问完后调 generate_report（messages 传你实际的前端对话即可）
$rep = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/generate_report -Method Post -Headers $h `
  -ContentType 'application/json' -Body '{"messages":[{"role":"user","content":"我的回答"},{"role":"assistant","content":"下一个问题"}]}'
Write-Output "报告生成: overall=$($rep.overall_score)"
```

```powershell
# 会话应当变成 finished，并且 report 已落库
.\venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:interview.db?mode=ro',uri=True);print(c.execute('select session_id,status,ended_reason,length(report) from interview_sessions order by created_at desc limit 1').fetchone());c.close()"
```

**期望**：`status='finished'`、`ended_reason='completed'`、`length(report) > 0`。

然后**再开一场应当成功**（终态不占唯一锁）：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/start_interview -Method Post -Headers $h `
  -ContentType 'application/x-www-form-urlencoded' -Body 'role=后端开发&questions_json=["N1"]' | ConvertTo-Json
```

**修复前**：`pop(user_id)` 也能释放锁 —— 这一条是"改动没有把原有能力弄丢"的对照。

---

## 6. 自动化验收（零风险）

```powershell
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe -m unittest tests.test_chat_auth tests.test_dead_params -v
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**期望**：前者 16 项 OK（含"伪造 user_id 不推进他人会话"）；
全量 **438 项 OK**。

---

## 7. 已知边界（如实说明）

| 项 | 说明 |
|---|---|
| **前端尚未处理 409** | `/api/chat` 与 `/api/skip_question` 的前端处理器**没有检查 `res.ok`**。正常 UI 流程不会触发 409（界面只在有会话时发消息），但真触发时会出现**一个空气泡**。已在后端把 `hint` 做成可直接展示的字符串，前端加 3 行判断即可 —— 属 T-24/T-25 的前端配套 |
| `seq` 幂等未接线 | `ChatRequest` 目前没有 `seq` 字段，因此 `find_replay` 这条能力尚未启用（存储层与测试都已就绪）。客户端传 `seq` 是 T-26 前后的契约变更 |
| 客户端尚未持有 `session_id` | 路由仍按"用户当前活跃会话"定位，所以刷新页面后前端回不到同一场 —— 需要 T-24 的 `GET /api/interview/session` 与前端配套。**本次只是把会话搬到了库里**，Bug 2 的完整修复在 T-24/T-25 |
| 报告落库失败不报错 | `_finish_session` 失败只记 warning，仍把报告返回给用户（报告已生成，不能因落库失败让用户拿不到）。冲突时重试一次 |
| `interview_sessions` 只增不减 | 过期只置 `abandoned`（ADR-007R 要求超时后仍能出报告），清理见 T-22 |

---

## 8. 回滚

无数据库结构变更，回滚只需 `git revert <T-23 的 commit>`。
回滚后 `/api/chat` 会恢复"无会话即通用 AI 对话"的旧行为 —— 即 Bug 1 的白嫖路径会回来。

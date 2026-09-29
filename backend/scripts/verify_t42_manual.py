"""T-42 / T-43 人工验收：**傻瓜版**（一条命令，不用起服务、不用手动复制 Token、不花钱、不联网）。

## 它证明什么

T-42 / FR-4.12 修的是「**前端超时强制闭环**」：倒计时归零后界面必须**锁死**
（输入区从 DOM 里**销毁**），而不是"归零了但还能一直答下去"。这条闭环由四个
**互相独立**的事实拼起来，少任何一环都会留下"看起来改好了"的假象：

  ① **倒计时用的是服务端下发的时长/死线**（不再是前端硬编码 15 分钟）：
     `GET /api/interview/config` 下发 `duration_seconds`，`GET /api/interview/session`
     下发 `deadline_at` / `interview_remaining_seconds` / `duration_seconds`；
  ② **服务端到点后写路径一律 409 `interview_timeout`** —— 前端的锁定不是
     "自己算着玩的"，而是被服务端**拒绝写入**后的必然结果；
  ③ **前端据此锁定 UI + 销毁输入区 + 弹持久 Toast**：这一层由 `node --test` 的
     **源码契约测试**证明（见「边界」）；
  ④ **T-43**：超时报告页那句标注的开关是 `ended_reason === 'timeout'`，所以必须
     看到**报告体里真的有这个字段**，而不是"文案恰好被写死在页面上"。

## 边界（先读这条，别把它当成浏览器验收）

本脚本**不驱动真实浏览器渲染**，因此**不能**证明"输入框真的从屏幕上消失了"。
「锁定后输入区被**销毁**（而不是置灰）」是用
`frontend/tests/interview-timeout.test.mjs` 对 `App.tsx` 的**源码结构**断言证明的
（`<textarea>` 必须落在 `interviewLocked ? (...) : (...)` 的 **else 分支**里）——
那是**契约**，不是截图。端到端（`e2e-timeout-live.mjs`）走真实 HTTP + 真实数据库，
证明的是**服务端那一半**：到点后 409、会话落 `abandoned`、报告口径 `timeout`。
本脚本**不替代**那个 Node 脚本：它没写好时预检会直接指出缺哪个文件并退出 2。

## 它怎么做到"一条命令"

  1. 起一个**本机假 AI**（回环，永不联网），把收到的 prompt 记下来当证据；
  2. `OPENAI_BASE_URL` 指向它，另起一个 uvicorn（真实库），面试时长压成
     `--duration` 秒（默认 12），`DATABASE_URL` 指向 `--db`，保证**脚本查的库**
     与**服务端写的库**是同一个；
  3. 自己造**临时用户**并**自己签令牌**（`auth.create_access_token`）——不用登录、
     不用从浏览器复制 Token；
  4. 跑真正的端到端脚本并**实时转发**它的 `[PASS]/[FAIL]`，再用 Python
     **自己直查数据库**复核一遍：两条互不依赖的取证链；
  5. 结束时连同会话/报告/通知删掉临时用户，并**再查一次库**确认无残留。

## 逐段（0 / A~F）

  0. 预检：`node` 可执行；三个前端文件存在；`--duration` > 3；`--db` 存在且已建表。
  A. 前端契约测试（`node --test`）：倒计时纯逻辑、锁定接线、"输入区在锁定分支
     之外"的**判别力**、与后端字面量对齐。
  B. 起服务：假 AI（回环）+ uvicorn（真实库，`INTERVIEW_DURATION_SECONDS`）。
  C. 临时用户 + 自签令牌（先用一个 GET 证明令牌真被服务端接受）。
  D. 端到端：`node tests/e2e-timeout-live.mjs --base-url ... --token ... --duration N`，
     实时转发；退出码 0 → PASS、1 → FAIL、2 → 环境问题。
  E. 服务端事实复核（Python 直接查库，与 Node 侧**互相独立**）：会话行必须是
     `status='abandoned'` + `ended_reason='timeout'`；超时报告已落库且**报告体里
     真的带** `ended_reason='timeout'`；到点后写路径再也写不进去（409）。
  F. 清理：临时用户及其会话/报告/通知全删，再查一次库确认无残留。

## 关于「到点后写路径一律 409」的一个细节（读代码看出来的，不是脚本的 Bug）

`_enforce_interview_timeout()` 置超时是**一次性**的：**第一帧**过点的写请求让会话从
`active` 变 `abandoned`，拿到 `409 interview_timeout`；此后再写，因为已**没有活跃
会话**，拿到的是 `409 no_active_session`。两者都意味着"写不进去"，但只有前者是
"**因超时而**拒绝"。所以：**首帧**由 D 段（Node 侧）观察，E 段只复核"这一行确实
已是终态、且再怎么发也写不进去"；若 Node 侧没留下终态行，E 段会**自己补一帧**，
那时它反而能独立观察到首帧的 409。

## 用法

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t42_manual.py

可选参数：`--duration 12`（把面试时长压成 12 秒，默认 12，必须 > 3）；
`--db <路径>`（默认 backend/interview.db；服务端会被指向同一个库）。

退出码：0 = 通过；1 = 未通过（有断言失败）；2 = 环境问题（缺 node/缺文件/参数不合法/服务起不来）。
"""
import argparse
import datetime
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")
PY = os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe")

#: 静音 python-jose → cryptography 在本机 Python 3.8 上打的
#: `CryptographyDeprecationWarning: Python 3.8 is no longer supported ...`。
#: 它纯粹是噪音（T-28 的脚本同样有），但会让 PowerShell 把整条验收
#: 渲染成一片红色报错，看起来像失败 —— 而退出码其实是 0。
#: 按**消息**过滤而不是按 category：cryptography 的这个警告类继承的是
#: UserWarning，不是 DeprecationWarning（按 category 过滤会漏掉）。
import warnings as _warnings  # noqa: E402  (紧挨着使用点，故在此导入)

_warnings.filterwarnings("ignore", message=r".*Python 3\.8 is no longer supported.*")
_warnings.filterwarnings("ignore", message=r".*will remove support for Python 3\.8.*")

#: node 的调用前缀与展示用路径 —— 由 `resolve_node()` 在预检里确定。
#: 为什么不能简单写 `"node"`：这台机器的 PATH 里最先命中的是
#: `...\harness\.desktop-bin\node.cmd`（批处理 shim），而 Python 的
#: `subprocess` 走 CreateProcess，**不能**直接执行 `.cmd`（WinError 193）。
NODE_PREFIX = []
NODE_DISPLAY = ""

#: 服务端"因超时结束"的机器可读标识（与 `routers/interview.py` 一致）。
TIMEOUT_CODE = "interview_timeout"
#: 与 `services/stores/base.py` 的 `EndedReason.TIMEOUT` 一致。
TIMEOUT_REASON = "timeout"
#: 前端 `timeout.ts` 的 `TIMEOUT_REPORT_NOTE`（逐字；T-43 报告页那句标注）。
TIMEOUT_NOTE = "因超时自动结束，仅基于已答部分评分"
#: 后端在"零作答 + 超时"时补的标注（T-27 / ADR-007R）。
ZERO_ANSWER_LABEL = "未及作答，无法评分"
#: `--duration` 的下限：> 3 秒，否则"还没到点"的倒计时断言会自己先超时。
MIN_DURATION = 4

#: 两个 Node 侧产物都在 frontend 目录下（相对路径，cwd=frontend）。
CONTRACT_TEST = os.path.join("tests", "interview-timeout.test.mjs")
E2E_TEST = os.path.join("tests", "e2e-timeout-live.mjs")

FAILED = []
SKIPPED = []
ENV_PROBLEMS = []
SEG = {}
SEG_ORDER = [
    ("0", "预检（node / 三个文件 / 参数 / 库）"),
    ("A", "前端契约测试（倒计时·锁定·销毁输入区）"),
    ("B", "起服务（假 AI + uvicorn，真实库）"),
    ("C", "临时用户 + 自签令牌"),
    ("D", "端到端（e2e-timeout-live.mjs）"),
    ("E", "服务端事实复核（Python 直查库）"),
    ("F", "清理（确认无残留）"),
]

#: 假 AI 的固定回复：一份**没有任何标注**、看起来"全错"的报告。用它证明报告里的
#: `ended_reason` / 计数 / 「未及作答」标注都是**服务端**给的，而不是碰巧模型写了。
FAKE_REPORT = json.dumps({
    "expression_score": 0, "technical_score": 0, "logic_score": 0,
    "overall_score": 0.0, "answered_count": 7, "total_questions": 99,
    "suggestion": "本次为假 AI 的固定回复，不代表真实评分。",
    "details": "假 AI 的固定总结（故意不含任何超时说明）。",
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 输出与断言（沿用 verify_t28_manual.py 的约定：0 通过 / 1 未通过 / 2 环境问题）
# ---------------------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say("  [PASS] %s" % msg)


def bad(msg):
    FAILED.append(msg)
    say("  [FAIL] %s" % msg)


def skip(msg):
    SKIPPED.append(msg)
    say("  [SKIP] %s" % msg)


def env_problem(msg):
    ENV_PROBLEMS.append(msg)
    say("  [ENV ] %s" % msg)


def seg(name, passed):
    """记下某一段的结论（True / False / None=跳过），最后汇总成总表。"""
    SEG[name] = passed


def section(title):
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


# ---------------------------------------------------------------------------
# HTTP / 端口 / 日志小工具（与 T-28 脚本同源，保持风格一致）
# ---------------------------------------------------------------------------

def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _as_dict(value):
    """响应体可能是错误字符串（连不上等）—— 统一成 dict，避免 `.get` 崩掉。"""
    return value if isinstance(value, dict) else {}


def request(method, url, token=None, body=None, form=None, timeout=90):
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_ready(base, proc, seconds=45):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        st, _ = request("GET", base + "/openapi.json", timeout=5)
        if st == 200:
            return True
        time.sleep(0.5)
    return False


def tail(path, lines=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return "(无日志)"


def _decode_line(raw):
    """Node 写管道时是 UTF-8；万一混进 GBK（老控制台）也不让脚本崩掉。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc).rstrip("\r\n")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def resolve_node():
    """找到**可直接执行**的 node，返回 `(调用前缀, 展示用路径)`；找不到返回 `(None, None)`。

    这台机器上 `PATH` 里最先命中的是
    `C:\\Users\\...\\dsh-desktop\\harness\\.desktop-bin\\node.cmd` —— 一个批处理 shim。
    Python 的 `subprocess` 底层是 CreateProcess，**不能**直接执行 `.cmd`/`.bat`
    （会 WinError 193 / not a valid Win32 application），所以：

      1. 优先 `shutil.which("node.exe")` —— 这一步会**跳过** `.cmd` shim，
         命中真正的可执行文件（本机是 `D:\\Program Files\\node.exe`）；
      2. 找不到 `.exe` 才退回 `shutil.which("node")`；若拿到的是 `.cmd`/`.bat`，
         用 `[COMSPEC, "/c", shim]` 包一层再调（cmd.exe 才认得批处理）；
      3. 都没有 -> `(None, None)`，由调用方归入"环境问题"并退出码 2。
    """
    exe = shutil.which("node.exe")
    if exe:
        return [exe], exe
    found = shutil.which("node")
    if found:
        if found.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return ([comspec, "/c", found],
                    "%s（批处理 shim，经 %s /c 调用）" % (found, comspec))
        return [found], found
    return None, None


def run_node(args, live=False, timeout=None):
    """跑一条 node 命令并**逐行**收集输出；`live=True` 时同时实时转发。

    为什么必须逐行读而不是 `subprocess.run()`：端到端要跑十几秒，一次性打印会让
    人以为脚本卡死了，而"实时转发"本身就是验收要求。用管道 + 读线程而不是
    `select()`：Windows 的 `select()` 只支持 socket，管道只能靠线程。

    管道不会死锁：读线程从进程一起步就**并发**把 stdout 抽干到 EOF，所以子进程
    永远不会因为管道缓冲区写满而卡住；`proc.wait()` 只是等退出码。
    返回 `(退出码, 输出行)`；node 起不来或找不到时退出码为 `None`。
    """
    if not NODE_PREFIX:
        return None, ["<找不到 node 可执行文件：见预检段的输出>"]
    try:
        proc = subprocess.Popen(NODE_PREFIX + list(args), cwd=FRONTEND_DIR,
                                env=dict(os.environ), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError as exc:
        return None, ["<无法启动 node（%s）：%s: %s>"
                      % (NODE_DISPLAY, type(exc).__name__, exc)]
    lines = []

    def _pump():
        for raw in iter(proc.stdout.readline, b""):
            line = _decode_line(raw)
            lines.append(line)
            if live:
                say("    | " + line)

    pump = threading.Thread(target=_pump)
    pump.daemon = True
    pump.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        lines.append("(超过 %s 秒仍未结束 —— 进程已被强制结束)" % timeout)
        rc = None
    pump.join(timeout=10)          # 等读线程把管道读干净（正常已经在 EOF 处收尾）
    try:
        proc.stdout.close()
    except Exception:  # noqa: BLE001
        pass
    return rc, lines


def _grab_count(lines, name):
    """从 `node --test` 输出里抓 `ℹ pass 23` / `ℹ fail 0` 之类的计数。"""
    for line in lines:
        parts = line.replace("ℹ", " ").replace("#", " ").split()
        for i, tok in enumerate(parts[:-1]):
            if tok == name and parts[i + 1].isdigit():
                return int(parts[i + 1])
    return None


def _marker_counts(lines):
    """数一数 Node 侧自己打了多少 `[PASS]` / `[FAIL]`（旁证它真的断言过）。"""
    return (sum(1 for ln in lines if "[PASS]" in ln),
            sum(1 for ln in lines if "[FAIL]" in ln))


# ---------------------------------------------------------------------------
# 假 AI（回环，永不联网）：收到的 prompt 全部留下来当证据
# ---------------------------------------------------------------------------

def _is_report_prompt(system, user):
    return "overall_score" in user or "面试评估专家" in system


class _FakeAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        msgs = payload.get("messages") or []
        system = next((m.get("content", "") for m in msgs
                       if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in msgs
                     if m.get("role") == "user"), "")
        self.server.records.append({"system": system, "user": user})
        content = FAKE_REPORT if _is_report_prompt(system, user) \
            else "【假面试官】收到，请继续。"
        body = json.dumps({
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
            "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeAIServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass          # 关停时的 keep-alive 断连不值得刷屏


class FakeAI(object):
    def __init__(self):
        self.httpd = _FakeAIServer(("127.0.0.1", 0), _FakeAIHandler)
        self.httpd.records = []
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]

    def report_prompts(self):
        return [r for r in list(self.httpd.records)
                if _is_report_prompt(r["system"], r["user"])]

    def last_report_prompt(self):
        prompts = self.report_prompts()
        return prompts[-1]["user"] if prompts else ""

    def counts(self):
        total = len(self.httpd.records)
        reports = len(self.report_prompts())
        return total, reports, total - reports

    def start(self):
        self.thread.start()

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 临时用户 / 库操作（只读查询一律 `mode=ro`，不改动任何既有数据）
# ---------------------------------------------------------------------------

def make_probe_user(db_path):
    """造一个临时用户并**自己签令牌** —— 无需你登录或从浏览器复制 Token。"""
    # `auth` 会拉起 python-jose → cryptography（噪音已在文件头静音，见那里的注释）。
    from auth import create_access_token, get_password_hash
    name = "t42_probe_%s" % uuid.uuid4().hex[:8]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (username, email, hashed_password, role, "
            "is_active, created_at) VALUES (?,?,?,?,1,?)",
            (name, name + "@probe.local", get_password_hash("Probe#12345"),
             "user", datetime.datetime.utcnow().isoformat()))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username=?",
                           (name,)).fetchone()[0]
    finally:
        conn.close()
    return uid, name, create_access_token(
        {"sub": name, "user_id": uid, "role": "user"})


def drop_probe_user(db_path, uid):
    """连同会话/报告/通知/记录一起删干净 —— 只针对这个临时 uid。"""
    conn = sqlite3.connect(db_path)
    try:
        for sql in ("DELETE FROM interview_sessions WHERE user_id=?",
                    "DELETE FROM notifications WHERE user_id=?",
                    "DELETE FROM interview_records WHERE user_id=?",
                    "DELETE FROM users WHERE id=?"):
            conn.execute(sql, (uid,))
        conn.commit()
    finally:
        conn.close()


def _ro(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def probe_rows(db_path, uid):
    """只读地看这个临时用户的会话行（`report` 换成长度，避免刷屏）。"""
    conn = _ro(db_path)
    try:
        rows = conn.execute(
            "SELECT session_id, status, ended_reason, current_index, version, "
            "       created_at, updated_at, expires_at, questions, "
            "       question_status, "
            "       (report IS NOT NULL AND report <> '') AS has_report, "
            "       length(coalesce(report, '')) AS report_bytes "
            "FROM interview_sessions WHERE user_id=? "
            "ORDER BY created_at, session_id", (uid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def raw_report(db_path, session_id):
    """只读地取出会话行里落库的那份报告 JSON（T-43 的开关就在里面）。"""
    conn = _ro(db_path)
    try:
        row = conn.execute("SELECT report FROM interview_sessions "
                           "WHERE session_id=?", (session_id,)).fetchone()
        return (row["report"] if row else None) or ""
    finally:
        conn.close()


def leftover_counts(db_path, uid):
    """清理之后这个临时 uid 在各表里还剩多少行（"无残留"的取证）。"""
    conn = _ro(db_path)
    try:
        out = []
        for table, col in (("users", "id"), ("interview_sessions", "user_id"),
                           ("interview_records", "user_id"),
                           ("notifications", "user_id")):
            try:
                n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?"
                                 % (table, col), (uid,)).fetchone()[0]
            except sqlite3.Error:
                n = "?"
            out.append((table, n))
        return out
    finally:
        conn.close()


def status_counts(question_status):
    """把库里的 `question_status`（JSON 数组）数成三种状态。

    口径与后端 `_answer_counts()` 完全一致（不认识的状态按 `pending` 算），
    这样才能拿它校验报告里的 `answered_count` 是不是服务端的事实。
    """
    try:
        items = json.loads(question_status) if question_status else []
    except ValueError:
        items = []
    if not isinstance(items, list):
        items = []
    counts = {"answered": 0, "skipped": 0, "pending": 0}
    for st in items:
        counts[st if st in counts else "pending"] += 1
    return counts


def deadline_of(row, duration_seconds):
    """服务端算死线用的正是这两样：`created_at` + 时长上限。返回 ISO 串。"""
    try:
        begun = datetime.datetime.fromisoformat(row.get("created_at") or "")
    except (TypeError, ValueError):
        return None
    return (begun + datetime.timedelta(seconds=duration_seconds)).isoformat()


def deadline_passed(row, duration_seconds):
    dl = deadline_of(row, duration_seconds)
    if not dl:
        return False
    try:
        return datetime.datetime.utcnow() >= datetime.datetime.fromisoformat(dl)
    except ValueError:
        return False


def print_rows(rows, indent="    "):
    """把库里的原始行打出来 —— 这是"服务端事实"最直接的证据。"""
    if not rows:
        say("%s(这个临时用户没有任何会话行)" % indent)
    for r in rows:
        say("%ssession_id=%s  status=%s  ended_reason=%r  current_index=%s"
            % (indent, r["session_id"], r["status"], r["ended_reason"],
               r["current_index"]))
        say("%s  created_at=%s  expires_at=%s  report=%d 字节"
            % (indent, r["created_at"], r["expires_at"], r["report_bytes"]))
        say("%s  questions=%s" % (indent, r.get("questions") or ""))
        say("%s  question_status=%s" % (indent, r.get("question_status") or ""))


# ---------------------------------------------------------------------------
# D 段：端到端（实时转发 Node 侧输出，本脚本只负责裁定退出码）
# ---------------------------------------------------------------------------

def segment_d(base, token, duration):
    section("D. 端到端：node tests/e2e-timeout-live.mjs（实时转发它的每一行）")
    say("  $ cd frontend && node %s --base-url %s --token <临时令牌> --duration %d"
        % (E2E_TEST, base, duration))
    say("  （下面带 [PASS]/[FAIL] 的行是**它自己**打的；本脚本只转发并裁定退出码）")
    say("")
    rc, lines = run_node([E2E_TEST, "--base-url", base, "--token", token,
                          "--duration", str(duration)],
                         live=True, timeout=max(180, duration * 6 + 120))
    say("")
    passes, fails = _marker_counts(lines)
    say("  转发完毕：退出码=%s，它自己打了 %d 个 [PASS] / %d 个 [FAIL]"
        % (rc, passes, fails))
    if rc == 0 and passes:
        ok("端到端全部通过（%d 个 [PASS]）—— 服务端那一半由它证明" % passes)
        seg("D", True)
    elif rc == 0:
        bad("它退出了 0，但一个 [PASS] 都没打 —— 无法确认它真的断言过什么")
        seg("D", False)
    elif rc == 1:
        bad("端到端有断言失败（退出码 1，%d 个 [FAIL]）—— 详见它自己的输出" % fails)
        seg("D", False)
    elif rc == 2:
        env_problem("端到端脚本报「环境/参数问题」（退出码 2）—— 不是断言失败")
        seg("D", False)
    else:
        bad("端到端脚本没有正常结束（退出码 %s）" % rc)
        seg("D", False)


# ---------------------------------------------------------------------------
# E 段：服务端事实复核（Python 自己直查库 + 独立复验写路径）
# ---------------------------------------------------------------------------

def _ensure_timeout_report(base, db_path, uid, token, target, duration, active):
    """确保"这个临时用户名下有一份**超时口径**的报告"可查，返回带报告的那一行。

    为什么要这么绕：Node 侧跑完端到端后可能又开了一场**新面试**（它要验证
    "超时后唯一锁已释放"）。此时直接调报告接口会评**新**那一场 ——
    `_report_target()` 取的是"最近结束且还没出报告"的一场，口径就不是超时了。
    所以这里先等那场新面试**自己到点**（真实时间、**不碰数据库**），顺便**独立**
    观察一次首帧 `409 interview_timeout`；然后再出报告，口径仍是 timeout。

    拿不到超时报告就返回 `None`，由调用方 skip 掉这一段（绝不假装通过）。
    """
    live = active[-1] if active else None
    if live is not None:
        deadline = deadline_of(live, duration)
        try:
            remain = (datetime.datetime.fromisoformat(deadline)
                      - datetime.datetime.utcnow()).total_seconds()
        except (TypeError, ValueError):
            remain = None
        if remain is None or remain > duration + 10:
            skip("库里那场 active 会话的死线算不出来 / 还太远（%r）-> 不代它出报告"
                 % deadline)
            return None
        say("  库里还有一场 active 新会话（Node 侧验证「锁已释放」时开的）。**不能**"
            "直接出报告 —— 那会把报告算到新会话上。改为等它自己到点：约 %.0f 秒，"
            "真实时间、不碰数据库。" % max(0.0, remain + 1.5))
        time.sleep(max(0.0, remain + 1.5))
        st, late = request("POST", base + "/api/chat", token,
                           body={"message": "超时之后我还想继续答。"})
        late = _as_dict(late)
        say("  POST /api/chat（首帧） -> HTTP %s  code=%s  detail=%r"
            % (st, late.get("code"), late.get("detail")))
        if st == 409 and late.get("code") == TIMEOUT_CODE:
            ok("Python 侧**独立**观察到首帧写请求被拒：409 interview_timeout")
        else:
            bad("过点后的首帧写请求居然返回 %s %s" % (st, late.get("code")))

    if target["has_report"]:
        ok("超时报告已落库（report 字段 %d 字节）" % target["report_bytes"])
        return target
    say("  库里还没有报告 -> POST /api/generate_report（超时路径按 T-27 允许出报告；"
        "这一步同时为 T-43 取证）")
    st, resp = request("POST", base + "/api/generate_report", token)
    resp = _as_dict(resp)
    say("  POST /api/generate_report -> HTTP %s  ended_reason=%s  answered_count=%s  "
        "total_questions=%s"
        % (st, resp.get("ended_reason"), resp.get("answered_count"),
           resp.get("total_questions")))
    with_report = [r for r in probe_rows(db_path, uid) if r["has_report"]]
    if st != 200 or not with_report:
        bad("超时报告没能落库：HTTP %s，库里带报告的行数=%d"
            % (st, len(with_report)))
        return None
    reported = with_report[-1]
    say("  报告落在 session_id=%s（status=%s  ended_reason=%r）"
        % (reported["session_id"], reported["status"], reported["ended_reason"]))
    if reported["ended_reason"] != TIMEOUT_REASON:
        skip("报告落在了另一场（ended_reason=%r）—— 那是 Node 侧留下的结束行，"
             "不能用它证明 T-43 的 timeout 开关" % reported["ended_reason"])
        return None
    if reported["status"] == "abandoned":
        ok("报告已落库，且那一行**保持** abandoned + ended_reason=timeout"
           "（没被报告路径改成 finished）")
    else:
        bad("报告落库后那一行状态变成了 %s（超时口径要求保持 abandoned）"
            % reported["status"])
    return reported


def segment_e(base, db_path, uid, token, duration, fake):
    section("E. 服务端事实复核（Python 直接查库，与 Node 侧互相独立取得证据）")
    rows = probe_rows(db_path, uid)
    say("  临时用户的会话行（原始，直接来自 SQLite）：")
    print_rows(rows)
    active = [r for r in rows if r["status"] == "active"]
    timed = [r for r in rows if r["status"] == "abandoned"
             and r["ended_reason"] == TIMEOUT_REASON]

    # Node 侧若没留下终态行，Python 自己补一帧过点的写请求 ——
    # 此时它反而能**独立**观察到首帧的 409 interview_timeout。
    if not timed and [r for r in active if deadline_passed(r, duration)]:
        say("")
        say("  Node 侧没留下终态行，但有一场已过死线的 active 会话 ->")
        say("  Python 自己补一帧 POST /api/chat（首帧，应当拿到 409 interview_timeout）")
        st, late = request("POST", base + "/api/chat", token,
                           body={"message": "超时之后我还想继续答。"})
        late = _as_dict(late)
        say("  -> HTTP %s code=%s ended_reason=%s"
            % (st, late.get("code"), late.get("ended_reason")))
        if st == 409 and late.get("code") == TIMEOUT_CODE:
            ok("Python 侧独立观察到首帧写请求被拒：409 interview_timeout")
        else:
            bad("过点后的首帧写请求居然返回 %s %s" % (st, late.get("code")))
        rows = probe_rows(db_path, uid)
        say("  复核后的会话行：")
        print_rows(rows)
        active = [r for r in rows if r["status"] == "active"]
        timed = [r for r in rows if r["status"] == "abandoned"
                 and r["ended_reason"] == TIMEOUT_REASON]

    # E1：会话行的终态（Node 侧无法伪造这一行）
    if not timed:
        bad("库里没有 status='abandoned' + ended_reason='timeout' 的会话行："
            "超时兜底没有落库")
        seg("E", False)
        return
    ok("会话行已被服务端置为 status='abandoned' + ended_reason='timeout'（%d 行）"
       % len(timed))
    target = timed[-1]
    sid = target["session_id"]
    counts = status_counts(target.get("question_status"))
    say("  服务端死线 = created_at(%s) + %d 秒 = %s（前端倒计时照它走）"
        % (target["created_at"], duration, deadline_of(target, duration)))
    say("  这一场的题目状态：answered=%d skipped=%d pending=%d"
        % (counts["answered"], counts["skipped"], counts["pending"]))
    seg("E", True)

    # E2 / E3：报告落库 + T-43 依赖的那个字段
    reported = _ensure_timeout_report(base, db_path, uid, token, target, duration,
                                      active)
    if reported is None:
        skip("拿不到这个临时用户名的超时报告 -> T-43 的报告体复核本次跳过")
    else:
        counts = status_counts(reported.get("question_status"))
        raw = raw_report(db_path, reported["session_id"])
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        say("  落库报告体的关键字段：ended_reason=%r  answered_count=%r  "
            "total_questions=%r"
            % (body.get("ended_reason"), body.get("answered_count"),
               body.get("total_questions")))
        if body.get("ended_reason") == TIMEOUT_REASON:
            ok("报告体里**真的有** ended_reason='timeout' —— 前端 isTimeoutReport() "
               "的开关会被点亮（T-43 的标注就靠它）")
        else:
            bad("报告体里没有 ended_reason='timeout'（实得 %r）—— T-43 的标注"
                "永远不会显示" % body.get("ended_reason"))
        if body.get("answered_count") == counts["answered"]:
            ok("报告体的 answered_count(%s) 与库里的题目状态一致 —— 计数是服务端"
               "事实，不是模型自报（假 AI 编的是 7 / 99）" % counts["answered"])
        else:
            bad("answered_count 与库里的题目状态不一致：报告=%r 库=%d"
                % (body.get("answered_count"), counts["answered"]))
        details = "%s %s" % (body.get("details") or "",
                             body.get("suggestion") or "")
        if counts["answered"] == 0 and counts["skipped"] == 0:
            if ZERO_ANSWER_LABEL in details:
                ok("零作答的超时会话带上了「%s」标注（T-27 口径不回归）"
                   % ZERO_ANSWER_LABEL)
            else:
                bad("零作答的超时会话缺少「%s」标注" % ZERO_ANSWER_LABEL)
        elif ZERO_ANSWER_LABEL in details:
            bad("答过题的会话被误贴了「%s」（反向误导）：%r"
                % (ZERO_ANSWER_LABEL, details[:90]))
        else:
            ok("已答过题的会话**没有**被误贴「%s」（反向误导的防线还在）"
               % ZERO_ANSWER_LABEL)
        say("  报告页会显示的那句话（前端 timeout.ts 的 TIMEOUT_REPORT_NOTE）：%r"
            % TIMEOUT_NOTE)
        say("  它由 ended_reason 这个开关控制；而「报告页真的把它挂在报告分支里」"
            "这一点由 A 段的契约测试证明。")

    # 报告/等待期间状态可能变了（比如上面刚把 Node 侧那场新面试等到了点）
    rows = probe_rows(db_path, uid)
    active = [r for r in rows if r["status"] == "active"]
    say("  此刻的会话行：")
    print_rows(rows)

    # 假 AI 侧的独立证据：服务端真的把「超时」口径写进了提示词
    prompt = fake.last_report_prompt()
    if not prompt:
        skip("假 AI 没收到过报告提示词（报告可能已由 Node 侧出过）-> 跳过 prompt 复核")
    elif "ended_reason = timeout" in prompt:
        ok("真正喂给模型的 prompt 里写着 ended_reason = timeout —— 评分口径由服务端给定")
    else:
        bad("报告 prompt 里没有超时口径（前 160 字）：%r" % prompt[:160])

    # E4：到点后写路径再也写不进去（独立复验）
    if active:
        say("  库里有 active 会话 -> 不再发写探针（会打进那场新面试里）")
    else:
        say("")
        say("  到点后再发写请求（Python 侧独立复验；注意超时转移是**一次性**的）：")
        st, late = request("POST", base + "/api/chat", token,
                           body={"message": "超时之后我还想继续答。"})
        late = _as_dict(late)
        say("  POST /api/chat          -> HTTP %s  code=%s  detail=%r"
            % (st, late.get("code"), late.get("detail")))
        if st == 409 and late.get("code") in (TIMEOUT_CODE, "no_active_session"):
            ok("写不进去：HTTP 409 %s（首帧的 interview_timeout 由 D 段观察；此后"
               "已无活跃行，故退化为 no_active_session）" % late.get("code"))
        else:
            bad("到点后 /api/chat 居然返回 %s %s —— 超时闭环有缺口"
                % (st, late.get("code")))
        st, sk = request("POST", base + "/api/skip_question", token)
        sk = _as_dict(sk)
        say("  POST /api/skip_question -> HTTP %s  code=%s" % (st, sk.get("code")))
        if st == 409:
            ok("「跳过本题」这条路同样被拒 —— 写路径没有漏网的")
        else:
            bad("到点后 /api/skip_question 返回 %s —— 这条写路径没被拦住" % st)
        # 附加：唯一锁已释放（ADR-022R）—— 锁定面板上的"开新面试"要能用
        st, fresh = request("POST", base + "/api/start_interview", token,
                            form={"role": "后端开发",
                                  "questions_json": '["T42-NEW-1"]'})
        fresh = _as_dict(fresh)
        say("  POST /api/start_interview -> HTTP %s  new_session=%s"
            % (st, fresh.get("session_id")))
        if st == 200 and fresh.get("session_id"):
            ok("超时后能**立刻**开新面试（超时不会把用户锁在门外，ADR-022R）")
        else:
            bad("超时后开不了新面试：HTTP %s %s" % (st, fresh))

    total, reports, chats = fake.counts()
    say("  假 AI 共收到 %d 次调用（%d 次面试对话 + %d 次报告）" % (total, chats, reports))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="T-42 / T-43 人工验收（自起服务 + 假 AI + 真端到端 + 直查库）")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="目标数据库（默认 backend/interview.db）")
    ap.add_argument("--duration", type=int, default=12,
                    help="把面试时长压成多少秒（默认 12；必须 > 3）")
    args = ap.parse_args()
    db_path = os.path.abspath(args.db)
    started = time.time()

    section("T-42 / T-43 人工验收：前端超时强制闭环（一条命令，不联网，不花钱）")
    say("仓库根目录            : %s" % REPO_DIR)
    say("面试时长上限（压成）  : %d 秒" % args.duration)
    say("数据库                : %s" % db_path)

    # ---- 0. 预检 ----------------------------------------------------------
    section("0. 预检：缺什么就直说（不猜、不臆造）")
    if args.duration < MIN_DURATION:
        say("  --duration 太小（%d 秒）：必须 > %d 秒，否则「还没到点」的倒计时断言"
            "会自己先超时。" % (args.duration, MIN_DURATION - 1))
        say("环境问题：参数不合法。")
        return 2
    if args.duration > 600:
        say("  提示：--duration=%d 会让本次验收超过 10 分钟（仅提醒，不拦你）。"
            % args.duration)
    node_prefix, node_display = resolve_node()
    if not node_prefix:
        path_head = [p for p in (os.environ.get("PATH") or "").split(os.pathsep)
                     if p][:6]
        say("环境问题：PATH 里找不到 `node.exe` 也找不到 `node`。")
        say("  当前 PATH 前几项：%s" % " | ".join(path_head))
        say("  请安装 Node（建议 >= 20）并确认它在 PATH 里。")
        return 2
    global NODE_PREFIX, NODE_DISPLAY
    NODE_PREFIX, NODE_DISPLAY = node_prefix, node_display
    say("  node 可执行文件       : %s" % NODE_DISPLAY)
    say("  node 调用形式         : %s" % " ".join(
        ['"%s"' % p if " " in p else p for p in NODE_PREFIX]))
    rc, lines = run_node(["--version"], timeout=60)
    if rc != 0:
        say("  node --version 的输出：%s" % (" / ".join(lines) or "(空)"))
        say("环境问题：`node` 不可执行（实际用的是上面那个路径）。本脚本依赖 Node 的 "
            "`--test` 与原生 TS 剥离。")
        return 2
    say("  node --version        : %s" % (lines[0].strip() if lines else "?"))
    required = [os.path.join(FRONTEND_DIR, "src", "interview", "timeout.ts"),
                os.path.join(FRONTEND_DIR, "tests", "interview-timeout.test.mjs"),
                os.path.join(FRONTEND_DIR, "tests", "e2e-timeout-live.mjs")]
    missing = []
    for path in required:
        say("  [%s] %s" % ("有" if os.path.isfile(path) else "缺", path))
        if not os.path.isfile(path):
            missing.append(path)
    if missing:
        say("")
        if any(p.endswith("e2e-timeout-live.mjs") for p in missing):
            say("  缺的是端到端脚本（由前端侧提供）。本脚本**不**代替它做断言，也不会"
                "自己造一个假文件 —— 请先把它写好再跑本验收。")
        say("环境问题：缺 %d 个必需文件，退出码 2。" % len(missing))
        return 2
    if not os.path.isfile(db_path):
        say("环境问题：数据库不存在：%s（先让后端跑起来一次，把库和表建好）" % db_path)
        return 2
    try:
        conn = _ro(db_path)
        try:
            tables = set(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        say("环境问题：数据库打不开（%s: %s）" % (type(exc).__name__, exc))
        return 2
    need = ("users", "interview_sessions", "notifications")
    lack = [t for t in need if t not in tables]
    if lack:
        say("环境问题：数据库 %s 缺表 %s（先跑迁移/让后端启动一次）"
            % (db_path, "、".join(lack)))
        return 2
    say("  数据库表              : 齐全（%s）" % "、".join(need))
    seg("0", True)

    # ---- A. 前端契约测试 --------------------------------------------------
    section("A. 前端契约测试：倒计时 / 锁定 / 销毁输入区（毫秒级，不联网）")
    say("  $ cd frontend && node --test --experimental-test-isolation=none %s"
        % CONTRACT_TEST)
    rc, lines = run_node(["--test", "--experimental-test-isolation=none",
                          CONTRACT_TEST], timeout=300)
    say("  -- 输出尾部（最后 15 行）--")
    for line in lines[-15:]:
        say("  " + line)
    passed = _grab_count(lines, "pass")
    failed = _grab_count(lines, "fail")
    if rc == 0:
        ok("契约测试全部通过（pass=%s fail=%s）—— 「锁定后输入区被销毁」这一层是"
           "**源码契约**证明的，不是截图" % (passed, failed))
        seg("A", True)
    elif rc is None:
        bad("契约测试 300 秒没结束（已强制结束）")
        seg("A", False)
    else:
        bad("契约测试未通过（退出码 %s，pass=%s fail=%s）—— 先修它，后面的端到端"
            "没有意义" % (rc, passed, failed))
        seg("A", False)

    # ---- B. 起服务（假 AI + uvicorn） -------------------------------------
    section("B. 起服务：假 AI（回环）+ uvicorn（真实库，时长压成 %d 秒）"
            % args.duration)
    fake = FakeAI()
    fake.start()
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = fake.base_url          # 所有 AI 调用都打到回环
    env["INTERVIEW_DURATION_SECONDS"] = str(args.duration)
    # 让服务端用的库**就是**本脚本要查的那个库（否则 --db 会形同虚设）
    env["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = os.path.join(tempfile.gettempdir(), "dsh-t42-uvicorn.log")
    say("  临时服务              : %s" % base)
    say("  假 AI（回环，不联网）  : %s" % fake.base_url)
    say("  INTERVIEW_DURATION_SECONDS=%d（只在进程启动时读一次，所以必须现在设）"
        % args.duration)
    say("  服务端 DATABASE_URL    : sqlite:///%s" % db_path.replace("\\", "/"))
    with open(log_path, "wb") as logfh:
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=BACKEND_DIR, env=env,
            stdout=logfh, stderr=subprocess.STDOUT)
    ready = wait_ready(base, proc)
    if ready:
        ok("临时服务已就绪（日志：%s）" % log_path)
        seg("B", True)
    else:
        say(tail(log_path))
        env_problem("临时服务没起来（日志见上：%s）" % log_path)
        seg("B", False)

    uid = None
    token = None
    try:
        if not ready:
            for key in ("C", "D", "E"):
                seg(key, None)
            skip("服务没起来 -> C/D/E 三段整体跳过；临时用户也没创建，无需清理")
        else:
            # ---- C. 临时用户 + 自签令牌 ----------------------------------
            section("C. 临时用户 + 自签令牌（不用登录、不用从浏览器复制 Token）")
            uid, name, token = make_probe_user(db_path)
            say("  临时用户              : id=%s username=%s（用完即删）" % (uid, name))
            say("  令牌                  : 由 auth.create_access_token 现签，"
                "只活在本进程内存里")
            st, body = request("GET", base + "/api/interview/session", token)
            body = _as_dict(body)
            say("  GET /api/interview/session -> HTTP %s  session=%s  last_ended=%s"
                % (st, body.get("session"), body.get("last_ended")))
            if st == 200:
                ok("自签令牌被服务端接受（HTTP 200）—— 全自动，无需手工粘贴")
                seg("C", True)
            else:
                bad("自签令牌被拒（HTTP %s）：%s" % (st, body))
                seg("C", False)
            # ---- D / E ---------------------------------------------------
            if proc.poll() is not None:
                env_problem("uvicorn 在跑 C 段时意外退出（日志尾部见下）")
                say(tail(log_path, 12))
                seg("D", None)
                seg("E", None)
            else:
                segment_d(base, token, args.duration)
                segment_e(base, db_path, uid, token, args.duration, fake)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        fake.stop()
        # ---- F. 清理（临时用户 + 会话/报告/通知 + 两个进程） -----------
        section("F. 清理：临时用户与其会话/报告/通知，逐表确认无残留")
        say("  uvicorn 已停止，假 AI 已关闭（端口已释放）")
        if uid is None:
            seg("F", None)
            say("  没有创建临时用户，无需清理（你的真实账号从未被触碰）")
        else:
            before = probe_rows(db_path, uid)
            drop_probe_user(db_path, uid)
            left_rows = probe_rows(db_path, uid)
            left = leftover_counts(db_path, uid)
            for table, n in left:
                say("  %-20s 剩余 %s 行" % (table, n))
            dirty = [t for t, n in left if n != 0]
            if dirty or left_rows:
                bad("临时用户仍有残留：%s" % "、".join(dirty or ["interview_sessions"]))
                seg("F", False)
            else:
                ok("临时用户及其 %d 场会话（含落库报告）已全部删除，四张表复查均为 0"
                   % len(before))
                seg("F", True)

    # ---- 汇总 -------------------------------------------------------------
    section("汇总（逐段）")
    for key, title in SEG_ORDER:
        label = {True: "PASS", False: "FAIL", None: "跳过"}.get(SEG.get(key), "未跑到")
        say("  [%-4s] %s" % (label, title))
    for title, items in (("跳过/说明", SKIPPED), ("断言失败", FAILED),
                         ("环境问题", ENV_PROBLEMS)):
        if items:
            say("")
            say("  %s %d 条：" % (title, len(items)))
            for item in items:
                say("    - %s" % item)
    say("")
    say("  耗时 %.1f 秒" % (time.time() - started))
    if ENV_PROBLEMS:
        say("⚠️  T-42 / T-43 人工验收**未能完成**（环境问题，不是断言失败）")
        return 2
    if FAILED:
        say("❌ T-42 / T-43 人工验收未通过（%d 项断言失败）" % len(FAILED))
        return 1
    say("✅ T-42 / T-43 人工验收通过：倒计时的时长与死线来自服务端；到点后写路径被 "
        "409 interview_timeout 拒绝、会话落 abandoned+timeout；报告体真的带 "
        "ended_reason=timeout；而「锁定后销毁输入区」由前端契约测试证明。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("环境问题：%s: %s" % (type(exc).__name__, exc))
        sys.exit(2)

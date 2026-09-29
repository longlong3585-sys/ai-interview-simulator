"""T-28 人工验收：**傻瓜版**（一条命令，不用起服务、不用写 curl、不用手动复制 Token、不花钱、不联网）。

## 它证明什么

T-28 修的是 FR-4.12 / Bug 3B：**"15 分钟归零"原先只活在前端的倒计时里**。
归零之后再手工发一次 `POST /api/chat`，服务端照样把这一轮记进会话 ——
"超时了还能一直答下去"，业务规则形同虚设。修法是：**超时时刻由服务端掌握**
（死线 = 会话行的 `created_at` + 时长上限），到点后由服务端把会话置
`abandoned`（`ended_reason=timeout`）并释放唯一锁。

脚本自己做了三件事，因此**不需要你手工配合任何一步**：

  1. 起一个**本机假 AI**（回环，永不联网），把收到的 prompt 原样记下来；
  2. 用 `OPENAI_BASE_URL` 指向它，另起一个 uvicorn，并把面试时长压成
     `--duration` 秒（默认 20）—— 否则"人工验收"就得真的等一刻钟；
  3. 自己往库里造一个**临时用户**并**自己签令牌**（`create_access_token`），
     所以不需要你登录、也不需要从浏览器里复制 Token。

逐条证明：

  A. **服务端是时长的唯一来源**：`GET /api/interview/config` 下发的
     `duration_seconds` 与本次启动时设定的一致（前端不再自己硬编码）；
     会话读接口同时下发 `deadline_at` / `interview_remaining_seconds`。
  B. **不误伤**：没到点的会话照常能答题（库里仍是 `active`）。
  C. **到点后写路径一律 409**：**等真实时间走过去**（不碰数据库、不改任何
     数据 —— 这是"服务端自己掌握超时时刻"的关键证据），随后
     `POST /api/chat` 必须 409 且 `code=interview_timeout`；
     会话落库为 `abandoned` + `ended_reason=timeout`；再发一次仍然 409。
  D. **明确反馈**（FR-4.12 第③条）：`GET /api/interview/session` 返回
     `session: null` 且 `last_ended.ended_reason = "timeout"`
     —— 前端据此能说"因超时已自动结束"，而不是"你没有任何面试"。
  E. **唯一锁已释放**（ADR-022R）：超时后**立刻**能开新面试（HTTP 200）。
  F. **超时也出报告且不回归 T-27**：`ended_reason=timeout`、状态**保持**
     `abandoned`（不是 `finished`）、零作答必须带「未及作答，无法评分」标注，
     且真正喂给模型的 prompt 里写明"因超时自动结束"。
  G. **不重复计费**：已经出过报告再请求 → 409，且**不会**再调一次 AI。
  H. **客户端无法伪造超时**：正常答完一场（没到点）→ `ended_reason=completed`；
     请求体里塞 `ended_reason=timeout` 也不会让服务端改口
     —— 结束原因只由服务端时钟裁定。

全程使用**临时用户**，结束时连同会话一起删除，不碰你的真实账号。

## 用法

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t28_manual.py

可选参数：

    --duration 5     把面试时长压成 5 秒（默认 20；越小跑得越快）
    --db <路径>      指定要用的数据库（默认 backend/interview.db）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。
"""
import argparse
import datetime
import json
import os
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

import os as _archive_os

# 归档位置：backend/scripts/archive/<本文件> —— 四层 dirname 即仓库根。
_ARCHIVE_REPO = _archive_os.path.dirname(_archive_os.path.dirname(
    _archive_os.path.dirname(_archive_os.path.dirname(
        _archive_os.path.abspath(__file__)))))
BACKEND_DIR = os.path.join(_ARCHIVE_REPO, "backend")
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")
PY = os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe")

FAILED = []

#: 假 AI 的固定回复：一份**没有**任何标注、看起来"全错"的报告。
#: 用它来证明超时口径与"未及作答"标注都是**服务端**给的，而不是碰巧模型写了。
FAKE_REPORT = json.dumps({
    "expression_score": 0, "technical_score": 0, "logic_score": 0,
    "overall_score": 0.0, "answered_count": 7, "total_questions": 99,
    "suggestion": "本次为假 AI 的固定回复，不代表真实评分。",
    "details": "假 AI 的固定总结（故意不含任何超时说明）。",
}, ensure_ascii=False)

LABEL = "未及作答，无法评分"


def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say("  [PASS] %s" % msg)


def bad(msg):
    FAILED.append(msg)
    say("  [FAIL] %s" % msg)


def section(title):
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _as_dict(value):
    """响应体可能是错误字符串（连不上等）—— 统一成 dict，避免 .get 崩掉。"""
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


# ---------------------------------------------------------------------------
# 假 AI（回环，永不联网）
# ---------------------------------------------------------------------------

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

        if "overall_score" in user or "面试评估专家" in system:
            content = FAKE_REPORT
        else:
            content = "【假面试官】收到，请继续。"
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
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def report_prompts(self):
        return [r for r in list(self.httpd.records)
                if "overall_score" in r["user"] or "面试评估专家" in r["system"]]

    def last_report_prompt(self):
        prompts = self.report_prompts()
        return prompts[-1]["user"] if prompts else ""

    def start(self):
        self.thread.start()

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 临时用户 / 库操作
# ---------------------------------------------------------------------------

def make_probe_user(db_path):
    """造一个临时用户并**自己签令牌** —— 无需你登录或复制 Token。"""
    from auth import create_access_token, get_password_hash
    name = "t28_probe_%s" % uuid.uuid4().hex[:8]
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


def probe_rows(db_path, uid):
    """只读地看这个临时用户的会话行（`report` 换成是否存在的布尔值）。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, status, ended_reason, current_index, "
            "       created_at, expires_at, "
            "       (report IS NOT NULL AND report <> '') AS has_report "
            "FROM interview_sessions WHERE user_id=?", (uid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def row_of(db_path, uid, session_id):
    for r in probe_rows(db_path, uid):
        if r["session_id"] == session_id:
            return r
    return None


def age_session(db_path, session_id, seconds_ago):
    """把会话的 `created_at` 改到过去 —— **只用于"零作答超时"这一条**。

    为什么不在这里也等真实时间：C 段已经用真实时钟证明过"服务端自己会到点"
    了；这一段要验证的是**报告口径**（零作答 + 超时 → 不得当作全错）。
    改 `created_at` 得到的库状态与真实到点**完全一致**（它正是服务端用来算
    死线的那一列），因此不削弱任何结论，只省掉一次等待。
    """
    past = (datetime.datetime.utcnow()
            - datetime.timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE interview_sessions SET created_at=? "
                     "WHERE session_id=?", (past, session_id))
        conn.commit()
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="T-28 人工验收（自起服务 + 假 AI）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--duration", type=int, default=20,
                    help="把面试时长压成多少秒（默认 20）")
    args = ap.parse_args()

    if args.duration < 5:
        say("--duration 太小（至少 5 秒），否则「没到点」的检查会自己先超时。")
        return 2

    fake = None
    proc = None
    log_path = os.path.join(tempfile.gettempdir(), "dsh-t28-uvicorn.log")

    section("T-28 人工验收：后端超时兜底（服务端自己掌握超时时刻）")
    say("面试时长上限（本次压成）: %d 秒" % args.duration)
    say("数据库                : %s" % args.db)

    # ---- 起 假 AI + 临时服务 ------------------------------------------------
    fake = FakeAI()
    fake.start()
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = fake.base_url          # 所有 AI 调用都打到回环
    env["INTERVIEW_DURATION_SECONDS"] = str(args.duration)
    env["PYTHONIOENCODING"] = "utf-8"
    say("临时服务              : %s" % base)
    say("假 AI（不联网）        : %s" % fake.base_url)
    with open(log_path, "wb") as logfh:
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=BACKEND_DIR, env=env,
            stdout=logfh, stderr=subprocess.STDOUT)
    if not wait_ready(base, proc):
        say(tail(log_path))
        bad("临时服务没起来（日志见上）")
        proc.terminate()
        fake.stop()
        return 2
    ok("临时服务已就绪（日志：%s）" % log_path)

    uid, name, token = make_probe_user(args.db)
    say("临时用户              : id=%s username=%s（用完即删）" % (uid, name))

    def start(questions='["T28-Q1","T28-Q2","T28-Q3"]'):
        return request("POST", base + "/api/start_interview", token,
                       form={"role": "后端开发", "questions_json": questions})

    def chat(text="我的回答", extra=None):
        body = {"message": text}
        if extra:
            body.update(extra)
        return request("POST", base + "/api/chat", token, body=body)

    def get_session():
        return request("GET", base + "/api/interview/session", token)

    def report():
        return request("POST", base + "/api/generate_report", token)

    try:
        # ---- A. 服务端是时长的唯一来源 -------------------------------------
        section("A. 服务端是时长的唯一来源（前端不再硬编码 15 分钟）")
        st, cfg = request("GET", base + "/api/interview/config", token)
        cfg = _as_dict(cfg)
        say("  GET /api/interview/config -> HTTP %s  duration_seconds=%s"
            % (st, cfg.get("duration_seconds")))
        if st == 200 and cfg.get("duration_seconds") == args.duration:
            ok("下发的时长 = 本次启动设定的 %d 秒（服务端说了算）" % args.duration)
        else:
            bad("时长下发不对：期望 %d，实得 %r"
                % (args.duration, cfg.get("duration_seconds")))

        # ---- B. 不误伤：没到点照常答题 --------------------------------------
        section("B. 没到点：照常能答题（不误伤）")
        st, first = start()
        first = _as_dict(first)
        sid1 = first.get("session_id")
        if st != 200 or not sid1:
            bad("开不了面试：HTTP %s %s" % (st, first))
            raise SystemExit(1)
        st, body = get_session()
        sess = _as_dict(body).get("session") or {}
        say("  服务端下发的死线: deadline_at=%s  剩余=%s 秒"
            % (sess.get("deadline_at"), sess.get("interview_remaining_seconds")))
        if sess.get("duration_seconds") == args.duration and sess.get("deadline_at"):
            ok("会话里带上了业务死线（前端倒计时可以照它走）")
        else:
            bad("会话没有下发 deadline_at/duration_seconds：%r" % sess)

        st, replied = chat("第一题：这是我的回答。")
        row = row_of(args.db, uid, sid1)
        say("  POST /api/chat -> HTTP %s；库里 status=%s"
            % (st, row["status"] if row else "?"))
        if st == 200 and row and row["status"] == "active":
            ok("没到点就能正常答题，会话仍是 active（没有误杀）")
        else:
            bad("没到点就被拦下了：HTTP %s，库=%s" % (st, row))

        # ---- C. 真的等到点（不碰数据库！） ----------------------------------
        section("C. **等真实时间**走到死线，再看服务端是否自己兜底")
        created = row["created_at"] if row else None
        if created:
            begun = datetime.datetime.fromisoformat(created)
            wait = (begun + datetime.timedelta(seconds=args.duration + 1.5)
                    - datetime.datetime.utcnow()).total_seconds()
        else:
            wait = args.duration + 2
        say("  等待 %.1f 秒（不修改数据库、不伪造任何字段）..." % max(0, wait))
        time.sleep(max(0, wait))

        st, late = chat("超时之后我还想继续答。")
        late = _as_dict(late)
        row = row_of(args.db, uid, sid1)
        say("  POST /api/chat -> HTTP %s  code=%s  ended_reason=%s"
            % (st, late.get("code"), late.get("ended_reason")))
        say("  请求体 detail  = %r" % (late.get("detail"),))
        say("  库里           = status:%s ended_reason:%s"
            % (row["status"], row["ended_reason"]))
        if st == 409 and late.get("code") == "interview_timeout":
            ok("到点后服务端**自己**把会话结束了，/api/chat 返回 409 interview_timeout")
        else:
            bad("到点后 /api/chat 居然还是 %s（修复前就是这个 Bug）" % st)
        if row and (row["status"], row["ended_reason"]) == ("abandoned", "timeout"):
            ok("会话状态 = abandoned，ended_reason = timeout（不是 finished）")
        else:
            bad("会话状态不对：%s" % row)

        st2, late2 = chat("再试一次。")
        if st2 == 409:
            ok("再发一次仍然是 409（超时后不可能再答上题）")
        else:
            bad("第二次请求变成了 HTTP %s —— 锁定可被反复试探" % st2)

        # ---- D. 明确反馈 ----------------------------------------------------
        section("D. 明确反馈：前端能说清「因超时已自动结束」")
        st, body = get_session()
        body = _as_dict(body)
        ended = body.get("last_ended") or {}
        say("  GET /api/interview/session -> session=%s last_ended=%s"
            % (body.get("session"), ended))
        if body.get("session") is None and ended.get("status") == "abandoned" \
                and ended.get("ended_reason") == "timeout" \
                and ended.get("session_id") == sid1:
            ok("session=null 且 last_ended 指明「上一场因超时结束」（含 has_report=%s）"
               % ended.get("has_report"))
        else:
            bad("读接口没给出超时反馈：%r" % body)

        # ---- F. 超时也出报告（T-27 不回归） ---------------------------------
        section("F. 超时也能出报告，且口径是 timeout（T-27 不回归）")
        st, rep = report()
        rep = _as_dict(rep)
        prompt = fake.last_report_prompt()
        say("  POST /api/generate_report -> HTTP %s  ended_reason=%s"
            % (st, rep.get("ended_reason")))
        say("  answered_count=%s total_questions=%s（假 AI 编的是 7 / 99）"
            % (rep.get("answered_count"), rep.get("total_questions")))
        row = row_of(args.db, uid, sid1)
        if st == 200 and rep.get("ended_reason") == "timeout":
            ok("超时后仍能拿到报告，ended_reason=timeout")
        else:
            bad("超时报告不对：HTTP %s ended_reason=%s" % (st, rep.get("ended_reason")))
        if row and row["status"] == "abandoned" and row["has_report"]:
            ok("报告已落库，且状态**保持** abandoned（没被报告路径改成 finished）")
        else:
            bad("落库状态不对：%s" % row)
        if rep.get("answered_count") == 1 and rep.get("total_questions") == 3:
            ok("计数由服务端裁决（模型自报的 7 / 99 被覆盖）")
        else:
            bad("计数没有由服务端裁决：%s / %s"
                % (rep.get("answered_count"), rep.get("total_questions")))
        if "ended_reason = timeout" in prompt and "因超时自动结束" in prompt:
            ok("真正喂给模型的 prompt 里写明了「因超时自动结束」（口径由服务端给定）")
        else:
            bad("prompt 里没有超时口径（前 200 字）：%r" % prompt[:200])
        if LABEL not in (rep.get("details") or ""):
            ok("这一场已经答过题 -> 没有被误贴「%s」（反向误导的防线）" % LABEL)
        else:
            bad("答过题的会话被贴上了「%s」：%r" % (LABEL, rep.get("details")))

        # ---- F2. 零作答超时：不得当成"全错" ---------------------------------
        section("F2. **零作答**超时：必须标注「%s」，不得当作全错" % LABEL)
        st, zero = start('["T28-Z1","T28-Z2"]')
        zero = _as_dict(zero)
        sid3 = zero.get("session_id")
        if st != 200 or not sid3:
            bad("开不了零作答会话：HTTP %s %s" % (st, zero))
        else:
            age_session(args.db, sid3, args.duration + 60)
            st, blocked = chat("我还没答第一题就到了。")
            blocked = _as_dict(blocked)
            say("  零作答会话到点后 /api/chat -> HTTP %s code=%s"
                % (st, blocked.get("code")))
            if st == 409 and blocked.get("code") == "interview_timeout":
                ok("零作答的会话同样被服务端兜底拦住")
            else:
                bad("零作答会话没被拦住：HTTP %s %s" % (st, blocked.get("code")))

            st, repz = report()
            repz = _as_dict(repz)
            row = row_of(args.db, uid, sid3)
            say("  出报告 -> HTTP %s ended_reason=%s answered_count=%s"
                % (st, repz.get("ended_reason"), repz.get("answered_count")))
            say("  details = %r" % (repz.get("details") or "")[:90])
            if st == 200 and repz.get("ended_reason") == "timeout" \
                    and repz.get("answered_count") == 0:
                ok("零作答超时的报告：ended_reason=timeout、answered_count=0")
            else:
                bad("零作答超时报告不对：HTTP %s %s / %s"
                    % (st, repz.get("ended_reason"), repz.get("answered_count")))
            if LABEL in (repz.get("details") or ""):
                ok("带上了「%s」标注（即使假 AI 故意返回一份「全错」的报告）" % LABEL)
            else:
                bad("零作答超时缺少「%s」标注：%r" % (LABEL, repz.get("details")))
            if row and (row["status"], row["ended_reason"]) == ("abandoned", "timeout"):
                ok("落库：status=abandoned + ended_reason=timeout")
            else:
                bad("落库状态不对：%s" % row)

        # ---- G. 不重复计费 --------------------------------------------------
        section("G. 已出过报告再请求 -> 409，且不再调 AI")
        calls_before = len(fake.report_prompts())
        st, again = report()
        again = _as_dict(again)
        calls_after = len(fake.report_prompts())
        if st == 409 and again.get("code") == "no_active_session":
            ok("409 no_active_session")
        else:
            bad("期望 409 no_active_session，实得 %s %s" % (st, again.get("code")))
        if calls_after == calls_before:
            ok("没有重复调用 AI（%d 次不变）" % calls_before)
        else:
            bad("重复请求又调了一次 AI（%d -> %d）" % (calls_before, calls_after))

        # ---- E. 唯一锁已释放 -------------------------------------------------
        section("E. 唯一锁已释放：超时后能**立刻**开新面试（ADR-022R）")
        st, second = start('["T28-NEW-1"]')
        second = _as_dict(second)
        say("  POST /api/start_interview -> HTTP %s  new_session=%s"
            % (st, second.get("session_id")))
        if st == 200 and second.get("session_id") and second["session_id"] != sid1:
            ok("新面试正常开始（超时不会把用户锁在门外）")
        else:
            bad("开不了新面试：HTTP %s %s" % (st, second))

        # ---- H. 客户端无法伪造超时 ------------------------------------------
        section("H. 结束原因只由服务端时钟裁定（客户端无法伪造）")
        sid2 = second.get("session_id")
        st, _ = chat("正常作答第一题。", extra={"ended_reason": "timeout",
                                            "code": "interview_timeout",
                                            "status": "abandoned"})
        row = row_of(args.db, uid, sid2)
        say("  请求体里塞了 ended_reason=timeout 之后 -> HTTP %s；库里 status=%s"
            % (st, row["status"] if row else "?"))
        if st == 200 and row and row["status"] == "active":
            ok("请求体里的伪造字段被忽略，会话仍是 active（没到点就是没到点）")
        else:
            bad("客户端能把会话说成超时：HTTP %s 库=%s" % (st, row))

        st, rep2 = report()
        rep2 = _as_dict(rep2)
        row = row_of(args.db, uid, sid2)
        say("  未到点出报告 -> HTTP %s ended_reason=%s；库里 status=%s"
            % (st, rep2.get("ended_reason"), row["status"]))
        if st == 200 and rep2.get("ended_reason") == "completed" \
                and row["status"] == "finished":
            ok("正常完成的一场仍然是 completed + finished（不误伤正常路径）")
        else:
            bad("正常路径被改写了：HTTP %s ended_reason=%s 库=%s"
                % (st, rep2.get("ended_reason"), row))

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        if fake is not None:
            fake.stop()
        rows = probe_rows(args.db, uid)
        drop_probe_user(args.db, uid)
        left = probe_rows(args.db, uid)
        say("")
        if left:
            bad("临时用户仍有 %d 行会话残留" % len(left))
        else:
            ok("临时用户与其 %d 场会话已全部删除（你的真实账号未被触碰）"
               % len(rows))

    section("汇总")
    if FAILED:
        for f in FAILED:
            say("  FAIL: %s" % f)
        say("")
        say("❌ T-28 人工验收未通过（%d 项失败）" % len(FAILED))
        return 1
    say("✅ T-28 人工验收通过：服务端自己掌握超时时刻 ——")
    say("   到点后 /api/chat 返回 409（interview_timeout）；")
    say("   会话状态 abandoned + ended_reason=timeout；")
    say("   唯一锁已释放（可立刻重开）；超时报告口径与 T-27 一致。")
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

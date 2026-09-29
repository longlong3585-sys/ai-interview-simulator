"""T-27 人工验收：**傻瓜版**（一条命令，不用起服务、不用写 curl、不花钱、不联网）。

## 它证明什么

T-27 要修的是 **误导性 0 分**（FR-5.2 / ADR-007R）：
一个因为**超时**一道题都没答上的候选人，拿到的报告却和"全部答错"一模一样。

要证明修好了，光看接口返回 200 不够 —— 必须看到**真正喂给 AI 的那段 prompt**
里有什么、没有什么。所以脚本自己做了三件事：

  1. 起一个**本机假 AI**（回环，永不联网），把收到的 prompt 原样记下来；
  2. 用 `OPENAI_BASE_URL` 指向它，另起一个 uvicorn（真实数据库 + 临时用户）；
  3. 走完整流程，并用**真实的生产路径**把会话推入超时状态
     （改 `expires_at` 造出"已过期但仍 active"的行，再跑 `scripts/cleanup.py`
      —— 就是 systemd timer 每 15 分钟跑的那个清理任务）。

逐条证明：

  A. **契约**：`POST /api/generate_report` 仍然没有 requestBody（T-26 不回归）
  B. **正常路径不回归**：正常答完 → `ended_reason = completed`，状态 `finished`
  C. **超时也能出报告**（修复前这里 409）：
     超时后 `GET /api/interview/session` 返回 null（服务端认为"没在面试"），
     但 `generate_report` 必须仍能出报告，且 `ended_reason = timeout`；
     落库后**状态保持 `abandoned`**（不是 finished —— ADR-022R）。
  D. **提示词区分「未及作答 / 答不上」**：pending 的题不得计入扣分、
     skipped 照常计分、三题状态随之下发。
  E. **零作答超时不得当成"全错"**：即使假 AI 返回一份"看起来像全错"的报告，
     服务端也必须补上「未及作答，无法评分」标注。
  F. **反向误导的防线**：全部**主动跳过**时（answered 同样是 0），
     **不得**贴"未及作答，无法评分" —— 那是候选人明确拒绝回答，不是没机会。
  G. **不重复计费**：已经出过报告再请求 → 409，且**不会**再调一次 AI。
  H. **锁已释放**：超时出报告后能立刻开新一场面试。

全程使用**临时用户**，结束时连同会话一起删除，不碰你的真实账号。

## 用法

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t27_manual.py

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
SKIPPED = []

#: 假 AI 的固定回复：一份**没有**任何标注、看起来"全错"的报告。
#: 用它来证明标注是**服务端**补的，而不是碰巧模型写了。
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


def skip(msg):
    SKIPPED.append(msg)
    say("  [SKIP] %s" % msg)


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
# 假 AI
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
    from auth import create_access_token, get_password_hash
    name = "t27_probe_%s" % uuid.uuid4().hex[:8]
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
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"),
                           uri=True)
    try:
        return conn.execute(
            "SELECT session_id, status, ended_reason, report, current_index "
            "FROM interview_sessions WHERE user_id=?", (uid,)).fetchall()
    finally:
        conn.close()


def row_of(db_path, uid, session_id):
    for r in probe_rows(db_path, uid):
        if r[0] == session_id:
            return r
    return None


def force_expired(db_path, session_id, seconds_ago=120):
    """把 `expires_at` 改到过去，制造"已过期但仍 active"的行。

    这是**数据状态**，任何接口都不会产生它 —— 它正是 TTL 到期后、
    清理任务还没跑到时库里的真实样子（T-25 的自愈逻辑就是为它写的）。
    """
    past = (datetime.datetime.utcnow()
            - datetime.timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE interview_sessions SET expires_at=? "
                     "WHERE session_id=?", (past, session_id))
        conn.commit()
    finally:
        conn.close()


def run_cleanup(db_path):
    """跑**真实的**清理任务（systemd timer 每 15 分钟执行的那一个）。

    它会把"已过期但仍 active"的会话置为 `abandoned` + `ended_reason='timeout'`
    —— 也就是 T-28 的超时兜底将来要走的同一条路。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([PY, os.path.join("scripts", "cleanup.py"),
                           "--db", db_path],
                          cwd=BACKEND_DIR, capture_output=True, env=env,
                          timeout=120)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    return proc.returncode, out.strip()


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
    ap = argparse.ArgumentParser(description="T-27 人工验收（自起服务 + 假 AI）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--base", default=None,
                    help="指向你已经起好的服务；此时 prompt 级证据会被跳过")
    args = ap.parse_args()

    fake = None
    proc = None
    log_path = os.path.join(tempfile.gettempdir(), "dsh-t27-uvicorn.log")
    own_server = args.base is None
    ai_calls = {"n": 0}

    if own_server:
        fake = FakeAI()
        fake.start()
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        env = dict(os.environ)
        env["OPENAI_BASE_URL"] = fake.base_url
        env["PYTHONIOENCODING"] = "utf-8"
        say("启动临时服务：%s" % base)
        say("假 AI（不联网）：%s" % fake.base_url)
        with open(log_path, "wb") as logfh:
            proc = subprocess.Popen(
                [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                 "--port", str(port)], cwd=BACKEND_DIR, env=env,
                stdout=logfh, stderr=subprocess.STDOUT)
        if not wait_ready(base, proc):
            say(tail(log_path))
            bad("临时服务没起来（see log above）")
            if proc:
                proc.terminate()
            fake.stop()
            return 2
        ok("临时服务已就绪（日志：%s）" % log_path)
    else:
        base = args.base.rstrip("/")
        say("使用已有服务：%s" % base)
        say("！！ 该模式下脚本看不到 prompt，D 段会标为 SKIP")

    say("数据库  : %s" % args.db)
    st, _ = request("GET", base + "/openapi.json", timeout=10)
    if st != 200:
        bad("连不上服务（%s）" % st)
        if proc:
            proc.terminate()
        if fake:
            fake.stop()
        return 2

    uid, name, token = make_probe_user(args.db)
    say("临时用户: id=%s username=%s" % (uid, name))

    def start(questions):
        return request("POST", base + "/api/start_interview", token,
                       form={"role": "后端开发", "questions_json": questions})

    def chat(text):
        return request("POST", base + "/api/chat", token, body={"message": text})

    def skip_q():
        return request("POST", base + "/api/skip_question", token)

    def report():
        if fake is not None:
            ai_calls["n"] = len(fake.report_prompts())
        return request("POST", base + "/api/generate_report", token)

    def times_out(session_id, label):
        """把会话推入"超时"终态：过期数据 + 真实清理任务。"""
        force_expired(args.db, session_id)
        rc, out = run_cleanup(args.db)
        row = row_of(args.db, uid, session_id)
        say("  [%s] cleanup.py exit=%s；库里 status=%s reason=%s"
            % (label, rc, row[1], row[2]))
        return row

    try:
        # ---- A. 契约 ------------------------------------------------------
        section("A. 契约：POST /api/generate_report 仍然没有 requestBody（T-26 不回归）")
        st, spec = request("GET", base + "/openapi.json")
        op = (((spec or {}).get("paths", {}).get("/api/generate_report")
               or {}).get("post")) or {}
        if "requestBody" not in op and op.get("security"):
            ok("无 requestBody + 需要鉴权（前端仍无法注入 messages）")
        else:
            bad("接口契约变了：requestBody=%r security=%r"
                % (op.get("requestBody"), op.get("security")))

        # ---- B. 正常路径不回归 ---------------------------------------------
        section("B. 正常路径：答完就结束 → ended_reason = completed")
        st, first = start('["T27-N1","T27-N2","T27-N3"]')
        if st != 200:
            bad("start_interview 失败：HTTP %s %s" % (st, first))
            return 1
        chat("正常作答第一题")
        st, body = report()
        body = _as_dict(body)
        say("  HTTP %s  ended_reason=%s" % (st, body.get("ended_reason")))
        if st == 200 and body.get("ended_reason") == "completed":
            ok("正常结束仍标 completed")
        else:
            bad("正常路径的 ended_reason 不对：%s" % body.get("ended_reason"))
        row = row_of(args.db, uid, first["session_id"])
        if row[1] == "finished" and row[3]:
            ok("库里 status=finished 且报告已落库")
        else:
            bad("正常路径落库不对：status=%s report=%r" % (row[1], bool(row[3])))

        # ---- C. 超时也能出报告 ---------------------------------------------
        section("C. 超时路径：答 1 题 / 跳 1 题 / 留 1 题未答，然后超时")
        st, second = start('["T27-T1","T27-T2","T27-T3"]')
        sid = second["session_id"]
        chat("超时那场的回答")
        skip_q()
        before = len(fake.report_prompts()) if fake else 0

        row = times_out(sid, "超时")
        if row[1] == "abandoned" and row[2] == "timeout":
            ok("真实清理路径把会话置为 abandoned + timeout")
        else:
            bad("超时状态不对：status=%s reason=%s" % (row[1], row[2]))

        st, sess = request("GET", base + "/api/interview/session", token)
        if st == 200 and (sess or {}).get("session") is None:
            ok("GET /api/interview/session -> null（服务端认为没有进行中的面试）")
        else:
            bad("超时后 session 不是 null：HTTP %s %s" % (st, sess))

        say("  ---- 修复前：下面这一步会 409，用户超时后什么都拿不到 ----")
        st, rep = report()
        rep = _as_dict(rep)
        say("  HTTP %s  ended_reason=%s" % (st, rep.get("ended_reason")))
        if st == 200 and rep.get("ended_reason") == "timeout":
            ok("超时后**仍然能出报告**，且 ended_reason = timeout")
        else:
            bad("超时报告失败：HTTP %s %s" % (st, rep))

        if fake is not None and len(fake.report_prompts()) == before + 1:
            ok("评分真的走了 AI（1 次，且是打给本机假 AI）")

        row = row_of(args.db, uid, sid)
        say("  库里: status=%s ended_reason=%s report=%s…"
            % (row[1], row[2], (row[3] or "")[:56]))
        if row[1] == "abandoned":
            ok("落库报告**没有**把状态改成 finished（ADR-022R）")
        else:
            bad("写报告把超时会话变成了 %s —— ADR-022R 被违反" % row[1])
        if row[2] == "timeout":
            ok("ended_reason 仍是 timeout（没有被报告路径改写）")
        else:
            bad("ended_reason 被改成了 %s" % row[2])
        if row[3] and "overall_score" in row[3]:
            ok("报告已落库")
        else:
            bad("报告没有落库")

        # ---- D. 提示词证据 -------------------------------------------------
        section("D. 提示词证据：区分「未及作答」与「答不上」")
        if fake is None:
            skip("prompt 级证据需要脚本自己起服务（不要用 --base）")
        else:
            p = fake.last_report_prompt()
            checks = [
                ("ended_reason = timeout", "把结束方式告诉了模型"),
                ("[尚未作答]", "未作答的题如实标注"),
                (LABEL, "零作答超时的口径写进了提示词"),
                ("Q1: T27-T1 -> answered", "第 1 题状态=answered"),
                ("Q2: T27-T2 -> skipped", "第 2 题状态=skipped"),
                ("Q3: T27-T3 -> pending", "第 3 题状态=pending"),
                ("真正问过", "「全 0」规则已收窄到真正问过的题"),
            ]
            for needle, desc in checks:
                if needle in p:
                    ok(desc)
                else:
                    bad("提示词缺少：%s（%r）" % (desc, needle))

            pending_rule = _bullet(p, "- [尚未作答]")
            skipped_rule = _bullet(p, "- [跳过此题]")
            if pending_rule and "不得计入扣分" in pending_rule:
                ok("未及作答那条规则：不得计入扣分")
            else:
                bad("未及作答那条规则没有写「不得计入扣分」：%r" % pending_rule)
            if skipped_rule and "照常计分" in skipped_rule:
                ok("主动跳过那条规则：照常计分（与未作答相反）")
            else:
                bad("主动跳过那条规则没有写「照常计分」：%r" % skipped_rule)
            if "如果候选人对你提出的问题一个都没有给出有效回答" not in p:
                ok("旧的无条件「全 0」规则已经被删除")
            else:
                bad("提示词里还留着旧的无条件全 0 规则")

        # ---- E. 零作答超时的标注兜底 ---------------------------------------
        section("E. 零作答超时：服务端必须补上「%s」" % LABEL)
        st, third = start('["T27-Z1","T27-Z2","T27-Z3"]')
        sid = third["session_id"]                      # 一道都不答
        times_out(sid, "零作答超时")

        st, rep = report()
        rep = _as_dict(rep)
        say("  HTTP %s" % st)
        say("  details = %s" % ((rep.get("details") or "")[:120]))
        if st == 200 and LABEL in (rep.get("details") or ""):
            ok("报告里带上了「%s」（假 AI 故意没写，是服务端补的）" % LABEL)
        else:
            bad("零作答超时的报告没有标注 —— 这就是误导性 0 分")

        if rep.get("answered_count") == 0 and rep.get("total_questions") == 3:
            ok("计数取自服务端会话（answered=0 / total=3），不是模型编的")
        else:
            bad("计数不对：answered=%s total=%s（假 AI 编的是 7 / 99）"
                % (rep.get("answered_count"), rep.get("total_questions")))

        row = row_of(args.db, uid, sid)
        if row[3] and LABEL in row[3]:
            ok("标注也落库了（不是只在响应里）")
        else:
            bad("落库的报告里没有标注")

        # ---- F. 反向误导的防线 ---------------------------------------------
        section("F. 全部**主动跳过**后超时 → 不得标「未及作答，无法评分」")
        st, fourth = start('["T27-S1","T27-S2"]')
        sid = fourth["session_id"]
        skip_q()
        skip_q()
        times_out(sid, "跳过两题后超时")
        st, rep = report()
        rep = _as_dict(rep)
        say("  HTTP %s  details = %s" % (st, (rep.get("details") or "")[:100]))
        if st == 200 and LABEL not in (rep.get("details") or ""):
            ok("没有把「主动跳过」说成「没机会答」—— 反向误导的防线成立")
        else:
            bad("把全部主动跳过标成了未及作答 —— 这是反向误导")

        # ---- G. 不重复计费 --------------------------------------------------
        section("G. 已经出过报告再请求 → 409，且不再调 AI")
        calls_before = len(fake.report_prompts()) if fake else 0
        st, again = report()
        again = _as_dict(again)
        calls_after = len(fake.report_prompts()) if fake else 0
        if st == 409 and again.get("code") == "no_active_session":
            ok("409 no_active_session")
        else:
            bad("期望 409，实得 %s" % st)
        if calls_after == calls_before:
            ok("没有重复调用 AI（不重复计费，也不覆盖用户看过的结论）")
        else:
            bad("重复请求又调了一次 AI（%d -> %d）" % (calls_before, calls_after))

        # ---- H. 锁已释放 ----------------------------------------------------
        section("H. 锁已释放：能立刻开新一场面试")
        st, fifth = start('["T27-NEW"]')
        if st == 200:
            ok("新面试正常开始（超时出报告不会把用户锁在门外）")
        else:
            bad("开不了新面试：HTTP %s %s" % (st, fifth))

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
        say("❌ T-27 人工验收未通过（%d 项失败）" % len(FAILED))
        return 1
    if SKIPPED:
        for s in SKIPPED:
            say("  SKIP: %s" % s)
        say("")
        say("⚠️  T-27 人工验收通过，但有 %d 项未被验证；"
            "建议不带 --base 重跑一次以获得完整证据" % len(SKIPPED))
        return 0
    say("✅ T-27 人工验收通过：超时也能出报告且状态保持 abandoned、"
        "未及作答不计入扣分、零作答超时有明确标注、主动跳过不被误标")
    return 0


def _bullet(prompt, marker):
    """取出提示词里以 `marker` 开头的那一条规则（含其续行）。"""
    lines = prompt.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(marker):
            block = [ln]
            for nxt in lines[i + 1:]:
                if not nxt.startswith("  "):
                    break
                block.append(nxt)
            return "\n".join(block)
    return ""


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

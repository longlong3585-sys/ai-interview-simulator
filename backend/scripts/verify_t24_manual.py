"""T-24 人工验收：**傻瓜版**（自动签令牌，不用写任何 curl / 不用复制粘贴）。

## 它证明什么

  A. **会话可恢复**（"刷新页面进度不丢"的后端证据）
     start_interview 拿到 session_id
       -> GET /api/interview/session 拿回**同一个** session_id（前端刷新后就是这么恢复的）
       -> 并且**直接读数据库**比对：会话真的在磁盘上，不在进程内存里
  B. **可放弃、可重开**（ADR-022R：用户永远能开新面试）
     POST /api/interview/abandon -> GET 变成 null -> 立刻能 start 一场新的

默认用**临时用户**，结束时连会话一起删掉 —— 全程不碰你的真实账号
（T-09 事故后定下的硬规矩）。

## 用法

    cd backend
    # 终端 1
    .\\venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
    # 终端 2
    .\\venv\\Scripts\\python.exe scripts\\verify_t24_manual.py

想顺便证明"**服务重启**也不丢"，用你自己的账号在浏览器里开一场面试，然后：

    .\\venv\\Scripts\\python.exe scripts\\verify_t24_manual.py --check-own
    # 重启 uvicorn 之后再跑一次，两次都应当拿到**同一个** session_id

退出码：0 = 通过；1 = 未通过；2 = 环境问题。
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")
DEFAULT_BASE = "http://127.0.0.1:8000"

FAILED = []


def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say("  [PASS] %s" % msg)


def bad(msg):
    FAILED.append(msg)
    say("  [FAIL] %s" % msg)


def section(title):
    say("")
    say("=" * 68)
    say(title)
    say("=" * 68)


# ---------------------------------------------------------------------------
# HTTP（纯标准库）
# ---------------------------------------------------------------------------

def _request(method, url, token=None, body=None, form=None, timeout=60):
    data = None
    headers = {}
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


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 数据库（只读辅助）
# ---------------------------------------------------------------------------

def db_row(db_path, session_id):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        return conn.execute(
            "SELECT session_id, user_id, status, current_index, version, "
            "       ended_reason, expires_at FROM interview_sessions "
            "WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()


def make_probe_user(db_path):
    """建临时用户 + 签令牌。返回 (uid, username, token)。"""
    from auth import create_access_token, get_password_hash
    name = "t24_probe_%s" % uuid.uuid4().hex[:8]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (username, email, hashed_password, role, "
            "is_active, created_at) VALUES (?,?,?,?,1,?)",
            (name, name + "@probe.local", get_password_hash("Probe#12345"),
             "user", datetime.datetime.utcnow().isoformat()))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    finally:
        conn.close()
    token = create_access_token({"sub": name, "user_id": uid, "role": "user"})
    return uid, name, token


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


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="T-24 人工验收（自动签令牌）")
    ap.add_argument("--base", default=os.getenv("T24_BASE", DEFAULT_BASE))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check-own", action="store_true",
                    help="只读：查你自己的账号当前有没有活跃会话（用于重启前后对比）")
    ap.add_argument("--user", default="123", help="配合 --check-own 使用的账号")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    say("服务地址: %s" % base)
    say("数据库  : %s" % args.db)

    st, _ = _request("GET", base + "/openapi.json", timeout=10)
    if st != 200:
        bad("连不上服务（%s）。请先起：uvicorn main:app --host 0.0.0.0 --port 8000" % st)
        return 2

    if args.check_own:
        return _check_own(args, base)

    section("0. 环境")
    ok("服务可达（GET /openapi.json -> 200）")

    section("1. 造一个临时用户并签令牌（不碰你的真实账号）")
    uid, name, token = make_probe_user(args.db)
    say("  临时用户: id=%s username=%s" % (uid, name))
    ok("令牌已生成（%d 字符，脚本内部签的）" % len(token))

    try:
        # ---- A. 会话可恢复 -------------------------------------------------
        section("A. 会话可恢复（这就是『刷新不丢进度』的后端证据）")

        st, body = _request("GET", base + "/api/interview/session", token)
        if st == 200 and body.get("session") is None:
            ok("开始之前：GET session -> 200 + null（无会话是正常状态，不是错误）")
        else:
            bad("开始之前的 GET session 异常：HTTP %s %s" % (st, body))

        st, started = _request("POST", base + "/api/start_interview", token,
                               form={"role": "后端开发",
                                     "questions_json": '["题一", "题二", "题三"]'})
        if st != 200:
            bad("开始面试失败：HTTP %s %s" % (st, started))
            return 1
        sid = started["session_id"]
        say("  start_interview -> session_id=%s，共 %s 题"
            % (sid, started.get("total")))
        ok("面试已开始")

        # 这就是"前端刷新后"的那一次请求
        st, recovered = _request("GET", base + "/api/interview/session", token)
        session = (recovered or {}).get("session") or {}
        say("  GET /api/interview/session -> session_id=%s current_index=%s"
            % (session.get("session_id"), session.get("current_index")))
        if st == 200 and session.get("session_id") == sid:
            ok("拿回了**同一个** session_id —— 前端刷新后就是这么恢复的")
        else:
            bad("恢复失败：HTTP %s（拿到 %s，期望 %s）"
                % (st, session.get("session_id"), sid))
            return 1

        if session.get("total") == 3 and session.get("questions") == ["题一", "题二", "题三"]:
            ok("题目与题量都完整返回（前端可以直接重建界面）")
        else:
            bad("返回的题目不完整：%s" % session.get("questions"))

        if "user_id" not in session and "report" not in session:
            ok("响应不含 user_id / report（不泄露归属、不塞大字段）")
        else:
            bad("响应里出现了不该有的字段")

        row = db_row(args.db, sid)
        say("  直接读库: %s" % (row,))
        if row and row[0] == sid and row[2] == "active":
            ok("会话**确实在磁盘上**（不是进程内存）—— 所以重启/多 worker 也不会丢")
        else:
            bad("库里查不到这场会话：%s" % (row,))

        # ---- B. 可放弃、可重开 --------------------------------------------
        section("B. 可放弃、可重开（ADR-022R：用户永远能开新面试）")

        st, ab = _request("POST", base + "/api/interview/abandon", token, body={})
        say("  POST /api/interview/abandon -> HTTP %s %s" % (st, ab))
        if st == 200 and ab.get("status") == "abandoned":
            ok("已放弃（ended_reason=%s）" % ab.get("ended_reason"))
        else:
            bad("放弃失败：HTTP %s %s" % (st, ab))
            return 1

        st, after = _request("GET", base + "/api/interview/session", token)
        if st == 200 and (after or {}).get("session") is None:
            ok("放弃后 GET session -> null（不再算活跃）")
        else:
            bad("放弃后 GET session 仍返回会话：HTTP %s %s" % (st, after))

        row = db_row(args.db, sid)
        if row and row[2] == "abandoned" and row[5] == "manual":
            ok("库里是 abandoned + manual（**置终态而非删除**，内容留给历史/报告）")
        else:
            bad("库里的状态不对：%s" % (row,))

        st, again = _request("POST", base + "/api/start_interview", token,
                             form={"role": "前端开发",
                                   "questions_json": '["新题一"]'})
        if st == 200 and again.get("session_id") != sid:
            ok("**立刻能重开**一场新面试（修复前要等 2 小时 TTL）")
        else:
            bad("重开失败：HTTP %s %s" % (st, again))
            return 1

        st, empty = _request("POST", base + "/api/interview/abandon", token, body={})
        st2, _ = _request("POST", base + "/api/interview/abandon", token, body={})
        if st == 200 and st2 == 409:
            ok("没有会话时放弃 -> 409 + 指引（与 T-23 口径一致）")
        else:
            bad("无会话时放弃的状态码不对：第一次 %s，第二次 %s" % (st, st2))

    finally:
        drop_probe_user(args.db, uid)
        say("")
        say("  临时用户 %s 及其会话已删除（你的真实账号未被触碰）" % name)

    section("汇总")
    if FAILED:
        for f in FAILED:
            say("  FAIL: %s" % f)
        say("")
        say("❌ T-24 人工验收未通过（%d 项失败）" % len(FAILED))
        return 1
    say("✅ T-24 人工验收通过：会话可恢复（刷新不丢）、可放弃、可立刻重开")
    say("")
    say("  想再证明『服务重启也不丢』：")
    say("    1) 在浏览器里用你自己的账号开一场面试")
    say("    2) 跑一次:  python scripts/verify_t24_manual.py --check-own")
    say("    3) 重启 uvicorn，再跑一次同一条命令")
    say("    -> 两次应当拿到**同一个** session_id")
    return 0


def _check_own(args, base):
    """只读：查指定账号当前有没有活跃会话（重启前后对比用）。"""
    from auth import create_access_token
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    try:
        row = conn.execute("SELECT id, username, role, is_active FROM users "
                           "WHERE username = ?", (args.user,)).fetchone()
    finally:
        conn.close()
    if row is None:
        bad("库里没有用户 %r" % args.user)
        return 2
    uid, uname, urole, is_active = row
    if urole == "admin":
        bad("管理员不参与面试（/api/interview/session 会返回 403），请换普通账号")
        return 2
    token = create_access_token({"sub": uname, "user_id": uid, "role": urole})
    st, body = _request("GET", base + "/api/interview/session", token)
    if st != 200:
        bad("GET session -> HTTP %s %s" % (st, body))
        return 1
    session = (body or {}).get("session")
    section("账号 %s 的当前活跃会话（只读）" % uname)
    if session is None:
        say("  没有活跃会话（null）")
        say("")
        say("  若你刚重启过服务，而这里变成了 null —— 说明会话没落库，那才是丢进度。")
    else:
        say("  session_id      : %s" % session.get("session_id"))
        say("  进度            : %s / %s" % (session.get("current_index"),
                                             session.get("total")))
        say("  剩余有效期(秒)  : %s" % session.get("remaining_seconds"))
        say("  状态            : %s" % session.get("status"))
        say("")
        say("  把上面这行 session_id 与重启前对比 —— 相同即证明**重启不丢进度**。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

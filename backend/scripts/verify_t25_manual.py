"""T-25 人工验收：**傻瓜版**（自动签令牌，不用写 curl、不用复制粘贴）。

## 它证明什么

  A. **过期行自愈**（ADR-022 R-10，本任务要修的核心缺陷）
     造一条"已过期但 status 仍是 active"的会话 —— 它仍然占着
     `UNIQUE(user_id) WHERE status='active'` 索引。修复前会出现自相矛盾的一幕：

         GET  /api/interview/session    -> null        「你没有在面试」
         POST /api/start_interview      -> 409         「你已有进行中的面试」

     用户完全无从下手。现在 start_interview 会**先自愈再过检查**：
     过期行被置为 abandoned(timeout)，新会话顺利建出来。

  B. **409 携带会话摘要**（同一时间只能有一场，但前端要能立刻给出选择）
     真正有未过期会话时仍返回 409，但响应体里**直接带上会话摘要**
     （session_id / current_index / last_seq / total / remaining_seconds），
     前端无需再发一次 GET 就能显示"继续上次面试 / 放弃并重新开始"。

默认用**临时用户**，结束时连会话一起删掉 —— 不碰你的真实账号。

## 用法

    cd backend
    # 终端 1
    .\\venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
    # 终端 2
    .\\venv\\Scripts\\python.exe scripts\\verify_t25_manual.py

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


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def request(method, url, token=None, body=None, form=None, timeout=60):
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


# ---------------------------------------------------------------------------
# 临时用户 / 库操作
# ---------------------------------------------------------------------------

def make_probe_user(db_path):
    from auth import create_access_token, get_password_hash
    name = "t25_probe_%s" % uuid.uuid4().hex[:8]
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


def force_expire(db_path, session_id, seconds_ago=120):
    """把会话的 expires_at 改到过去，制造"过期但仍 active"的行。

    这是**数据状态**，不是任何接口能产生的 —— 正是自愈逻辑要清理的对象。
    """
    past = (datetime.datetime.utcnow()
            - datetime.timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE interview_sessions SET expires_at=? WHERE session_id=?",
                     (past, session_id))
        conn.commit()
    finally:
        conn.close()


def session_row(db_path, session_id):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        return conn.execute(
            "SELECT status, ended_reason, current_index FROM interview_sessions "
            "WHERE session_id=?", (session_id,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="T-25 人工验收（自动签令牌）")
    ap.add_argument("--base", default=os.getenv("T25_BASE", DEFAULT_BASE))
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    say("服务地址: %s" % base)
    say("数据库  : %s" % args.db)

    st, _ = request("GET", base + "/openapi.json", timeout=10)
    if st != 200:
        bad("连不上服务（%s）。请先起：uvicorn main:app --host 0.0.0.0 --port 8000" % st)
        return 2

    section("0. 环境")
    ok("服务可达")
    uid, name, token = make_probe_user(args.db)
    say("  临时用户: id=%s username=%s" % (uid, name))
    ok("令牌已生成（脚本内部签的）")

    def start(questions='["题一","题二"]'):
        return request("POST", base + "/api/start_interview", token,
                       form={"role": "后端开发", "questions_json": questions})

    try:
        # ---- A. 过期行自愈 ------------------------------------------------
        section("A. 过期行自愈（ADR-022 R-10 —— 本任务要修的核心缺陷）")

        st, first = start()
        if st != 200:
            bad("首次开始面试失败：HTTP %s %s" % (st, first))
            return 1
        old_sid = first["session_id"]
        say("  第一次 start -> session_id=%s" % old_sid)

        # 造出"已过期但 status 仍是 active"的行（惰性判定的时序缺口）
        force_expire(args.db, old_sid)
        row = session_row(args.db, old_sid)
        say("  已把它的 expires_at 改到 2 分钟前；库里状态: status=%s reason=%s"
            % (row[0], row[1]))
        if row[0] == "active":
            ok("已造出『过期但仍 active』的行（这正是占着唯一锁的那种）")
        else:
            bad("构造失败：状态不是 active（%s）" % (row,))
            return 1

        st, body = request("GET", base + "/api/interview/session", token)
        if st == 200 and (body or {}).get("session") is None:
            ok("GET session -> null（用户看到『我没有在面试』）")
        else:
            bad("GET session 不是 null：HTTP %s %s" % (st, body))

        say("  ---- 修复前：下面这一步会 409，于是用户看到『说我有面试、又说我没有任何面试』----")
        st, second = start(questions='["新题"]')
        if st == 200:
            ok("POST start_interview -> 200：过期行被自愈，新会话建出来了")
        else:
            bad("POST start_interview -> HTTP %s %s（过期行没有被自愈）" % (st, second))
            return 1

        new_sid = second["session_id"]
        say("  新 session_id=%s（与旧的 %s 不同）" % (new_sid, old_sid[:12] + "…"))
        if new_sid != old_sid:
            ok("确实是一个**新**会话，没有覆盖也没有被卡住")
        else:
            bad("返回的是同一个 session_id")

        row = session_row(args.db, old_sid)
        say("  旧会话现在的库里状态: status=%s ended_reason=%s" % (row[0], row[1]))
        if row[0] == "abandoned" and row[1] == "timeout":
            ok("自愈语义正确：abandoned + timeout（与超时兜底同口径）")
        else:
            bad("自愈后的状态不对：%s" % (row,))

        # ---- B. 409 携带会话摘要 -----------------------------------------
        section("B. 真正冲突时：409 直接携带会话摘要（前端无需再往返一次）")

        st, conflict = start(questions='["第三场"]')
        say("  已有**未过期**会话时再 start -> HTTP %s" % st)
        if st != 409:
            bad("期望 409，实得 %s：%s" % (st, conflict))
            return 1
        ok("被正确拒绝（同一时间只能有一场未过期的面试）")

        s = (conflict or {}).get("session")
        say("  409 响应体: %s" % json.dumps(conflict, ensure_ascii=False)[:260])
        if s and s.get("session_id") == new_sid:
            ok("409 里带着会话摘要（session_id 与当前会话一致）")
        else:
            bad("409 里没有可用的会话摘要：%s" % s)

        if s and all(k in s for k in ("current_index", "last_seq", "total",
                                      "remaining_seconds")):
            ok("摘要含 current_index / last_seq / total / remaining_seconds")
        else:
            bad("摘要字段不全：%s" % (list(s) if s else None))

        if s and "user_id" not in s and "report" not in s:
            ok("摘要不泄露 user_id / report")
        else:
            bad("摘要里有不该有的字段")

        actions = (conflict or {}).get("actions") or []
        if "get_session" in actions and "abandon" in actions:
            ok("actions 指向 T-24 的两个恢复接口：%s" % actions)
        else:
            bad("actions 不完整：%s" % actions)

        if isinstance((conflict or {}).get("detail"), str):
            ok("detail 是字符串（前端直接渲染，不会变成 [object Object]）")
        else:
            bad("detail 不是字符串")

        # ---- C. 对照：未过期会话不能被误自愈 ------------------------------
        section("C. 对照组：未过期的会话**不能**被自愈掉（否则等于静默覆盖）")

        row = session_row(args.db, new_sid)
        if row[0] == "active":
            ok("冲突之后，当前会话仍是 active（没有被误置为 abandoned）")
        else:
            bad("未过期的会话被误改了：%s" % (row,))

        st, still = request("GET", base + "/api/interview/session", token)
        if st == 200 and ((still or {}).get("session") or {}).get("session_id") == new_sid:
            ok("GET session 仍返回同一场（进度没丢）")
        else:
            bad("GET session 与预期不符：HTTP %s %s" % (st, still))

    finally:
        drop_probe_user(args.db, uid)
        say("")
        say("  临时用户 %s 及其会话已删除（你的真实账号未被触碰）" % name)

    section("汇总")
    if FAILED:
        for f in FAILED:
            say("  FAIL: %s" % f)
        say("")
        say("❌ T-25 人工验收未通过（%d 项失败）" % len(FAILED))
        return 1
    say("✅ T-25 人工验收通过：过期行自愈、409 携带会话摘要、未过期会话不受影响")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

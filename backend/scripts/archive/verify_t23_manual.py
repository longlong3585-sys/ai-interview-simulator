"""T-23 人工验收：**傻瓜版**（自动签令牌，不需要你复制粘贴任何东西）。

## 它验什么

  POST /api/chat  {message: "hello"}   在没有进行中的会话时
    -> 必须返回 **409**，且响应体里带 `no_active_session`

这是 T-23 的核心行为：修复前该接口在无会话时会**静默走通用 AI 对话分支**，
任何登录用户不 start_interview 就能把 `/api/chat` 当免费的 DeepSeek 代理用
（Bug 1 里"白嫖额度"的另一条路径）。

## 为什么需要这个脚本

PowerShell 里手写 `curl.exe -H "Authorization: Bearer <token>"` 有两个坑：
  1. 引号地狱（本项目日志里已记录过多次，内联命令的引号会被 PS 吃掉）；
  2. 从浏览器复制 token 时容易带上换行/空格 -> JWT 签名校验失败 -> 401。
本脚本用应用自己的 `create_access_token` 现签一个令牌，
**不经过剪贴板**，因此这两个坑都不存在。

## 用法

    cd backend
    # 终端 1（先起服务）
    .\\venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
    # 终端 2
    .\\venv\\Scripts\\python.exe scripts\\verify_t23_manual.py
    .\\venv\\Scripts\\python.exe scripts\\verify_t23_manual.py --full   # 额外验完整链路

默认模式**只读**：不创建、不修改、不删除任何数据。
`--full` 会**新建一个临时用户**（`t23_probe_*`）跑完整链路，结束时把它连同
它的会话一起删掉 —— 全程不碰你的真实账号（T-09 事故后定下的硬规矩）。

退出码：0 = 通过；1 = 未通过；2 = 环境问题（服务没起、库里没有该用户等）。
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

import os as _archive_os

# 归档位置：backend/scripts/archive/<本文件> —— 四层 dirname 即仓库根。
_ARCHIVE_REPO = _archive_os.path.dirname(_archive_os.path.dirname(
    _archive_os.path.dirname(_archive_os.path.dirname(
        _archive_os.path.abspath(__file__)))))
BACKEND_DIR = os.path.join(_ARCHIVE_REPO, "backend")
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)                      # 让相对路径的 DATABASE_URL 也指向这里

DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")
DEFAULT_BASE = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say("  [PASS] %s" % msg)


def bad(msg):
    say("  [FAIL] %s" % msg)


def read_user(db_path, username):
    """**只读**取用户；返回 (id, username, role, is_active) 或 None。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        row = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        conn.close()


def active_session_of(db_path, user_id):
    """**只读**看该用户有没有 active 且未过期的会话。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        return conn.execute(
            "SELECT session_id, current_index, version, status, expires_at "
            "FROM interview_sessions "
            "WHERE user_id = ? AND status = 'active' AND expires_at > ?",
            (user_id, __import__("datetime").datetime.utcnow().isoformat()),
        ).fetchone()
    finally:
        conn.close()


def mint_token(user_id, username, role):
    """用**应用自己的**签发逻辑造令牌 —— 与运行中的服务同源（同 SECRET_KEY/算法）。"""
    from auth import create_access_token
    return create_access_token({"sub": username, "user_id": user_id, "role": role})


def post(base, path, token=None, payload=None):
    """JSON POST。返回 (status, body_text)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post_form(base, path, token, fields):
    """**表单** POST —— `/api/start_interview` 用的是 `Form(...)` 而不是 JSON body。

    首版这里传了 JSON，服务返回 422。这个坑正是本脚本要替你挡掉的那一类。
    """
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="T-23 人工验收（自动签令牌）")
    ap.add_argument("--base", default=os.getenv("T23_BASE", DEFAULT_BASE),
                    help="服务地址（默认 %s）" % DEFAULT_BASE)
    ap.add_argument("--db", default=DEFAULT_DB, help="真库路径（**只读**访问）")
    ap.add_argument("--user", default="123", help="用哪个已有账号（默认 123）")
    ap.add_argument("--full", action="store_true",
                    help="额外用**临时用户**验完整链路（start/二次 start/chat）")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    say("服务地址: %s" % base)
    say("数据库  : %s（只读）" % args.db)
    say("")

    # ---- 0. 服务是否可达 -------------------------------------------------
    say("=" * 66)
    say("0. 服务可达性")
    say("=" * 66)
    try:
        with urllib.request.urlopen(base + "/openapi.json", timeout=10) as resp:
            paths = len(json.loads(resp.read().decode("utf-8")).get("paths", {}))
        ok("GET /openapi.json -> 200（%d 个路径）" % paths)
    except Exception as exc:  # noqa: BLE001
        bad("连不上服务：%s: %s" % (type(exc).__name__, exc))
        say("")
        say("  请先在另一个终端起服务：")
        say("    cd backend")
        say("    .\\venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000")
        return 2

    # ---- 1. 取用户 + 签令牌（不经过剪贴板）--------------------------------
    say("")
    say("=" * 66)
    say("1. 自动签令牌（用应用自己的签发逻辑，无需复制粘贴）")
    say("=" * 66)

    if args.full:
        user = _ensure_probe_user(args.db)
        if user is None:
            return 2
        uid, uname, urole, cleanup = user
    else:
        row = read_user(args.db, args.user)
        if row is None:
            bad("库里没有用户 %r —— 用 --user 指定一个已存在的普通账号" % args.user)
            return 2
        uid, uname, urole, is_active = row
        cleanup = None
        if urole == "admin":
            bad("用户 %r 是管理员；/api/chat 对管理员返回 403，请换普通账号" % uname)
            return 2
        if not is_active:
            bad("用户 %r 已被禁用；请换一个启用的账号" % uname)
            return 2

    say("  用户: id=%s username=%s role=%s" % (uid, uname, urole))
    token = mint_token(uid, uname, urole)
    say("  令牌: %s...（%d 字符，脚本内部生成）" % (token[:24], len(token)))
    ok("令牌已生成")

    try:
        # ---- 2. 核心断言：无会话 -> 409 ----------------------------------
        say("")
        say("=" * 66)
        say("2. 核心断言：无进行中的会话时 POST /api/chat -> 409")
        say("=" * 66)

        existing = active_session_of(args.db, uid)
        if existing is not None and not args.full:
            say("  ⚠️ 该用户**已有**进行中的会话：%s" % (existing,))
            say("     本验收要求『无会话』，因此这一项无法判定为 409。")
            say("     两个选择：① 先把那场面试结束掉；② 用 --full（临时用户，不碰真实账号）。")
            return 2

        status, body = post(base, "/api/chat", token, {"message": "hello"})
        say("  POST /api/chat  ->  HTTP %s" % status)
        say("  响应体: %s" % (body[:300] + ("..." if len(body) > 300 else "")))
        say("")

        passed = True
        if status == 409:
            ok("状态码是 409")
        else:
            bad("状态码是 %s，期望 409" % status)
            if status == 401:
                say("     401 = 令牌没被接受（T-07 之后 SECRET_KEY 必须与服务的 .env 一致）")
            elif status == 403:
                say("     403 = 该账号是管理员；/api/chat 对管理员返回 403")
            elif status == 200:
                say("     200 = 服务落到了旧行为（无会话也能聊）—— 说明跑的不是本次改动后的代码，")
                say("          或端口上是个残留的旧进程（先用 netstat 确认监听 PID 的启动时间）")
            passed = False

        if "no_active_session" in body:
            ok("响应体包含 no_active_session")
        else:
            bad("响应体里没有 no_active_session")
            passed = False

        if not passed:
            say("")
            say("❌ T-23 人工验收未通过")
            return 1

        # ---- 3.（可选）完整链路：临时用户 --------------------------------
        if args.full:
            say("")
            say("=" * 66)
            say("3. 完整链路（**临时用户**，结束时连会话一起删除）")
            say("=" * 66)

            st, bd = post_form(base, "/api/start_interview", token,
                               {"role": "后端开发",
                                "questions_json": '["探针题 1", "探针题 2"]'})
            say("  开始面试 -> HTTP %s" % st)
            if st == 200:
                ok("首次开始面试成功")
                sid = (json.loads(bd) or {}).get("session_id")
                say("     session_id=%s（写入数据库，可在库里查到）" % sid)
            else:
                bad("首次开始面试失败：%s" % bd[:200])
                return 1

            st2, bd2 = post_form(base, "/api/start_interview", token,
                                 {"role": "前端开发",
                                  "questions_json": '["另一场题 1"]'})
            say("  再开一场   -> HTTP %s" % st2)
            if st2 == 409 and "active_session_exists" in bd2:
                ok("同一用户第二场被拒（409 active_session_exists，不再静默覆盖）")
            else:
                bad("第二场没有被拒：HTTP %s %s" % (st2, bd2[:200]))
                return 1

            st3, bd3 = post(base, "/api/chat", token, {"message": "我的回答"})
            say("  有会话时聊天 -> HTTP %s" % st3)
            if st3 == 200:
                ok("有会话时 /api/chat 正常（对照项：改完没把正常路径弄坏）")
            else:
                bad("有会话时聊天失败：HTTP %s %s" % (st3, bd3[:200]))
                return 1

        say("")
        say("=" * 66)
        say("✅ T-23 人工验收通过：无会话返回 409")
        say("=" * 66)
        return 0
    finally:
        if cleanup is not None:
            cleanup()


# ---------------------------------------------------------------------------
# --full：临时用户（不碰真实账号）
# ---------------------------------------------------------------------------

def _ensure_probe_user(db_path):
    """建一个临时普通用户，返回 (id, username, role, cleanup_fn)。

    直接写库（不经服务）—— 服务没有注册之外的创建用户接口，而注册要过验证码。
    """
    import datetime
    import uuid

    from auth import get_password_hash

    name = "t23_probe_%s" % uuid.uuid4().hex[:8]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (username, email, hashed_password, role, "
            "is_active, created_at) VALUES (?,?,?,?,1,?)",
            (name, name + "@probe.local", get_password_hash("Probe#12345"),
             "user", datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        bad("创建临时用户失败：%s" % exc)
        return None
    finally:
        conn.close()

    say("  已创建临时用户: id=%s username=%s（结束时删除）" % (uid, name))

    def cleanup():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM interview_sessions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM interview_records WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM auth_attempts WHERE ip LIKE 't23_probe%'")
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        say("")
        say("  临时用户 %s 及其会话已删除（你的真实账号未被触碰）" % name)

    return uid, name, "user", cleanup


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

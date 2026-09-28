"""T-15 真机冒烟：只读地走一遍 HTTP -> FastAPI -> SQLAlchemy engine -> SQLite 全链路。

本脚本**只发 GET 请求**（代码里强制断言），不创建/修改/删除任何数据，
可以安全地对真库运行 —— 但**前提是后端已经在跑**。

为什么需要自签 token：登录接口（`POST /api/login`）要求图形验证码，
脚本无法识别验证码图片。因此这里用 `auth.create_access_token` 直接造一个
管理员令牌 —— **它只读 SECRET_KEY、不碰数据库**。

同时做一次**跨层一致性核对**：接口返回的统计数字必须与直接用 sqlite3
只读查出来的行数一致。这能同时证明"HTTP 层没坏"和"engine 读写正常"。

用法
----
    cd backend
    # 终端 1：启动服务
    .\\venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
    # 终端 2：
    .\\venv\\Scripts\\python.exe scripts\\smoke_t15.py
    .\\venv\\Scripts\\python.exe scripts\\smoke_t15.py --base http://127.0.0.1:8011
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + detail) if detail else ""))
    return bool(ok)


def call(base, path, token=None):
    """只读 HTTP GET。非 GET 一律拒绝 —— 本脚本不允许改数据。"""
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            payload = None
            if "json" in ctype:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    payload = None
            return resp.status, payload, resp.headers.get("X-Captcha-Id"), ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, None, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return None, None, None, "%s: %s" % (type(exc).__name__, exc)


def main():
    ap = argparse.ArgumentParser(description="T-15 真机只读冒烟")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--db", default=DEFAULT_DB, help="用于跨层核对的库（只读打开）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("后端: %s" % base)
    print("")

    # --- 0. 服务在线 ---
    print("=" * 72)
    print("0. 服务可达性")
    print("=" * 72)
    code, payload, _, err = call(base, "/openapi.json")
    if code != 200:
        print("  [FAIL] GET /openapi.json -> %s  %s" % (code, err))
        print("")
        print("  服务没起来。请先确认后端已启动、且端口与 --base 一致。")
        return 1
    check("GET /openapi.json -> 200", True, "共 %d 个路径" % len(payload.get("paths", {})))

    # --- 1. 公开接口 ---
    print("")
    print("=" * 72)
    print("1. 公开接口（无需令牌）")
    print("=" * 72)
    code, _, cid, err = call(base, "/api/captcha")
    check("GET /api/captcha -> 200 且带 X-Captcha-Id", code == 200 and bool(cid),
          "code=%s captcha_id=%s" % (code, (cid or "")[:12]))

    # --- 2. 鉴权门禁（这是阶段 1 的成果，顺手回归） ---
    print("")
    print("=" * 72)
    print("2. 未授权访问必须 401")
    print("=" * 72)
    for path in ("/api/user/profile", "/api/history", "/api/user/stats", "/api/interview/config"):
        code, _, _, err = call(base, path)
        check("GET %-26s -> 401" % path, code == 401, "code=%s" % code)

    # --- 3. 授权只读（真实 DB 路径） ---
    print("")
    print("=" * 72)
    print("3. 授权只读访问（自签管理员令牌）")
    print("=" * 72)

    # 用**只读**连接取管理员身份；故意不通过 app 的 SessionLocal，
    # 避免为了冒烟而在真库上开启 WAL（journal_mode 变更是有副作用的）。
    if not os.path.exists(args.db):
        check("库文件存在", False, args.db)
        return 1
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    try:
        row = conn.execute(
            "select id, username, role from users where role='admin' order by id limit 1"
        ).fetchone()
        db_counts = {
            "users": conn.execute("select count(*) from users").fetchone()[0],
            "records": conn.execute("select count(*) from interview_records").fetchone()[0],
        }
    finally:
        conn.close()

    if row is None:
        check("库中存在管理员账号", False, "库里没有 role='admin' 的用户")
        return 1
    uid, uname, urole = row
    check("库中存在管理员账号", True, "id=%s username=%s" % (uid, uname))

    from auth import create_access_token  # noqa: E402  仅读 SECRET_KEY，不碰数据库

    token = create_access_token({"sub": uname, "user_id": uid, "role": urole})

    code, payload, _, err = call(base, "/api/user/profile", token)
    ok = code == 200 and isinstance(payload, dict) and payload.get("username") == uname
    check("GET /api/user/profile -> 200 且 username 匹配", ok,
          "code=%s username=%s" % (code, (payload or {}).get("username")))

    code, payload, _, err = call(base, "/api/user/stats", token)
    check("GET /api/user/stats -> 200", code == 200, "code=%s" % code)

    code, payload, _, err = call(base, "/api/admin/stats", token)
    ok = code == 200 and isinstance(payload, dict)
    check("GET /api/admin/stats -> 200", ok, "code=%s" % code)
    if ok:
        print("       total_users=%s total_interviews=%s" % (
            payload.get("total_users"), payload.get("total_interviews")))

    code, payload, _, err = call(base, "/api/admin/users", token)
    check("GET /api/admin/users -> 200 列表", code == 200 and isinstance(payload, list),
          "code=%s 条数=%s" % (code, len(payload) if isinstance(payload, list) else "?"))

    # --- 4. 跨层一致性（HTTP 数字 == 直接读库数字） ---
    print("")
    print("=" * 72)
    print("4. 跨层一致性核对（接口数字 必须等于 直接读库数字）")
    print("=" * 72)
    code, payload, _, err = call(base, "/api/admin/stats", token)
    if code == 200 and isinstance(payload, dict) and "total_interviews" in payload:
        check(
            "接口 total_interviews == 库中 interview_records 行数",
            payload["total_interviews"] == db_counts["records"],
            "接口=%s 库=%s" % (payload["total_interviews"], db_counts["records"]),
        )
    else:
        check("接口 total_interviews == 库中 interview_records 行数", False,
              "取不到 total_interviews")

    # 管理员不能进行面试 —— 这是**有意的业务规则**，顺手确认路由与权限仍在工作
    code, _, _, err = call(base, "/api/history", token)
    check("GET /api/history（管理员）-> 403 管理员不能进行面试", code == 403,
          "code=%s %s" % (code, err))

    # --- 汇总 ---
    print("")
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    failed = [r for r in RESULTS if not r[0]]
    print("  共 %d 项，通过 %d 项，失败 %d 项" % (
        len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    print("")
    print("  说明: 本脚本只发 GET，未修改任何数据。")
    print("        库中现有 users=%d records=%d（仅供参考，应与你的实际数据一致）"
          % (db_counts["users"], db_counts["records"]))
    print("")
    print("  结论: %s" % ("全链路正常 [ALL PASS]" if not failed else "存在失败项 [FAILED]"))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

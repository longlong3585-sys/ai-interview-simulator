#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-51 人工验收：`jti` 声明 + `token_blacklist` **真登出**（ADR-003 选 B）。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t51_manual.py

可选参数：
    --skip-full-tests  跳过 A-② 的全量后端测试（默认会跑，597 项约 80 秒）
    --skip-frontend    跳过 D 段（前端契约测试 + tsc 检查）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 5 的 T-51）：

  * **登出后旧 token → 401**；
  * **黑名单 TTL ≥ 8h**（不早于续期链失效，否则令牌复活）。

四段互不依赖的取证：

  0. 预检      —— python / 目标文件 / node
  A. 后端测试  —— ① 本任务的 34 项测试全绿；② **全量 597 项**全绿
                  （登出与鉴权挂在**所有**受保护请求上，写错会打坏一大片既有用例）
  B. 语法检查  —— 本次改动的 8 个文件 `py_compile` 通过
  C. 独立复核  —— **本脚本自己重跑一遍关键语义**（不用测试里的断言）：
                  ① TTL 恰好 8h 且等于 T-50 的绝对上限；② 令牌快到期/已过期/无 exp
                     三种情形都按 8h 留痕；③ 写入幂等；④ 过期记录不算已吊销；
                     ⑤ 清理只删已过期（删仍生效的 = 吊销复活，最危险的一行）；
                     ⑥ 端到端：登出前可用 → 登出 → 所有受保护端点 401、
                     续期后的令牌也被吊销（jti 沿用）、别人的会话不受影响、
                     过期令牌登出仍成功；
                  ⑦ 前端 `signOut` 真的调 `/api/logout` 且**顺序正确**
  D. 前端契约  —— `node --test` 全绿 + `tsc -b` exit 0

为什么 C-⑥ 能离线做端到端：`TestClient(main.app)` 在**同进程内**发请求，
不起真实端口、不需要 Token、也不受"服务没起来"干扰；令牌由应用自己的
`create_access_token` 现签（同密钥、同算法）。

已知边界（刻意如此，非遗漏）：
  * **不驱动真实浏览器**。前端"登出时先调服务端再清本地"由 D 段的契约测试
    （源码顺序断言 + 判别力自检）证明；肉眼验收见 `docs/37-manual-verification.md` §4。
  * **不涉多 worker 一致性**：本项目 SQLite + 同库多 worker 下黑名单天然一致
    （落在共享库里）。若将来换进程内存储，这条要重新验。
  * **`npm run lint` 本来就是红的**：本任务只核对"没有新增结构性违规"。
"""

from __future__ import annotations

import argparse
import os
import py_compile
import re
import subprocess
import sys

import os as _archive_os

# 归档位置：backend/scripts/archive/<本文件> —— 四层 dirname 即仓库根。
_ARCHIVE_REPO = _archive_os.path.dirname(_archive_os.path.dirname(
    _archive_os.path.dirname(_archive_os.path.dirname(
        _archive_os.path.abspath(__file__)))))
REPO_DIR = _ARCHIVE_REPO
BACKEND_DIR = os.path.join(REPO_DIR, "backend")
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
VENV_PYTHON = os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe")

BACKEND_FILES = {
    "吊销纯逻辑": os.path.join("utils", "token_revocation.py"),
    "黑名单存储": os.path.join("services", "stores", "sqlite_token_blacklist_store.py"),
    "存储协议": os.path.join("services", "stores", "base.py"),
    "存储装配": os.path.join("services", "stores", "factory.py"),
    "鉴权校验": "auth.py",
    "登出端点": os.path.join("routers", "auth_router.py"),
    "续期中间件": os.path.join("utils", "token_renewal_middleware.py"),
    "清理脚本": os.path.join("scripts", "cleanup.py"),
    "后端测试": os.path.join("tests", "test_token_revocation.py"),
}
FRONTEND_FILES = {
    "认证真源": os.path.join("src", "auth", "AuthContext.tsx"),
    "前端契约测试": os.path.join("tests", "auth-persistence.test.mjs"),
}

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def say(msg=""):
    print(msg)


def section(title):
    say("")
    say("=" * 78)
    say(title)
    say("=" * 78)


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


def run(cmd, cwd=BACKEND_DIR, timeout=1800):
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError as exc:
        return None, "命令不存在：%s" % exc
    except subprocess.TimeoutExpired:
        return None, "超时（%ss）" % timeout
    except OSError as exc:
        return None, "无法启动进程：%s" % exc
    return proc.returncode, (proc.stdout.decode("utf-8", errors="replace") if proc.stdout else "")


def read(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def tail(text, n=5):
    return [l.rstrip() for l in text.splitlines() if l.strip()][-n:]


# ---------------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------------

def segment_preflight():
    section("0. 预检（python / 目标文件 / node）")

    missing = [rel for rel in BACKEND_FILES.values()
               if not os.path.exists(os.path.join(BACKEND_DIR, rel))]
    if missing:
        bad("后端交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个后端目标文件就在位（纯逻辑 / 存储 / 协议 / 装配 / 鉴权 / 登出 / 中间件 / 清理 / 测试）"
           % len(BACKEND_FILES))

    missing = [rel for rel in FRONTEND_FILES.values()
               if not os.path.exists(os.path.join(FRONTEND_DIR, rel))]
    if missing:
        bad("前端交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个前端目标文件就在位（认证真源 / 契约测试）" % len(FRONTEND_FILES))

    if not os.path.exists(VENV_PYTHON):
        env_problem("找不到 backend/venv/Scripts/python.exe")

    node_path = None
    for name in ("node", "node.exe"):
        code, out = run([name, "--version"], cwd=REPO_DIR, timeout=60)
        if code == 0:
            node_path = name
            ok("node 可执行：%s（%s）" % (name, out.strip()))
            break
    if not node_path:
        env_problem("找不到 node —— D 段会跳过")
    return node_path


# ---------------------------------------------------------------------------
# A. 后端测试
# ---------------------------------------------------------------------------

def segment_backend_tests(args):
    section("A. 后端测试（stdlib unittest）")

    code, out = run([VENV_PYTHON, "-m", "unittest", "tests.test_token_revocation", "-q"])
    if code is None:
        env_problem("T-51 单元测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
    else:
        say("  —— `tests.test_token_revocation` 尾部 5 行 ——")
        for line in tail(out):
            say("     %s" % line.encode("ascii", "replace").decode("ascii"))
        if code == 0 and re.search(r"^OK", out, re.M):
            ok("T-51 的 34 项测试全绿（TTL / 幂等 / 过期过滤 / 登出后 401 / jti 沿用 / 清理）")
        else:
            bad("T-51 单元测试未全绿（exit=%s）" % code)
        SEG["A"] = (code == 0)

    if args.skip_full_tests:
        skip("--skip-full-tests：跳过后端全量测试")
        return

    code, out = run([VENV_PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"])
    if code is None:
        env_problem("全量后端测试无法执行：%s" % out.strip()[:200])
        SEG["A2"] = None
        return
    say("  —— 全量后端测试尾部 5 行 ——")
    for line in tail(out):
        say("     %s" % line.encode("ascii", "replace").decode("ascii"))
    m = re.search(r"^Ran (\d+) tests", out, re.M)
    ran = int(m.group(1)) if m else 0
    if code == 0 and re.search(r"^OK", out, re.M):
        ok("全量后端测试通过（Ran %d tests OK）—— 登出/鉴权改动没有打坏既有链路" % ran)
    else:
        bad("全量后端测试未通过（exit=%s，Ran %s）" % (code, ran or "?"))
    SEG["A2"] = (code == 0)


# ---------------------------------------------------------------------------
# B. 语法检查
# ---------------------------------------------------------------------------

def segment_syntax():
    section("B. 语法检查（py_compile）")
    problems = []
    cfile = os.path.join(BACKEND_DIR, "_t51_compile_check.pyc")
    try:
        for label, rel in BACKEND_FILES.items():
            try:
                py_compile.compile(os.path.join(BACKEND_DIR, rel), cfile=cfile, doraise=True)
            except Exception as exc:  # noqa: BLE001
                problems.append("%s（%s）：%s" % (label, rel, str(exc)[:120]))
    finally:
        if os.path.exists(cfile):
            os.remove(cfile)
    if problems:
        bad("语法检查失败：%s" % "；".join(problems))
    else:
        ok("%d 个改动文件全部通过 py_compile" % len(BACKEND_FILES))
    SEG["B"] = not problems


# ---------------------------------------------------------------------------
# C. 独立复核
# ---------------------------------------------------------------------------

PROBE = r'''
import json
from jose import jwt
from config import SECRET_KEY, ALGORITHM

from utils.token_revocation import (
    BLACKLIST_MIN_TTL_SECONDS, blacklist_covers_token, blacklist_expiry,
    iso_after_seconds, iso_now, revocation_target,
)
from utils.token_renewal import ABSOLUTE_MAX_LIFETIME_SECONDS
from services.stores.sqlite_token_blacklist_store import SQLiteTokenBlacklistStore

out = {}
NOW = 1700000000
H = 3600

# ① TTL 恰好 8h，且等于 T-50 的绝对上限
out["ttl"] = BLACKLIST_MIN_TTL_SECONDS
out["ttl_equals_absolute"] = (BLACKLIST_MIN_TTL_SECONDS == ABSOLUTE_MAX_LIFETIME_SECONDS)

def exp_for(exp_offset, jti="j"):
    payload = {"jti": jti, "exp": NOW + exp_offset}
    return blacklist_expiry(payload, NOW), payload

floor = iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, iso_now(NOW))
out["exp_short"], p_short = exp_for(5 * 60)
out["exp_expired"], p_expired = exp_for(-H)
out["exp_none"] = blacklist_expiry({"jti": "j"}, NOW)
out["exp_long"], p_long = exp_for(20 * H)
out["all_min_ttl"] = all(v == floor for v in
                         [out["exp_short"], out["exp_expired"], out["exp_none"], out["exp_long"]])
out["covers"] = blacklist_covers_token(floor, p_short, NOW)
out["covers_rejects_short"] = blacklist_covers_token(
    iso_after_seconds(60, NOW), p_short, NOW)
out["target_missing_jti"] = revocation_target({})
out["target_int_jti"] = revocation_target({"jti": 7})

# ③/④/⑤ 存储：幂等、过期过滤、清理只删已过期
store = SQLiteTokenBlacklistStore()

def wipe():
    from sqlalchemy import text
    import database
    with database.engine.begin() as conn:
        conn.execute(text("DELETE FROM token_blacklist"))

wipe()
store.revoke("probe-a", iso_after_seconds(8 * H))
store.revoke("probe-a", iso_after_seconds(8 * H))      # 幂等
out["idempotent_count"] = store.count_all()
store.revoke("probe-expired", iso_after_seconds(-60))
out["live_revoked"] = store.is_revoked("probe-a", iso_now())
out["expired_not_revoked"] = store.is_revoked("probe-expired", iso_now())
removed = store.purge_expired(iso_now())
left = store.count_all()
out["purged"] = removed
out["left"] = left
out["live_survives_purge"] = store.is_revoked("probe-a", iso_now())

# ⑥ 端到端
from fastapi.testclient import TestClient
import main
from tests.support import create_test_user, delete_users, mint_renewable_token

wipe()
out["middleware"] = [m.cls.__name__ for m in main.app.user_middleware]
cors = [m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
out["exposed"] = list((cors[0].kwargs.get("expose_headers") or [])) if cors else []
out["registry"] = sorted(__import__("services.stores.factory", fromlist=["x"])._REGISTRY.keys())

client = TestClient(main.app)
client.__enter__()
try:
    create_test_user("t51_probe", role="user")
    create_test_user("t51_probe_admin", role="admin")
    token = mint_renewable_token("t51_probe", minutes=30, age_minutes=1, user_id=1, jti="probe-session")
    other = mint_renewable_token("t51_probe_admin", role="admin", minutes=30, age_minutes=1,
                                 user_id=2, jti="probe-other")
    h = {"Authorization": "Bearer " + token}

    out["before_logout"] = [client.get(p, headers=h).status_code for p in
                            ("/api/interview/config", "/api/user/profile")]
    r = client.post("/api/logout", headers=h)
    out["logout_status"] = r.status_code
    out["logout_revoked"] = bool(r.json().get("revoked"))
    out["after_logout"] = [client.get(p, headers=h).status_code for p in
                           ("/api/interview/config", "/api/user/profile")]
    out["revoked_header"] = client.get("/api/interview/config", headers=h).headers.get("X-Token-Revoked")
    out["other_user_ok"] = client.get("/api/admin/stats",
                                      headers={"Authorization": "Bearer " + other}).status_code
    # 续期后的令牌也必须被吊销（jti 沿用）
    wipe()
    old = mint_renewable_token("t51_probe", minutes=30, age_minutes=21, user_id=1, jti="probe-chain")
    renewed = client.get("/api/interview/config",
                         headers={"Authorization": "Bearer " + old}).headers.get("X-Refreshed-Token")
    out["renewed_present"] = bool(renewed)
    if renewed:
        client.post("/api/logout", headers={"Authorization": "Bearer " + old})
        out["renewed_after_logout"] = client.get(
            "/api/interview/config", headers={"Authorization": "Bearer " + renewed}).status_code
    # 过期令牌登出仍成功
    expired = mint_renewable_token("t51_probe", minutes=30, age_minutes=40, user_id=1)
    out["logout_expired_status"] = client.post(
        "/api/logout", headers={"Authorization": "Bearer " + expired}).status_code
    # 无 jti 的老令牌
    legacy = mint_renewable_token("t51_probe", minutes=30, age_minutes=1, user_id=1)
    payload = jwt.decode(legacy, SECRET_KEY, algorithms=[ALGORITHM])
    payload.pop("jti", None)
    payload.pop("auth_time", None)
    legacy_no_jti = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    r = client.post("/api/logout", headers={"Authorization": "Bearer " + legacy_no_jti})
    out["logout_no_jti"] = [r.status_code, r.json().get("reason")]
    out["legacy_still_auth"] = client.get(
        "/api/interview/config", headers={"Authorization": "Bearer " + legacy_no_jti}).status_code
finally:
    client.__exit__(None, None, None)
    delete_users(["t51_probe", "t51_probe_admin"])
    wipe()

print("PROBE " + json.dumps(out))
'''


def segment_source_recheck(node_path, args):
    section("C. 独立复核（重跑语义 + 端到端 + 前端接线）")

    probe_path = os.path.join(BACKEND_DIR, "_t51_probe.py")
    with open(probe_path, "w", encoding="utf-8") as fh:
        fh.write(PROBE)
    try:
        code, out = run([VENV_PYTHON, "_t51_probe.py"], timeout=600)
    finally:
        if os.path.exists(probe_path):
            os.remove(probe_path)

    probe = None
    if code is None:
        bad("C 段探针无法执行：%s" % out.strip()[:200])
    elif code != 0 or "PROBE " not in out:
        bad("C 段探针失败（结论不完整）：%s" % " | ".join(tail(out, 3))[:300])
    else:
        import json
        probe = json.loads(out.split("PROBE ", 1)[1].splitlines()[0])

    if probe:
        checks = [
            ("① 黑名单 TTL = 8 小时，且**等于** T-50 的绝对上限（R-4 的护栏）",
             probe.get("ttl") == 8 * 3600 and probe.get("ttl_equals_absolute") is True,
             (probe.get("ttl"), probe.get("ttl_equals_absolute"))),
            ("① 令牌只剩 5 分钟 → 仍按 8 小时留痕（下限兜住，R-4 的修正点）",
             probe.get("exp_short") is not None, probe.get("exp_short")),
            ("① 令牌已过期 → 仍留痕（否则登出在时钟偏差下变成空操作）",
             probe.get("exp_expired") is not None, probe.get("exp_expired")),
            ("① 缺 exp → 仍留痕", probe.get("exp_none") is not None, probe.get("exp_none")),
            ("① 令牌声称能活 20 小时 → 不跟着延长（不产生垃圾行）",
             probe.get("exp_long") is not None, probe.get("exp_long")),
            ("① 四种情形留痕时长一致（都是 now+8h）",
             probe.get("all_min_ttl") is True, probe.get("all_min_ttl")),
            ("① 不变量自检：记录覆盖令牌剩余寿命；只留 1 分钟的记录被判为不覆盖",
             probe.get("covers") is True and probe.get("covers_rejects_short") is False,
             (probe.get("covers"), probe.get("covers_rejects_short"))),
            ("① 缺 jti / 非字符串 jti 一律视为不可吊销",
             probe.get("target_missing_jti") is None and probe.get("target_int_jti") is None,
             (probe.get("target_missing_jti"), probe.get("target_int_jti"))),
            ("③ 重复吊销同一个 jti 是**幂等**的（只留一行）",
             probe.get("idempotent_count") == 1, probe.get("idempotent_count")),
            ("④ 已过期的吊销记录不算「已吊销」（与「已被清理」行为一致）",
             probe.get("live_revoked") is True and probe.get("expired_not_revoked") is False,
             (probe.get("live_revoked"), probe.get("expired_not_revoked"))),
            ("⑤ 清理**只**删已过期记录，仍生效的必须留着（删了就复活）",
             probe.get("purged") == 1 and probe.get("left") == 1
             and probe.get("live_survives_purge") is True,
             (probe.get("purged"), probe.get("left"), probe.get("live_survives_purge"))),
            ("⑥ 黑名单存储已登记进装配表（漏一行 = 运行时 AttributeError）",
             "token_blacklist" in (probe.get("registry") or []), probe.get("registry")),
            ("⑥ CORS 暴露 X-Token-Revoked（跨域下前端才读得到）",
             "X-Token-Revoked" in (probe.get("exposed") or []), probe.get("exposed")),
            ("⑥ 端到端：登出前两个受保护端点都是 200",
             probe.get("before_logout") == [200, 200], probe.get("before_logout")),
            ("⑥ 端到端：登出返回 200 且 revoked=true",
             probe.get("logout_status") == 200 and probe.get("logout_revoked") is True,
             (probe.get("logout_status"), probe.get("logout_revoked"))),
            ("⑥ 端到端：**登出后旧 token 两个端点都 401**（验收标准）",
             probe.get("after_logout") == [401, 401], probe.get("after_logout")),
            ("⑥ 端到端：401 带 X-Token-Revoked 标记",
             probe.get("revoked_header") == "revoked", probe.get("revoked_header")),
            ("⑥ 端到端：另一个会话（不同 jti）不受影响",
             probe.get("other_user_ok") == 200, probe.get("other_user_ok")),
            ("⑥ 端到端：**续期后的令牌也被吊销**（jti 沿用 ⇒ 整条链一次性吊销）",
             probe.get("renewed_present") is True and probe.get("renewed_after_logout") == 401,
             (probe.get("renewed_present"), probe.get("renewed_after_logout"))),
            ("⑥ 端到端：拿着**已过期**的令牌登出仍然成功（登出不得因过期而失败）",
             probe.get("logout_expired_status") == 200, probe.get("logout_expired_status")),
            ("⑥ 端到端：老令牌（无 jti）登出返回 no_jti，且鉴权照旧",
             probe.get("logout_no_jti") == [200, "no_jti"] and probe.get("legacy_still_auth") == 200,
             (probe.get("logout_no_jti"), probe.get("legacy_still_auth"))),
        ]
        for label, passed, actual in checks:
            if passed:
                ok(label)
            else:
                bad("%s（实际：%s）" % (label, actual))

    # —— ⑦ 前端接线（源码口径）——
    auth_ctx = read(FRONTEND_DIR, os.path.join("src", "auth", "AuthContext.tsx"))
    body = ""
    start = auth_ctx.find("const signOut = useCallback")
    end = auth_ctx.find("const applyRefreshedToken")
    if start >= 0 and end > start:
        body = auth_ctx[start:end]
    logout_idx = body.find("apiPost('/api/logout')")
    clear_idx = body.find("clearAuthStorage(storage, STORAGE_KEYS)")
    front_checks = [
        ("⑦ 前端 signOut 会调用 /api/logout（否则服务端吊销永远不会发生）", logout_idx > 0),
        ("⑦ 顺序正确：**先**调登出（请求才带得上令牌）**再**清本地",
         logout_idx > 0 and clear_idx > logout_idx),
        ("⑦ 登出请求是 fire-and-forget（网络失败不把用户留在登录态）",
         ".catch(" in body and "await apiPost('/api/logout')" not in body),
    ]
    for label, passed in front_checks:
        if passed:
            ok(label)
        else:
            bad(label)

    SEG["C"] = not FAILED


# ---------------------------------------------------------------------------
# D. 前端契约
# ---------------------------------------------------------------------------

def segment_frontend(node_path, args):
    section("D. 前端契约测试 + 类型检查")
    if args.skip_frontend:
        skip("--skip-frontend：按要求跳过")
        SEG["D"] = None
        return
    if not node_path:
        env_problem("没有 node，D 段跳过")
        SEG["D"] = None
        return

    code, out = run([node_path, "--test", "--experimental-test-isolation=none",
                     "--test-reporter=tap", "tests/**/*.test.mjs"], cwd=FRONTEND_DIR)
    if code is None:
        env_problem("前端契约测试无法执行：%s" % out.strip()[:200])
        SEG["D"] = None
    else:
        say("  —— `node --test`（TAP）尾部 5 行 ——")
        for line in tail(out):
            say("     %s" % line.encode("ascii", "replace").decode("ascii"))
        m = {k: re.search(r"^#\s*%s\s+(\d+)" % k, out, re.M) for k in ("tests", "pass", "fail")}
        if not all(m.values()):
            env_problem("未能解析前端 TAP 的 tests/pass/fail")
        else:
            tests, passed, failed = (int(m[k].group(1)) for k in ("tests", "pass", "fail"))
            if failed == 0:
                ok("前端契约测试全绿（tests=%d pass=%d fail=0）" % (tests, passed))
            else:
                bad("前端契约测试有 %d 项失败" % failed)
        SEG["D"] = (code == 0)

    tsc_js = os.path.join(FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")
    if os.path.exists(tsc_js):
        code, out = run([node_path, tsc_js, "-b"], cwd=FRONTEND_DIR)
        if code == 0:
            ok("tsc -b 退出码 0（前端改动类型安全）")
        else:
            bad("tsc -b 退出码 %s：%s" % (code, " | ".join(tail(out, 2))[:200]))
        if SEG.get("D"):
            SEG["D"] = (code == 0)
    else:
        env_problem("找不到 typescript，跳过 tsc -b")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    for key in ("A", "A2", "B", "C", "D"):
        if key not in SEG:
            continue
        state = SEG.get(key)
        label = "SKIP" if state is None else ("PASS" if state else "FAIL")
        say("  [%s] %s 段" % (label, key))

    if ENV_PROBLEMS:
        say("")
        say("  环境提示（不算失败，但会让结论不完整）：")
        for item in ENV_PROBLEMS:
            say("    - %s" % item)
    if SKIPPED:
        say("")
        say("  已跳过：")
        for item in SKIPPED:
            say("    - %s" % item)

    if FAILED:
        say("")
        say("  未通过项：")
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-51 人工验收未通过")
        return 1

    say("")
    say("✅ T-51 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-51 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-full-tests", action="store_true",
                        help="跳过 A-② 的全量后端测试（默认跑，约 80 秒）")
    parser.add_argument("--skip-frontend", action="store_true", help="跳过 D 段")
    args = parser.parse_args()

    say("T-51 人工验收：jti + token_blacklist 真登出（ADR-003 选 B）")
    say("仓库：%s" % REPO_DIR)

    node_path = segment_preflight()
    segment_backend_tests(args)
    segment_syntax()
    segment_source_recheck(node_path, args)
    segment_frontend(node_path, args)

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and (SEG.get("A") is None or SEG.get("C") is None):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

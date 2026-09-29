#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-50 人工验收：JWT 滑动续期 + 8 小时绝对上限 + auth_time（ADR-016，P0）。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t50_manual.py

可选参数：
    --skip-full-tests  跳过 A-② 的全量后端测试（默认会跑，562 项约 80 秒）
    --skip-frontend    跳过 D 段（前端契约测试 + tsc 检查）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 5 的 T-50）：

  * 剩余有效期 <50% 时**响应头回写新 token**；
  * 超 8h **拒绝续期**并返回 `X-Token-Expired: absolute`；
  * 前端提示重新登录**且保留后端会话**。

四段互不依赖的取证：

  0. 预检      —— python / 目标文件 / node（前端段需要）
  A. 后端测试  —— ① 本任务的 30 项（`tests.test_token_renewal`）必须全绿；
                  ② 全量 562 项必须全绿（续期中间件挂在**所有**请求上，
                     它一旦写错会同时打坏一大片既有用例，所以必须跑全量）
  B. 语法检查  —— 本次改动的 6 个文件 `py_compile` 通过
  C. 独立复核  —— **本脚本自己重跑一遍关键语义**（不动用测试里的断言）：
                  ① jti/auth_time 由登录链路并入；② 恰好 8h 算超限、差一秒不算；
                  ③ 半衰点边界（恰好 50% 不续期）；④ 真的续期一次：exp 往后延、
                     jti/auth_time **不变**（沿用同一续期链）；⑤ 缺声明/过期不续期；
                  ⑥ 中间件真的注册在 app 上、CORS 真的暴露两个响应头；
                  ⑦ 前端三处接线（读头落库 / 401 分支 / 绝对上限不登出）
  D. 前端契约  —— `node --test` 全绿 + `tsc -b` exit 0（前端改动不能悄悄打断）

为什么 C 段不用"启服务再发请求"：中间件与纯逻辑都能在**同进程内**直接调用
（`evaluate_renewal` 是纯函数、`TestClient(main.app)` 不起真实端口），
因此整轮验收不占端口、不需要 Token、也不会被"服务没起来"干扰。

已知边界（刻意如此，非遗漏）：
  * **不驱动真实浏览器**。前端"读头落库"与"绝对上限不登出"由源码接线断言 +
    T-36/T-40 的契约测试共同保证（`docs/36-manual-verification.md` §4 有肉眼验收步骤）。
  * **不验证"被吊销令牌不能复活"**：那需要 `token_blacklist`，属 **T-51**（🔒）。
    本任务只保证 `jti` **沿用同一条续期链**，把 T-51 的前提备好。
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

# 本任务的目标文件（相对各自根目录）
BACKEND_FILES = {
    "续期纯逻辑": os.path.join("utils", "token_renewal.py"),
    "续期中间件": os.path.join("utils", "token_renewal_middleware.py"),
    "令牌签发": "auth.py",
    "登录端点": os.path.join("routers", "auth_router.py"),
    "应用装配": "main.py",
    "后端测试": os.path.join("tests", "test_token_renewal.py"),
}
FRONTEND_FILES = {
    "认证响应纯逻辑": os.path.join("src", "services", "authResponse.ts"),
    "统一出口": os.path.join("src", "services", "api.ts"),
    "前端契约测试": os.path.join("tests", "api-convergence.test.mjs"),
}

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t34 / verify_t36 / verify_t40 同风格）
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
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    return lines[-n:]


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
        ok("%d 个后端目标文件就在位（纯逻辑 / 中间件 / 签发 / 登录 / 装配 / 测试）"
           % len(BACKEND_FILES))

    missing = [rel for rel in FRONTEND_FILES.values()
               if not os.path.exists(os.path.join(FRONTEND_DIR, rel))]
    if missing:
        bad("前端交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个前端目标文件就在位（认证响应 / 统一出口 / 契约测试）" % len(FRONTEND_FILES))

    if os.path.exists(VENV_PYTHON):
        ok("找到项目虚拟环境 python：%s" % os.path.relpath(VENV_PYTHON, REPO_DIR))
    else:
        env_problem("找不到 backend/venv/Scripts/python.exe —— 后续段可能失败")

    node_path = None
    for name in ("node", "node.exe"):
        code, out = run([name, "--version"], cwd=REPO_DIR, timeout=60)
        if code == 0:
            node_path = name
            ok("node 可执行：%s（%s）" % (name, out.strip()))
            break
    if not node_path:
        env_problem("找不到 node —— D 段（前端契约测试）会跳过")
    return node_path


# ---------------------------------------------------------------------------
# A. 后端测试
# ---------------------------------------------------------------------------

def segment_backend_tests(args):
    section("A. 后端测试（stdlib unittest，零新增依赖）")

    code, out = run([VENV_PYTHON, "-m", "unittest", "tests.test_token_renewal", "-q"])
    if code is None:
        env_problem("T-50 单元测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
    else:
        say("  —— `tests.test_token_renewal` 尾部 5 行 ——")
        for line in tail(out):
            say("     %s" % line.encode("ascii", "replace").decode("ascii"))
        if code == 0 and re.search(r"^OK", out, re.M):
            ok("T-50 的 30 项测试全绿（滑动阈值 / 8h 上限 / jti 沿用 / 响应头 / 中间件接线）")
        else:
            bad("T-50 单元测试未全绿（exit=%s）" % code)
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
        ok("全量后端测试通过（Ran %d tests OK）—— 续期中间件没有打坏既有链路" % ran)
    else:
        bad("全量后端测试未通过（exit=%s，Ran %s）" % (code, ran or "?"))
    SEG["A2"] = (code == 0)


# ---------------------------------------------------------------------------
# B. 语法检查
# ---------------------------------------------------------------------------

def segment_syntax():
    section("B. 语法检查（py_compile）")
    problems = []
    # Windows 上不能把 cfile 指到 `nul`（会被判为非常规文件）；
    # 也不要用 py_compile 的默认输出（会在源码树里留下 __pycache__）。
    # 统一编译到一个临时文件里，跑完就删。
    cfile = os.path.join(BACKEND_DIR, "_t50_compile_check.pyc")
    try:
        for label, rel in BACKEND_FILES.items():
            path = os.path.join(BACKEND_DIR, rel)
            try:
                py_compile.compile(path, cfile=cfile, doraise=True)
            except py_compile.PyCompileError as exc:
                problems.append("%s（%s）：%s" % (label, rel, str(exc)[:120]))
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
# C. 独立复核（重跑语义 + 源码接线）
# ---------------------------------------------------------------------------

PROBE = r'''
import json, time
from jose import jwt
from config import SECRET_KEY, ALGORITHM
from auth import create_access_token
from utils.token_renewal import (
    ABSOLUTE_MAX_LIFETIME_SECONDS, REFRESHED_TOKEN_HEADER, TOKEN_EXPIRED_HEADER,
    evaluate_renewal, exceeds_absolute_lifetime, has_renewal_claims, should_renew,
)

out = {}
NOW = 1700000000

def payload(age, lifetime, auth_age=None, jti="jti-x"):
    iat = NOW - age
    return {
        "sub": "probe", "user_id": 1, "role": "user", "jti": jti,
        "auth_time": NOW - (age if auth_age is None else auth_age),
        "iat": iat, "exp": iat + lifetime,
    }

# 1) 登录链路签发的令牌确实带 jti / auth_time
tok = create_access_token({"sub": "probe", "user_id": 1, "role": "user"})
p = jwt.decode(tok, SECRET_KEY, algorithms=[ALGORITHM])
out["login_claims"] = [bool(isinstance(p.get("jti"), str) and p["jti"]), bool(p.get("auth_time"))]

# 2) 半衰点边界：恰好 50% 不续期，差一秒续期
out["half_exact"] = should_renew(payload(15 * 60, 30 * 60), NOW)
out["half_past"] = should_renew(payload(15 * 60 + 1, 30 * 60), NOW)

# 3) 8 小时边界：恰好算超限，差一秒不算
out["abs_exact"] = exceeds_absolute_lifetime(payload(60, 30 * 60, auth_age=ABSOLUTE_MAX_LIFETIME_SECONDS), NOW)
out["abs_under"] = exceeds_absolute_lifetime(payload(60, 30 * 60, auth_age=ABSOLUTE_MAX_LIFETIME_SECONDS - 1), NOW)

# 4) 真的续期一次：exp 往后延、jti/auth_time 不变
should_issue, claims, reason = evaluate_renewal(payload(21 * 60, 30 * 60), 30 * 60, NOW)
out["renew_issue"] = should_issue
out["renew_reason"] = reason
if should_issue:
    old = payload(21 * 60, 30 * 60)
    out["renew_keeps_jti"] = claims["jti"] == old["jti"]
    out["renew_keeps_auth_time"] = claims["auth_time"] == old["auth_time"]
    out["renew_extends_exp"] = claims["exp"] == NOW + 30 * 60 and claims["exp"] > old["exp"]

# 5) 超限拒绝 + 原因码；过期不续期（原因 expired）
_, _, abs_reason = evaluate_renewal(payload(60, 30 * 60, auth_age=9 * 3600), 30 * 60, NOW)
out["abs_reason"] = abs_reason
_, _, expired_reason = evaluate_renewal(payload(40 * 60, 30 * 60), 30 * 60, NOW)
out["expired_reason"] = expired_reason

# 6) 缺声明（老令牌）不续期，但声明判定本身可区分
out["legacy_claims"] = has_renewal_claims({"sub": "x", "iat": 1, "exp": 2})
out["legacy_issue"] = evaluate_renewal({"sub": "x", "iat": NOW - 1200, "exp": NOW + 600}, 1800, NOW)[0]

# 7) 中间件真的注册在 app 上；CORS 暴露两个头；中间件在外层
import main
names = [m.cls.__name__ for m in main.app.user_middleware]
out["mw_names"] = names
cors = [m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
out["exposed"] = list((cors[0].kwargs.get("expose_headers") or [])) if cors else []

# 8) 端到端（TestClient，不占端口）：低剩余时间 → 头里真的带新令牌
from fastapi.testclient import TestClient
from tests.support import create_test_user, delete_users, mint_renewable_token
client = TestClient(main.app)
client.__enter__()
try:
    create_test_user("t50_probe", role="user")
    t_low = mint_renewable_token("t50_probe", minutes=30, age_minutes=21, user_id=1)
    r_low = client.get("/api/interview/config", headers={"Authorization": "Bearer " + t_low})
    out["e2e_low_status"] = r_low.status_code
    out["e2e_low_has_header"] = bool(r_low.headers.get(REFRESHED_TOKEN_HEADER))
    t_abs = mint_renewable_token("t50_probe", minutes=600, age_minutes=540, user_id=1)
    r_abs = client.get("/api/interview/config", headers={"Authorization": "Bearer " + t_abs})
    out["e2e_abs_status"] = r_abs.status_code
    out["e2e_abs_marker"] = r_abs.headers.get(TOKEN_EXPIRED_HEADER)
    t_fresh = mint_renewable_token("t50_probe", minutes=30, age_minutes=1, user_id=1)
    r_fresh = client.get("/api/interview/config", headers={"Authorization": "Bearer " + t_fresh})
    out["e2e_fresh_status"] = r_fresh.status_code
    out["e2e_fresh_header"] = r_fresh.headers.get(REFRESHED_TOKEN_HEADER)
finally:
    client.__exit__(None, None, None)
    delete_users(["t50_probe"])

print("PROBE " + json.dumps(out))
'''


def _request_body(api_code):
    """截出 `services/api.ts` 里 `request()` 的函数体（不含 `authFetch` 之后的内容）。"""
    start = api_code.find("export async function request")
    if start < 0:
        return ""
    end = api_code.find("export async function authFetch", start)
    return api_code[start:end if end > start else len(api_code)]


def _request_body_order_ok(api_code):
    """绝对上限分支必须**排在**统一登出之前（否则"保留会话可恢复"直接失效）。"""
    body = _request_body(api_code)
    abs_idx = body.find("isAbsoluteExpiryResponse(response)")
    notify_idx = body.find("notifyUnauthorized()")
    return abs_idx > 0 and notify_idx > 0 and abs_idx < notify_idx


def segment_source_recheck(node_path, args):
    section("C. 独立复核（重跑语义 + 源码接线）")

    # —— C-① 纯逻辑与端到端（用被测代码本身，不 import 测试里的断言）——
    probe_path = os.path.join(BACKEND_DIR, "_t50_probe.py")
    with open(probe_path, "w", encoding="utf-8") as fh:
        fh.write(PROBE)
    try:
        code, out = run([VENV_PYTHON, "_t50_probe.py"], timeout=600)
    finally:
        if os.path.exists(probe_path):
            os.remove(probe_path)

    if code is None:
        env_problem("C 段探针无法执行：%s" % out.strip()[:200])
        probe = None
    elif code != 0 or "PROBE " not in out:
        env_problem("C 段探针失败：%s" % " | ".join(tail(out, 3))[:300])
        probe = None
    else:
        import json
        probe = json.loads(out.split("PROBE ", 1)[1].splitlines()[0])

    if probe:
        checks = [
            ("① 登录签发的令牌带 jti 与 auth_time（绝对上限的起算点）",
             all(probe.get("login_claims", [])), probe.get("login_claims")),
            ("① 恰好 50% 剩余**不**续期", probe.get("half_exact") is False, probe.get("half_exact")),
            ("① 过半衰点一秒就续期（阈值不是恒假）", probe.get("half_past") is True, probe.get("half_past")),
            ("① 恰好 8 小时算超限（`>=`）", probe.get("abs_exact") is True, probe.get("abs_exact")),
            ("① 差一秒不算超限（边界不是恒真）", probe.get("abs_under") is False, probe.get("abs_under")),
            ("① 低剩余寿命时判定为续期", probe.get("renew_issue") is True, probe.get("renew_issue")),
            ("① 续期沿用同一 jti（T-51 吊销整条链的前提）",
             probe.get("renew_keeps_jti") is True, probe.get("renew_keeps_jti")),
            ("① 续期不重置 auth_time（否则绝对上限被绕过）",
             probe.get("renew_keeps_auth_time") is True, probe.get("renew_keeps_auth_time")),
            ("① 续期把 exp 往后延一个完整寿命",
             probe.get("renew_extends_exp") is True, probe.get("renew_extends_exp")),
            ("① 超 8h 的拒绝原因码是 absolute_limit",
             probe.get("abs_reason") == "absolute_limit", probe.get("abs_reason")),
            ("① 已过期令牌的原因码是 expired（不续期）",
             probe.get("expired_reason") == "expired", probe.get("expired_reason")),
            ("① 老令牌（无 jti/auth_time）不续期但也不报错",
             probe.get("legacy_claims") is False and probe.get("legacy_issue") is False,
             (probe.get("legacy_claims"), probe.get("legacy_issue"))),
            ("① 续期中间件真的注册在 app 上",
             "TokenRenewalMiddleware" in (probe.get("mw_names") or []), probe.get("mw_names")),
            ("① CORS 暴露 X-Refreshed-Token 与 X-Token-Expired",
             {"X-Refreshed-Token", "X-Token-Expired"} <= set(probe.get("exposed") or []),
             probe.get("exposed")),
            ("① 端到端：低剩余寿命请求 200 且响应头带新令牌",
             probe.get("e2e_low_status") == 200 and probe.get("e2e_low_has_header") is True,
             (probe.get("e2e_low_status"), probe.get("e2e_low_has_header"))),
            ("① 端到端：超 8h 请求 401 且带 X-Token-Expired: absolute",
             probe.get("e2e_abs_status") == 401 and probe.get("e2e_abs_marker") == "absolute",
             (probe.get("e2e_abs_status"), probe.get("e2e_abs_marker"))),
            ("① 端到端：刚签发的令牌不续期（没有写放大）",
             probe.get("e2e_fresh_status") == 200 and probe.get("e2e_fresh_header") is None,
             (probe.get("e2e_fresh_status"), probe.get("e2e_fresh_header"))),
        ]
        for label, passed, actual in checks:
            if passed:
                ok(label)
            else:
                bad("%s（实际：%s）" % (label, actual))

    # —— C-② 前端接线（源码口径）——
    auth_response = read(FRONTEND_DIR, os.path.join("src", "services", "authResponse.ts"))
    api_src = read(FRONTEND_DIR, os.path.join("src", "services", "api.ts"))
    # 去注释：解释这条规则的注释里会**引用** `notifyUnauthorized()` 这个名字，
    # 不去掉的话"谁在前"会被注释带偏（与 T-50 的契约测试同一个坑）。
    api_code = re.sub(r"/\*[\s\S]*?\*/", "", api_src)
    api_code = "\n".join(
        "" if line.strip().startswith("//") else re.sub(r"\s//.*$", "", line)
        for line in api_code.splitlines()
    )

    front_checks = [
        ("② 前端认识 X-Refreshed-Token 响应头",
         "X-Refreshed-Token" in auth_response),
        ("② 前端认识 X-Token-Expired 响应头与 absolute 取值",
         "X-Token-Expired" in auth_response and "'absolute'" in auth_response),
        ("② 统一出口仍集中处理续期头（handleRefreshedToken → localStorage）",
         "handleRefreshedToken(response)" in api_code),
        # 只看 `request()` 的**函数体**：`notifyUnauthorized` 的定义在文件更前面，
        # 全文搜索会命中定义而不是调用（第一版就因此报了假失败）。
        ("② 绝对上限分支排在统一登出之前（保留会话可恢复）",
         _request_body_order_ok(api_code)),
        ("② 普通 401 仍然统一登出（T-36 不变量未回退）",
         "notifyUnauthorized();" in _request_body(api_code)),
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
        state = SEG.get(key)
        if key not in SEG:
            continue
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
        say("❌ T-50 人工验收未通过")
        return 1

    say("")
    say("✅ T-50 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-50 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-full-tests", action="store_true",
                        help="跳过 A-② 的全量后端测试（默认跑，约 80 秒）")
    parser.add_argument("--skip-frontend", action="store_true", help="跳过 D 段")
    args = parser.parse_args()

    say("T-50 人工验收：JWT 滑动续期 + 8 小时绝对上限 + auth_time（ADR-016，P0）")
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

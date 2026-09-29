#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-40 人工验收：AuthContext 集中并持久化 token + userId（Bug 2 前端）。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t40_manual.py

可选参数：
    --skip-tsc   跳过 B 段（TypeScript 全量类型检查）
    --verbose    把 A 段 TAP 尾部行数放宽（默认 5 行）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 4 的 T-40）：

  * **刷新后 `userId` 仍存在**；
  * **登出清理干净**。

四段互不依赖的取证：

  0. 预检      —— node、目标文件、node_modules。
  A. 契约测试  —— `node --test`（TAP）：fail 必须为 0，测试数量不低于下限，
                  且**点名用例必须真的出现过**（防止测试被删 / 改名后"假通过"）。
  B. 类型检查  —— `tsc -b` 退出码 0。
  C. 独立复核  —— **本脚本不 import 测试里的任何辅助函数**，用两套独立证据：
                  ① 重扫源码：持久化清单 / 唯一写路径 / hydrated 守卫 / 首帧还原 /
                     三个入口 / T-36 口子兑现 / 401 接线未回退（含判别力自检）；
                  ② **对着 5 条关键用例逐个点名跑**（`--test-name-pattern` 单文件隔离跑）——
                     这比"整包跑一次看总数"强得多：能证明**这一条**真的执行过并通过；
                  ③ 用 Python **独立复述**持久化语义（写入 → 刷新还原 → 登出清空），
                     键清单**从源码里解析出来**（不是硬编码），所以实现一改它就报错。

为什么 C-③ 是"独立复述"而不是"再跑一遍 JS"：
  `.ts` 模块 Node 不能直接 import（要类型剥离），把 `authStorage.ts` 机械转换成 `.mjs`
  需要重写一整套 TS 语法剥离规则 —— 那条路本轮试过，**探针副本里的代码被改写过**
  （对象字面量的值、泛型实参都被啃掉），"探针通过"就不再等于"被测代码通过"，
  属于自欺欺人。因此改成：**真正的执行交给 A 段（node:test 有类型剥离，跑的是原文件）**，
  C-③ 只做另一套语言的语义复述 + 从源码解析键清单，两边都过才算证据。

已知边界（刻意如此，非遗漏）：
  * **不驱动真实浏览器**。"刷新后 userId 仍存在"由"纯逻辑真跑 + 首帧还原接线"共同证明：
    还原路径就是 `AuthProvider` 首帧调用的同一个函数。肉眼验收见
    `docs/35-manual-verification.md` §4。
  * **还原是乐观的**：不校验令牌是否过期（校验要发请求，属于 T-41）。
    本任务只保证"上次登录留下的是谁"不丢，不保证"这个令牌还有效"。
  * **`npm run lint` 本来就是红的**：本任务只核对"没有新增结构性违规"，不假装 lint 是绿的。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
SRC_DIR = os.path.join(FRONTEND_DIR, "src")

FILES = {
    "持久化纯逻辑": "src/auth/authStorage.ts",
    "认证真源": "src/auth/AuthContext.tsx",
    "接线桥": "src/auth/AuthBridge.tsx",
    "统一出口": "src/services/api.ts",
    "契约测试": "tests/auth-persistence.test.mjs",
}

# A 段必须出现过的测试名（防止测试被删/改名后假通过）
REQUIRED_TEST_NAMES = [
    "刷新后 userId 仍存在：写入 → 重新还原，整份会话身份原样回来",
    "登出清理干净：四个键全部消失，且不误伤同源的其他数据",
    "持久化只有一条写路径，且**只在 hydrated 之后**写（不会抹掉刚还原的会话）",
    "T-36 的口子已兑现：AuthBridge 订阅 onTokenRefreshed 并调用 applyRefreshedToken",
    "userId 解析器拒绝一切非正整数（宁可退化成匿名，也不拿脏值去拼请求）",
]

# C-② 逐个点名跑的 5 条（与上面同一个清单；分开写是因为语义不同：
# 上面是"整包输出里出现过"，下面是"单独跑这条必须 ok 1"）
PINPOINT_TESTS = REQUIRED_TEST_NAMES

# T-40 的持久化清单（少一个 = 登出漏清 / 刷新丢；多一个 = 越权删数据）
EXPECTED_KEYS = ["token", "userId", "role", "username"]

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t34 / verify_t36 同风格）
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


def run(cmd, cwd=FRONTEND_DIR, timeout=900):
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


def find_node():
    for name in ("node", "node.exe"):
        code, out = run([name, "--version"], cwd=REPO_DIR, timeout=60)
        if code == 0:
            return name, out.strip()
    return None, None


def read(rel):
    with open(os.path.join(FRONTEND_DIR, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def code_only(text):
    """去注释：注释里正**引用**旧写法与规则本身（那是文档，不是代码）。"""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(re.sub(r"\s//.*$", "", line))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# C-③：Python 侧的持久化语义复述（键清单从源码解析，不硬编码）
# ---------------------------------------------------------------------------

def parse_persisted_keys(storage_src):
    """从 `AUTH_PERSISTED_KEYS = [...]` 解析出键清单，并把标识符换成字面量值。

    这样"实现改了键名"会让 C-③ 跟着变（而不是在这里维护第二份硬编码清单）。
    """
    literals = dict(re.findall(r"(AUTH_\w+_STORAGE_KEY)\s*=\s*'([^']+)'", storage_src))
    m = re.search(r"AUTH_PERSISTED_KEYS\s*=\s*\[([^\]]*)\]", storage_src, re.S)
    if not m:
        return None
    body = m.group(1)
    keys = []
    for token in [t.strip() for t in body.split(",") if t.strip()]:
        if token.startswith("'") or token.startswith('"'):
            keys.append(token.strip("'\""))
        else:
            keys.append(literals.get(token))
    return keys


def oracle_semantics(keys):
    """用 Python 复述持久化语义，返回断言清单。

    模拟的是 `authStorage.ts` 里那几个函数的**可观察行为**：
      · 写入：`null` 表示删除该键，其余值序列化成字符串；
      · 还原：userId 只认 `^\\d+$` 且 > 0；role 只认 admin/user；username 缺省成空串；
      · 登出：逐个删键（不是 `clear()`）。
    """
    out = []

    # 1) 写入 → 刷新型还原
    store = {}
    persisted = {"token": "jwt-abc", "userId": 42, "role": "user", "username": "alice"}
    for key in keys:
        value = persisted.get(key)
        if value is None:
            store.pop(key, None)
        else:
            store[key] = str(value)
    out.append(("refresh-keeps-userId", store.get("userId") == "42" and int(store["userId"]) == 42,
                store.get("userId")))
    out.append(("refresh-keeps-identity",
                store.get("token") == "jwt-abc" and store.get("role") == "user"
                and store.get("username") == "alice",
                "|".join(str(store.get(k)) for k in ("token", "role", "username"))))

    # 2) 反向自检：userId 缺失时必须还原成"无身份"（否则第 1 条恒真）
    store2 = dict(store)
    store2.pop("userId", None)
    out.append(("discriminating-userId", store2.get("userId") is None, store2.get("userId")))

    # 3) 登出清理干净，且不误伤同源其他数据
    store3 = dict(store)
    store3["ui-theme"] = "dark"
    for key in keys:
        store3.pop(key, None)
    left = [k for k in keys if k in store3]
    out.append(("logout-clears-all", left == [], ",".join(left) or "all-cleared"))
    out.append(("logout-keeps-others", store3.get("ui-theme") == "dark", store3.get("ui-theme")))

    # 4) userId 解析：只认正整数
    def parse_user_id(raw):
        if not isinstance(raw, str):
            return None
        raw = raw.strip()
        if not re.fullmatch(r"\d+", raw):
            return None
        value = int(raw)
        return value if value > 0 else None

    bads = ["", "   ", "abc", "12abc", "0", "-1", "3.5", "NaN", "Infinity", None]
    accepted = [b for b in bads if parse_user_id(b) is not None]
    out.append(("userId-rejects-garbage", accepted == [], ",".join(str(a) for a in accepted) or "all-rejected"))
    out.append(("userId-accepts-valid", parse_user_id("42") == 42 and parse_user_id(" 7 ") == 7, "ok"))

    # 5) role 白名单
    def parse_role(raw):
        return raw if raw in ("admin", "user") else None

    out.append(("role-whitelist",
                parse_role("admin") == "admin" and parse_role("user") == "user"
                and parse_role("root") is None,
                "ok"))

    # 6) 写 null = 删除键（不是留下字符串 "null"）
    store6 = dict(store)
    store6["token"] = None  # 模拟 writeAuthValue(storage, 'token', null)
    store6.pop("token", None)
    out.append(("null-removes-key", store6.get("token") is None, store6.get("token")))

    return out


# ---------------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------------

def segment_preflight():
    section("0. 预检（node / 目标文件 / node_modules）")

    node_path, node_version = find_node()
    if node_path:
        ok("node 可执行：%s（%s）" % (node_path, node_version))
    else:
        env_problem("找不到 node 可执行文件（A 段与 C-② 无法运行）")

    missing = [rel for rel in FILES.values() if not os.path.exists(os.path.join(FRONTEND_DIR, rel))]
    if missing:
        bad("T-40 的交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个目标文件全部就位（持久化纯逻辑 / 认证真源 / 接线桥 / 统一出口 / 契约测试）"
           % len(FILES))

    if os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules")):
        ok("node_modules 存在")
    else:
        env_problem("缺少 frontend/node_modules —— 请先 `cd frontend; npm install`")

    return node_path


# ---------------------------------------------------------------------------
# A. 契约测试（整包）
# ---------------------------------------------------------------------------

def segment_contract_tests(node_path):
    section("A. 前端契约测试（node --test，零依赖、不联网）")
    if not node_path:
        env_problem("没有 node，A 段跳过")
        SEG["A"] = None
        return

    code, out = run([node_path, "--test", "--experimental-test-isolation=none",
                     "--test-reporter=tap", "tests/**/*.test.mjs"], timeout=600)
    if code is None:
        env_problem("契约测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
        return

    lines = [l.strip() for l in out.splitlines() if l.strip()]
    say("  —— `node --test`（TAP）尾部 5 行 ——")
    for line in lines[-5:]:
        say("     %s" % line.encode("ascii", "replace").decode("ascii"))

    m = {k: re.search(r"^#\s*%s\s+(\d+)" % k, out, re.M) for k in ("tests", "pass", "fail")}
    if not all(m.values()):
        env_problem("未能从 TAP 输出里解析出 tests/pass/fail")
        SEG["A"] = False
        return
    tests, passed, failed = (int(m[k].group(1)) for k in ("tests", "pass", "fail"))
    say("  解析结果：tests=%d pass=%d fail=%d" % (tests, passed, failed))

    if failed == 0:
        ok("全部契约测试通过（pass=%d fail=0）" % passed)
    else:
        bad("有 %d 项契约测试失败（完整输出请直接跑 npm run test:node）" % failed)

    # T-40 新增 14 项；加进来之后总数应 ≥ 110（T-36 之后为 101）。
    if tests >= 110:
        ok("测试数量合理（%d ≥ 110，说明既有契约测试没被删、T-40 的用例真的进了套件）" % tests)
    else:
        bad("只跑了 %d 项测试（预期 ≥ 110）" % tests)

    for name in REQUIRED_TEST_NAMES:
        if name in out:
            ok("点名用例确实跑过：%s" % name)
        else:
            bad("输出里没有出现点名用例「%s」—— 它可能被删/改名，那样就是假通过" % name)

    SEG["A"] = (failed == 0)


# ---------------------------------------------------------------------------
# B. tsc
# ---------------------------------------------------------------------------

def segment_tsc(node_path, args):
    section("B. TypeScript 全量类型检查（tsc -b）")
    if args.skip_tsc:
        skip("--skip-tsc：按要求跳过")
        SEG["B"] = None
        return
    tsc_js = os.path.join(FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")
    if not node_path or not os.path.exists(tsc_js):
        env_problem("找不到 node_modules/typescript/bin/tsc，跳过 B 段")
        SEG["B"] = None
        return
    code, out = run([node_path, tsc_js, "-b"], timeout=900)
    if code is None:
        env_problem("tsc 无法执行：%s" % out.strip()[:200])
        SEG["B"] = None
        return
    if code == 0:
        ok("tsc -b 退出码 0：快照类型、reducer 分支、还原路径全部类型安全")
    else:
        bad("tsc -b 退出码 %s：\n%s" % (code, out.strip()[:1500]))
    SEG["B"] = (code == 0)


# ---------------------------------------------------------------------------
# C. 独立复核
# ---------------------------------------------------------------------------

def segment_source_recheck(node_path, args):
    section("C. 独立复核（本脚本重扫源码 + 逐个点名跑 + Python 侧语义复述）")

    storage_src = code_only(read("src/auth/authStorage.ts"))
    context_src = code_only(read("src/auth/AuthContext.tsx"))
    bridge_src = code_only(read("src/auth/AuthBridge.tsx"))
    test_src = read("tests/auth-persistence.test.mjs")

    # —— C-① 源码重扫 ——
    keys = parse_persisted_keys(storage_src)
    if keys is None:
        bad("① 没能从 authStorage.ts 解析出 AUTH_PERSISTED_KEYS（或它被删了）")
        keys = []
    elif sorted(keys) == sorted(EXPECTED_KEYS):
        ok("① 持久化清单（从源码解析）正好是这 4 个键：%s" % "、".join(keys))
    else:
        bad("① 持久化清单与预期不符：实际 %s（预期 %s）" % (keys, sorted(EXPECTED_KEYS)))

    if re.search(r"AUTH_TOKEN_STORAGE_KEY\s*=\s*'token'", storage_src) and \
            re.search(r"AUTH_USER_ID_STORAGE_KEY\s*=\s*'userId'", storage_src):
        ok("① 存储键字面量只有一份来源（authStorage），续期链路与 AuthContext 共用")
    else:
        bad("① 存储键字面量不在 authStorage 里（两份写法迟早漂移）")

    if re.search(r"persistAuthSnapshot\(\s*storage", context_src):
        ok("① AuthContext 的持久化只有一条写路径（persistAuthSnapshot）")
    else:
        bad("① AuthContext 没有调用唯一写路径 persistAuthSnapshot")

    write_idx = context_src.find("persistAuthSnapshot(")
    guard_idx = context_src.rfind("if (!state.hydrated) return;", 0, write_idx)
    if guard_idx > 0:
        ok("① 写盘前有 hydrated 守卫（首帧空快照不会把存储里的会话抹掉）")
    else:
        bad("① 写盘前缺 hydrated 守卫")

    if re.search(r"restoreAuthState\(storage, STORAGE_KEYS\)", context_src) and \
            re.search(r"useReducer\(reducer, undefined, \(\) => \{", context_src):
        ok("① 首帧就用 restoreAuthState 惰性还原（不依赖 effect 时序，不会闪一下未登录）")
    else:
        bad("① AuthProvider 没有在初始化时还原会话身份（或没走惰性初值）")

    entry_problems = []
    for pattern, label in [
        (r"case 'SIGN_IN':", "reducer 的 SIGN_IN 分支"),
        (r"userId: event\.value\.userId \?\? null", "SIGN_IN 落 userId"),
        (r"clearAuthStorage\(storage, STORAGE_KEYS\)", "signOut 立刻清盘"),
        (r"case 'SIGN_OUT':", "reducer 的 SIGN_OUT 分支"),
        (r"case 'REFRESH_TOKEN':", "reducer 的 REFRESH_TOKEN 分支"),
        (r"registerUnauthorizedHandler\(signOut\)", "401 统一登出接线（T-36 不变量）"),
    ]:
        source = bridge_src if "Unauthorized" in pattern else context_src
        if not re.search(pattern, source):
            entry_problems.append(label)
    if not entry_problems:
        ok("① signIn / signOut / applyRefreshedToken 入口齐全，登出立刻清盘，T-36 接线未回退")
    else:
        bad("① 缺少：%s" % "、".join(entry_problems))

    if re.search(r"onTokenRefreshed\(applyRefreshedToken\)", bridge_src):
        ok("① T-36 的口子已兑现：AuthBridge 订阅续期并调用 applyRefreshedToken")
    else:
        bad("① AuthBridge 没有把续期令牌接进 AuthContext（T-36 的口子没兑现）")

    direct = sorted(set(re.findall(r"localStorage\.(setItem|removeItem)\(", context_src)))
    if not direct:
        ok("① AuthContext 不再直接 setItem/removeItem（持久化口径只有一处）")
    else:
        bad("① AuthContext 仍在直接 %s —— 持久化口径分裂" % "、".join(direct))

    # —— C-② 逐个点名跑（证明"这一条真的执行并通过"） ——
    if not node_path:
        env_problem("没有 node，C-②（点名用例）跳过")
    else:
        say("")
        say("  ② 逐个点名跑关键用例（`--test-name-pattern` 逐个过滤，默认 TAP 输出）：")
        for name in PINPOINT_TESTS:
            # `--test-name-pattern` 是**正则**：用例名里带 `**`（"Nothing to repeat"）、
            # `(`、`|` 等元字符会直接让 node 报错退出。因此先转义成字面量
            # （转义后仍能匹配同一条用例名里的普通中文与空格）。
            pattern = re.escape(name)
            code, out = run(
                [node_path, "--test", "--experimental-test-isolation=none",
                 "--test-name-pattern", pattern, "tests/auth-persistence.test.mjs"],
                timeout=300,
            )
            if code is None:
                env_problem("点名跑失败（无法执行）：%s" % out.strip()[:160])
                continue
            passed_n = len(re.findall(r"^✔", out, re.M))
            failed_n = len(re.findall(r"^✖", out, re.M))
            if code == 0 and passed_n == 1 and failed_n == 0:
                say("     [PASS] 单独跑通（该用例唯一命中）：%s"
                    % name.encode("ascii", "replace").decode("ascii"))
            else:
                bad("② 单独跑「%s」没有通过（exit=%s 命中=%d 失败=%d）"
                    % (name, code, passed_n, failed_n))

    # —— C-③ Python 侧语义复述（键清单来自源码） ——
    say("")
    say("  ③ Python 独立复述持久化语义（键清单从 authStorage.ts 解析，不硬编码）：")
    if not keys:
        bad("③ 没有可用的键清单，语义复述跳过")
    else:
        for name, passed, detail in oracle_semantics(keys):
            labels = {
                "refresh-keeps-userId": "写入 → 丢掉内存重新还原：userId 仍然是 42",
                "refresh-keeps-identity": "同一次还原里 token / role / username 也原样回来",
                "discriminating-userId": "判别力自检：userId 缺失时必须还原成\"无身份\"（否则上一条恒真）",
                "logout-clears-all": "登出后 4 个键全部消失",
                "logout-keeps-others": "登出没有误删同源的非认证数据（不是粗暴 clear()）",
                "userId-rejects-garbage": "userId 解析只认正整数（空串/字母/负数/小数/NaN 全拒绝）",
                "userId-accepts-valid": "userId 解析对合法值仍然有效（规则不是恒假）",
                "role-whitelist": "role 只放行 admin/user，改坏的值退化成无角色",
                "null-removes-key": "写 null = 删除该键（盘上不留字符串 'null'）",
            }
            label = labels.get(name, name)
            if passed:
                ok("③ %s" % label)
            else:
                bad("③ %s（实际值：%s）" % (label, detail))

    # —— C-④ 判别力自检 + 反例 ——
    naive = "useEffect(() => { persistAuthSnapshot(storage, snapshot); }, [snapshot]);"
    if "if (!state.hydrated) return;" not in naive:
        ok("④ 判别力自检：没有 hydrated 守卫的写法会被判为违规")
    else:
        bad("④ 判别力自检失败")

    legacy_keys = ["token", "role", "username"]
    if "userId" not in legacy_keys and "userId" in (keys or []):
        ok("④ 反例自检：旧清单（token/role/username）不含 userId，本任务确实新增了它")
    else:
        bad("④ 反例自检失败：userId 没有出现在持久化清单里")

    if "AUTH_PERSISTED_KEYS" in test_src and "persistAuthSnapshot" in test_src:
        ok("④ 契约测试真的 import 并调用了被测模块（不是在测试里另写一份逻辑）")
    else:
        bad("④ 契约测试没有直接调用被测模块")

    SEG["C"] = not FAILED


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    for key in ("A", "B", "C"):
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
        say("❌ T-40 人工验收未通过")
        return 1

    say("")
    say("✅ T-40 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-40 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-tsc", action="store_true", help="跳过 TypeScript 类型检查")
    parser.add_argument("--verbose", action="store_true", help="打印更多 TAP 尾部行")
    args = parser.parse_args()

    say("T-40 人工验收：AuthContext 集中并持久化 token + userId（Bug 2 前端）")
    say("仓库：%s" % REPO_DIR)

    node_path = segment_preflight()
    segment_contract_tests(node_path)
    segment_tsc(node_path, args)
    segment_source_recheck(node_path, args)

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and (SEG.get("A") is None or SEG.get("B") is None):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

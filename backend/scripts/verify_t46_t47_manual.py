#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-46 / T-47 人工验收：面谈主流程 / 报告 / 个人中心 / 通知中心 / 题库拆分。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t46_t47_manual.py

可选参数：
    --skip-tsc     跳过 B 段（TypeScript 全量类型检查）
    --with-build   额外尝试 `vite build`（受限沙箱里会以 spawn EPERM 失败，见 §边界）

退出码：0 = 通过；1 = 未通过；2 = 环境问题（缺 node / 缺 node_modules 等）。

────────────────────────────────────────────────────────────────────────────
它由四段**互不依赖**的取证拼成，任何一段单独看都不足以证明"拆干净了"：

  0. 预检        —— node 用哪个可执行文件、13 个目标文件在不在、node_modules 有没有。
  A. 契约测试    —— `node --test --test-reporter=tap` 跑 frontend/tests/**/*.test.mjs，
                    要求 `fail=0`，且**点名用例必须真的出现过**
                    （防止"测试文件被删/改名"后 0 项 0 失败式假通过）。
  B. 类型检查    —— `tsc -b` 退出码必须为 0（拆分最容易死在类型上）。
  C. 独立复核    —— **本脚本自己重扫源码**，不 import 测试里的任何辅助函数：
                    逐条核对"全树 ≤400 行 / App.tsx 是装配层 / 单一所有者 /
                    接口归属 / 无反向依赖 / T-42 与 T-43 的不变量随代码一起搬走"，
                    并且带**判别力自检**（把拆分前的形状喂进同一个判定函数，必须判为失败）。
  D. 构建探测    —— `vite build`，**默认跳过**（见 §边界）。

────────────────────────────────────────────────────────────────────────────
已知边界（刻意如此，非遗漏）：

  * **不驱动真实浏览器**。"点开始面试真的渲染出面试室""报告页真的显示超时标注"是
    **源码契约**证明的（组件装配点、`data-testid`、条件渲染），不是截图。
    肉眼验收：`cd frontend; npm run dev` → 登录 → 上传简历 → 面试 → 出报告。
  * **`vite build` 在受限沙箱里以 `spawn EPERM` 失败**（要 spawn 走管道的子进程，
    沙箱禁止命名管道）。与 docs/29 §3、docs/30 §4 记录的是同一个**沙箱边界**，
    不是本次改动的问题：本脚本把它标成 `[ENV]` 而非 `[FAIL]`；`tsc -b` 不受影响。
  * **`npm run lint` 本来就是红的**（HEAD 上仅 App.tsx 就有 31 条）。
    T-46 / T-47 把代码搬了家，条数结构随之变化：本脚本 C 段只核对
    "没有新增结构性违规"（判据见 `check_lint_invariants`），不假装 lint 是绿的。
  * **A 段依赖测试输出里的中文用例名**，因此固定用 TAP 报告器
    （默认 spec 报告器会打 `ⓘ`（U+2139），在 GBK 控制台上让 Python 抛 UnicodeEncodeError）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
SRC_DIR = os.path.join(FRONTEND_DIR, "src")
TESTS_DIR = os.path.join(FRONTEND_DIR, "tests")

# T-46 / T-47 的交付物（相对 frontend/）
DELIVERABLES = [
    "src/interview/useInterviewTimeout.ts",
    "src/interview/useSpeech.ts",
    "src/interview/useInterviewChat.ts",
    "src/interview/useInterviewSession.ts",
    "src/interview/InterviewRoom.tsx",
    "src/interview/resumeUpload.ts",
    "src/interview/questionBank.ts",
    "src/interview/QuestionBankModal.tsx",
    "src/report/ReportView.tsx",
    "src/notifications/useNotificationCenter.ts",
    "src/notifications/NotificationCenter.tsx",
    "src/profile/ProfilePanel.tsx",
    "src/auth/AuthModal.tsx",
]

# 拆分后仍必须成立的旧不变量：文件 → 必须出现的源码事实
PRESERVED_INVARIANTS = {
    "src/interview/InterviewRoom.tsx": [
        'data-testid="interview-locked"',          # T-42 锁定面板
        'data-testid="interview-countdown"',       # T-42 倒计时（按死线渲染）
        "formatCountdown(",                        # 不是逐秒自减
        "resetInterviewFlow",                      # 锁定后可重开
        "endInterviewRef.current()",               # 出报告走 ref 拿最新回调
    ],
    "src/interview/useInterviewTimeout.ts": [
        "/api/interview/session",                  # 权威死线来源
        "syncDeadlineFromServer",
        "visibilitychange",                        # 挂起恢复后重新校正
        "remainingSeconds(",
        "resolveDeadline(",
        "setInterviewLocked(false)",               # 锁定态的清理入口（T-42）
        "persistent: true",                        # 超时 Toast 不允许自动消失
    ],
    "src/interview/useInterviewChat.ts": [
        "isTimeoutResponse(",                      # 两条写路径都要识别 409
        "interviewLocked",                         # 写入口在锁定态关闭
        "endInterviewRef.current()",
    ],
    "src/interview/useInterviewSession.ts": [
        "/api/interview/config",                   # 跳过词/时长的唯一来源（T-13 / T-42）
        "duration_seconds",
        "armInterviewDeadline(",
        "messages.length === 0 && !timeout.interviewLocked",   # 零作答超时也能出报告
        "endInterviewRef.current = endInterview",  # T-42 / Bug 3A
        "const logout = () => {",
        "signOut();",
        "resetTimeoutState()",
        "setToast(null)",
    ],
    "src/report/ReportView.tsx": [
        'data-testid="report-timeout-note"',       # T-43 标注
        "isTimeoutReport(report)",
        "TIMEOUT_REPORT_NOTE",
    ],
    "src/App.tsx": [
        "useAuth()",                               # T-44 认证唯一真源
        "navigate('/admin')",
        '<Navigate to="/admin" replace />',
        "<ToastHost",
        "<InterviewRoom",
        "<ReportView",
        "<NotificationCenter",
        "<ProfilePanel",
        "<AuthModal",
        "<QuestionBankModal",
    ],
}

# App.tsx 里**不允许**再出现的实现痕迹（拆分前的它全都有）
NOT_IN_APP = [
    (r"const sendMessage\s*=", "sendMessage 实现"),
    (r"const skipQuestion\s*=", "skipQuestion 实现"),
    (r"const startInterview\s*=", "startInterview 实现"),
    (r"const endInterview\s*=", "endInterview 实现"),
    (r"const handleResumeFile\s*=", "简历上传实现"),
    (r"const handleAuth\s*=", "登录/注册实现"),
    (r"const loadCaptcha\s*=", "验证码实现"),
    (r"const handleChangePassword\s*=", "改密实现"),
    (r"const loadHistory\s*=", "面试历史取数"),
    (r"const markAllRead\s*=", "通知已读实现"),
    (r'data-testid="interview-locked"', "锁定面板 JSX"),
    (r'data-testid="report-timeout-note"', "报告页 JSX"),
    (r'data-testid="interview-countdown"', "倒计时 JSX"),
    (r"setTimeLeft\(\s*15\s*\*\s*60\s*\)", "硬编码面试时长"),
]

# 单一所有者：这些"状态/行为"只允许在一个文件里被定义
SINGLE_OWNERS = {
    "const [interviewLocked, setInterviewLocked]": "interview/useInterviewTimeout.ts",
    "const [timeLeft, setTimeLeft]": "interview/useInterviewTimeout.ts",
    "const endInterviewRef = useRef": "interview/useInterviewSession.ts",
    "const sendMessage = async () => {": "interview/useInterviewChat.ts",
    "const skipQuestion = async () => {": "interview/useInterviewChat.ts",
}

# 接口归属：每个后端接口只允许出现在一个前端文件里（拆分不许产生重复请求）
ENDPOINT_OWNERS = {
    "/api/chat": "interview/useInterviewChat.ts",
    "/api/skip_question": "interview/useInterviewChat.ts",
    "/api/generate_report": "interview/useInterviewSession.ts",
    "/api/start_interview": "interview/useInterviewSession.ts",
    "/api/resume/upload": "interview/resumeUpload.ts",
    "/api/interview/config": "interview/useInterviewSession.ts",
    "/api/change_password": "profile/ProfilePanel.tsx",
    "/api/login": "auth/AuthModal.tsx",
    "/api/notifications/read_all": "notifications/useNotificationCenter.ts",
    "/api/history_item": "notifications/useNotificationCenter.ts",
}

# A 段必须出现过的测试名（防止测试被改名/删空）
REQUIRED_TEST_NAMES = [
    "T-46/T-47：App.tsx 已降为路由/页面装配",
    "T-45~T-47：全树单文件都在 ≤400 行预算内",
    "判别力：拆分前的 App.tsx 形状必须被判为\"不是装配层\"",
    "T-46：面试状态只有一处声明",
    "T-46/T-47：每个 API 调用只有一个归属文件",
    "T-47：App.tsx 只做装配 —— 报告屏与面试屏分别由 <ReportView> / <InterviewRoom> 承载",
]

LINE_BUDGET = 400

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t44_t45_manual.py 同风格）
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    """让中文在 GBK 控制台上也能打出来（编码不了的字符降级成 ?，绝不抛异常崩脚本）。"""
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
    """执行命令并返回 (returncode, combined_output)；执行不了返回 (None, 原因)。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return None, "命令不存在：%s" % exc
    except subprocess.TimeoutExpired:
        return None, "超时（%ss）" % timeout
    except OSError as exc:
        return None, "无法启动进程：%s" % exc
    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    return proc.returncode, out


def find_node():
    for name in ("node", "node.exe"):
        code, out = run([name, "--version"], cwd=REPO_DIR, timeout=60)
        if code == 0:
            return name, out.strip()
    return None, None


def read_src(rel):
    with open(os.path.join(FRONTEND_DIR, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def lines_of(text):
    return len(text.split("\n"))


def code_only(text):
    """去掉块注释与整行注释：注释里会**引用**旧代码，不该被当成"仍在用"。"""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(re.sub(r"\s//.*$", "", line))
    return "\n".join(out)


def walk_sources(root=SRC_DIR):
    """收集 src 下全部 .ts/.tsx：相对 src 的路径 → 源码。"""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8") as fh:
                found[rel] = fh.read()
    return found


# ---------------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------------

def segment_preflight():
    section("0. 预检（node / 目标文件 / node_modules / 测试目录）")

    node_path, node_version = find_node()
    if node_path:
        ok("node 可执行：%s（%s）" % (node_path, node_version))
    else:
        env_problem("找不到 node 可执行文件（A 段无法运行）")

    missing = [rel for rel in DELIVERABLES if not os.path.exists(os.path.join(FRONTEND_DIR, rel))]
    if missing:
        bad("T-46 / T-47 的交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个目标文件全部就位（面谈/报告/通知/资料/登录/题库）" % len(DELIVERABLES))

    modules = os.path.join(FRONTEND_DIR, "node_modules")
    if os.path.isdir(modules):
        ok("node_modules 存在（契约测试与 tsc 可用）")
    else:
        env_problem("缺少 frontend/node_modules —— 请先 `cd frontend; npm install`")

    tests = [f for f in os.listdir(TESTS_DIR) if f.endswith(".test.mjs")] if os.path.isdir(TESTS_DIR) else []
    if tests:
        ok("契约测试文件 %d 个：%s" % (len(tests), "、".join(sorted(tests))))
    else:
        env_problem("tests/ 下没有 *.test.mjs（A 段会跑空）")

    return node_path


# ---------------------------------------------------------------------------
# A. 前端契约测试
# ---------------------------------------------------------------------------

def segment_contract_tests(node_path):
    section("A. 前端契约测试（node --test，零依赖、不联网）")
    if not node_path:
        env_problem("没有 node，A 段跳过")
        SEG["A"] = None
        return

    code, out = run(
        [
            node_path,
            "--test",
            "--experimental-test-isolation=none",
            # TAP：机器可读、纯 ASCII（默认 spec 报告器会打 ⓘ 等字符，
            # 在 GBK 控制台上直接把脚本打崩 —— 这个坑已经踩过一次）。
            "--test-reporter=tap",
            "tests/**/*.test.mjs",
        ],
        timeout=600,
    )
    if code is None:
        env_problem("契约测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
        return

    lines = [l.strip() for l in out.splitlines() if l.strip()]
    say("  —— `node --test`（TAP）尾部 5 行 ——")
    for line in lines[-5:]:
        say("     %s" % line.encode("ascii", "replace").decode("ascii"))

    m_tests = re.search(r"^#\s*tests\s+(\d+)", out, re.M)
    m_pass = re.search(r"^#\s*pass\s+(\d+)", out, re.M)
    m_fail = re.search(r"^#\s*fail\s+(\d+)", out, re.M)
    if not (m_tests and m_pass and m_fail):
        env_problem("未能从 TAP 输出里解析出 tests/pass/fail（输出格式可能变了）")
        SEG["A"] = False
        return

    tests, passed, failed = int(m_tests.group(1)), int(m_pass.group(1)), int(m_fail.group(1))
    say("  解析结果：tests=%d pass=%d fail=%d" % (tests, passed, failed))

    if failed == 0:
        ok("全部契约测试通过（pass=%d fail=0）" % passed)
    else:
        bad("有 %d 项契约测试失败（完整输出请直接跑 npm run test:node）" % failed)

    if tests >= 55:
        ok("测试数量合理（%d ≥ 55，说明既有契约测试没被删）" % tests)
    else:
        bad("只跑了 %d 项测试（预期 ≥ 55）—— 测试文件可能被删或没被匹配到" % tests)

    for name in REQUIRED_TEST_NAMES:
        if name in out:
            ok("点名用例确实跑过：%s" % name)
        else:
            bad("输出里没有出现点名用例「%s」—— 它可能被删/改名，那样就是假通过" % name)

    SEG["A"] = (failed == 0)


# ---------------------------------------------------------------------------
# B. TypeScript 类型检查
# ---------------------------------------------------------------------------

def segment_tsc(node_path, args):
    section("B. TypeScript 全量类型检查（tsc -b，跨 13 个新文件 + App.tsx 重接线）")
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
        ok("tsc -b 退出码 0：拆分后的类型一致性成立")
        SEG["B"] = True
        return

    for line in out.splitlines()[:15]:
        say("     %s" % line.strip())
    bad("tsc -b 失败（exit=%s）—— 上面是前 15 行错误" % code)
    SEG["B"] = False


# ---------------------------------------------------------------------------
# C. 独立复核
# ---------------------------------------------------------------------------

def judge_assembly(app_source):
    """判定"App.tsx 是否只是装配层"。

    返回 (violations, missing_pieces)：
      violations —— 仍在 App.tsx 里的实现痕迹（拆分前全都有）
      missing_pieces —— 少装配了哪一屏
    """
    code = code_only(app_source)
    violations = [label for pattern, label in NOT_IN_APP if re.search(pattern, code)]
    missing = [
        needle
        for needle in (
            "<InterviewRoom", "<ReportView", "<NotificationCenter",
            "<ProfilePanel", "<AuthModal", "<QuestionBankModal",
        )
        if needle not in code
    ]
    return violations, missing


def over_budget(sources, budget=LINE_BUDGET):
    """返回超出预算的文件名列表（可复用 → 下面用它做判别力自检）。"""
    return ["%s(%d 行)" % (name, lines_of(src)) for name, src in sources.items() if lines_of(src) > budget]


def segment_source_recheck(node_path):
    section("C. 独立复核（本脚本自己扫源码，不复用测试里的辅助函数）")
    fail_before = len(FAILED)

    sources = walk_sources()
    if len(sources) < 25:
        bad("只扫到 %d 个源文件 —— 收集器可能失效（预期 ≥25）" % len(sources))
        SEG["C"] = False
        return
    ok("扫描口径：src 下 %d 个 .ts/.tsx" % len(sources))

    app = sources.get("App.tsx")
    if app is None:
        bad("找不到 src/App.tsx")
        SEG["C"] = False
        return

    # ① 单文件 ≤400 行（T-45~T-47 的硬指标）
    over = over_budget(sources)
    if over:
        bad("① 以下文件超出 ≤400 行预算：%s" % "、".join(over))
    else:
        biggest = max(sources.items(), key=lambda kv: lines_of(kv[1]))
        ok("① 全树单文件都在 ≤400 行预算内（最大 %s，%d 行）" % (biggest[0], lines_of(biggest[1])))

    # 判别力自检：把"超预算"喂进同一个函数必须被抓到
    if over_budget({"synthetic/TooBig.tsx": "x\n" * (LINE_BUDGET + 1)}):
        ok("① 判别力自检：超预算的合成文件被判为超预算（说明上面的 PASS 不是恒真）")
    else:
        bad("① 判别力自检失败：超预算的合成文件居然通过了行数判定")

    # ② App.tsx 降为装配层
    app_lines = lines_of(app)
    violations, missing = judge_assembly(app)
    if app_lines <= LINE_BUDGET:
        ok("② App.tsx %d 行（T-46 前 2189 行 → 现在只剩装配）" % app_lines)
    else:
        bad("② App.tsx 仍有 %d 行，超出 ≤400 预算" % app_lines)
    if violations:
        bad("② App.tsx 里仍残留实现：%s" % "、".join(violations))
    else:
        ok("② App.tsx 里没有残留任何一屏的实现（14 项实现痕迹全为 0）")
    if missing:
        bad("② App.tsx 缺少装配：%s" % "、".join(missing))
    else:
        ok("② App.tsx 装配了全部 6 个视图/弹窗（面试室/报告/通知/资料/登录/题库）")

    # 判别力自检：拆分前的形状必须被判为"不是装配层"
    before = (
        "function App() {\n"
        "  const sendMessage = async () => {};\n"
        "  const startInterview = async () => {};\n"
        "  const endInterview = async () => {};\n"
        "  return <div data-testid=\"interview-locked\"><textarea data-testid=\"interview-countdown\" /></div>;\n"
        "}\n"
    )
    b_violations, b_missing = judge_assembly(before)
    if b_violations and b_missing:
        ok("② 判别力自检：拆分前的写法被判为「仍内联实现且没装配子视图」（判定不是恒真）")
    else:
        bad("② 判别力自检失败：拆分前的写法居然被判成装配层")

    # ③ 交付物存在 + 非空
    empty = [rel for rel in DELIVERABLES if not sources.get(rel.split("src/", 1)[1], "").strip()]
    if empty:
        bad("③ 这些交付物是空的：%s" % "、".join(empty))
    else:
        ok("③ 13 个交付物全部非空")

    # ④ 单一所有者
    owner_problems = []
    for needle, expected in SINGLE_OWNERS.items():
        holders = [name for name, src in sources.items() if needle in src]
        if holders != [expected]:
            owner_problems.append("%s → 实际在 %s（预期只有 %s）" % (needle, holders or "无", expected))
    if owner_problems:
        bad("④ 状态/行为出现「两份真源」：%s" % "；".join(owner_problems))
    else:
        ok("④ 锁定态/倒计时/endInterview ref/sendMessage/skipQuestion 各自只有一个所有者")

    # ⑤ 接口归属
    endpoint_problems = []
    for endpoint, expected in ENDPOINT_OWNERS.items():
        holders = sorted(name for name, src in sources.items() if endpoint in code_only(src))
        if holders != [expected]:
            endpoint_problems.append("%s → %s（预期 %s）" % (endpoint, holders or "无", expected))
    if endpoint_problems:
        bad("⑤ 接口出现重复归属：%s" % "；".join(endpoint_problems))
    else:
        ok("⑤ 10 个后端接口各自只有一个调用方（拆分没有产生重复请求）")

    # ⑥ 无反向依赖：除了 main.tsx，没人 import App.tsx
    offenders = [
        name for name, src in sources.items()
        if name != "main.tsx" and re.search(r"from\s+'\.\.?/App(\.tsx)?'", code_only(src))
    ]
    if offenders:
        bad("⑥ 这些模块反向 import 了 App.tsx（依赖成环）：%s" % "、".join(offenders))
    else:
        ok("⑥ 依赖方向单向：只有 main.tsx import App.tsx")

    # ⑦ 旧不变量随代码一起搬走（这是"拆分不改行为"的核心取证）
    invariant_problems = []
    for rel, needles in PRESERVED_INVARIANTS.items():
        rel_key = rel.split("src/", 1)[1]
        src = code_only(sources.get(rel_key, ""))
        if not src:
            invariant_problems.append("%s（文件缺失）" % rel)
            continue
        for needle in needles:
            if needle not in src:
                invariant_problems.append("%s 缺 `%s`" % (rel, needle))
    if invariant_problems:
        bad("⑦ 旧不变量在搬家后丢失：%s" % "；".join(invariant_problems))
    else:
        total = sum(len(v) for v in PRESERVED_INVARIANTS.values())
        ok("⑦ %d 条 T-42 / T-43 / T-44 不变量在拆分后全部仍在（超时锁定、报告标注、认证真源、路由跳转）" % total)

    # ⑧ 视图层不发请求
    view_files = ["interview/InterviewRoom.tsx", "report/ReportView.tsx", "notifications/NotificationCenter.tsx"]
    dirty = [
        name for name in view_files
        if re.search(r"await\s+(authFetch|fetch)\(", code_only(sources.get(name, "")))
    ]
    if dirty:
        bad("⑧ 这些视图文件里出现了请求（视图层应只渲染）：%s" % "、".join(dirty))
    else:
        ok("⑧ 面试室/报告页/消息中心的请求全部下沉到 hook（视图层 0 处 fetch）")

    # ⑨ lint 结构性不变量（lint 本来就是红的，这里只守"没有新增结构性违规"）
    check_lint_invariants(sources, node_path)

    # 本段的结论只看"这一段的 fail 数有没有增加"，不被 A / B 段的历史失败污染。
    SEG["C"] = (len(FAILED) == fail_before)


# 这三条是"拆分最容易踩、且一定是真 bug"的结构性 Hook 规则。
# 其余规则（no-explicit-any / set-state-in-effect …）在 HEAD 上就是红的，
# 属于既有技术债，本段不拿它们当交付门槛 —— 只报告数字。
STRUCTURAL_RULES = ("react-hooks/refs", "react-hooks/immutability", "react-hooks/purity")


def run_split(cmd, cwd, timeout=900):
    """分别捕获 stdout / stderr（eslint 的 JSON 只在 stdout，混进 stderr 会解析失败）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8-sig", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def check_lint_invariants(sources, node_path):
    """用**仓库自己的 eslint**守结构性规则；顺手报告整体红/绿（不作为门槛）。"""
    app = code_only(sources.get("App.tsx", ""))
    if re.search(r"if\s*\(isAdmin\)[\s\S]*?return[\s\S]*?\n\s*(use[A-Z])", app):
        bad("⑨ App.tsx 的 `if (isAdmin) return` 之后还有 Hook —— FR-11.3 条件 Hook 会复发")
    else:
        ok("⑨ App.tsx 的早退（isAdmin → /admin）在所有 Hook 之后，FR-11.3 不会复发")

    eslint_js = os.path.join(FRONTEND_DIR, "node_modules", "eslint", "bin", "eslint.js")
    if not node_path or not os.path.exists(eslint_js):
        env_problem("找不到 eslint，跳过「结构性 Hook 规则」复核")
        return

    code, out, err = run_split([node_path, eslint_js, ".", "-f", "json"], FRONTEND_DIR)
    if code is None:
        env_problem("eslint 无法执行：%s" % err.strip()[:200])
        return
    try:
        results = json.loads(out)
    except Exception as exc:  # noqa: BLE001 - 解析失败就是环境问题，不该让脚本崩
        env_problem("eslint JSON 解析失败（%s）" % exc)
        return

    counts, errors, warnings = {}, 0, 0
    for item in results:
        errors += item.get("errorCount", 0)
        warnings += item.get("warningCount", 0)
        for msg in item.get("messages", []):
            key = msg.get("ruleId") or "<parse-error>"
            counts[key] = counts.get(key, 0) + 1

    hits = {rule: counts.get(rule, 0) for rule in STRUCTURAL_RULES}
    if any(hits.values()):
        bad("⑨ T-46 / T-47 引入了结构性 Hook 违规：%s" % hits)
    else:
        ok("⑨ 结构性 Hook 规则全为 0（%s）—— 拆分没有写坏 Hook" % "、".join(STRUCTURAL_RULES))
    say("     eslint 现状（**不作为门槛**，HEAD 上就是红的）：%d errors / %d warnings；Top 规则 %s"
        % (errors, warnings, sorted(counts.items(), key=lambda kv: -kv[1])[:3]))


# ---------------------------------------------------------------------------
# D. 构建探测（默认跳过）
# ---------------------------------------------------------------------------

def segment_build(node_path, args):
    section("D. 构建探测（vite build，默认跳过）")
    if not args.with_build:
        skip("默认不跑（受限沙箱里 spawn 走管道的子进程会 EPERM，见文件头 §边界）")
        SEG["D"] = None
        return
    vite_js = os.path.join(FRONTEND_DIR, "node_modules", "vite", "bin", "vite.js")
    if not node_path or not os.path.exists(vite_js):
        env_problem("找不到 node_modules/vite/bin/vite.js，跳过 D 段")
        SEG["D"] = None
        return

    code, out = run([node_path, vite_js, "build"], timeout=1800)
    if code is None:
        env_problem("vite build 无法执行：%s" % out.strip()[:200])
        SEG["D"] = None
        return
    if "EPERM" in out or "spawn" in out and "EPERM" in out:
        env_problem("vite build 撞上沙箱边界（spawn EPERM）—— 与 docs/29 §3、docs/30 §4 同因，非本次改动问题")
        SEG["D"] = None
        return
    if code == 0:
        ok("vite build 成功（产物在 frontend/dist）")
        SEG["D"] = True
        return
    for line in out.splitlines()[:12]:
        say("     %s" % line.strip())
    bad("vite build 失败（exit=%s）" % code)
    SEG["D"] = False


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    labels = [("A", "契约测试"), ("B", "类型检查"), ("C", "独立复核"), ("D", "构建探测")]
    parts = []
    for key, _label in labels:
        val = SEG.get(key)
        if val is True:
            mark = "PASS"
        elif val is False:
            mark = "FAIL"
        elif key in ("B", "D") and ENV_PROBLEMS:
            mark = "ENV"
        else:
            mark = "SKIP"
        parts.append("[%s] %s" % (mark, key))
    say("  " + " / ".join(parts))

    if ENV_PROBLEMS:
        say("")
        say("  环境提示（不算失败，但会让结论不完整）：")
        for item in ENV_PROBLEMS:
            say("    - %s" % item)

    if FAILED:
        say("")
        say("  未通过项：")
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-46 / T-47 人工验收未通过")
        return 1

    say("")
    say("✅ T-46 / T-47 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-46 / T-47 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-tsc", action="store_true", help="跳过 TypeScript 类型检查")
    parser.add_argument("--with-build", action="store_true", help="额外尝试 vite build")
    args = parser.parse_args()

    say("T-46 / T-47 人工验收：面谈主流程 / 报告 / 个人中心 / 通知中心 / 题库拆分")
    say("仓库：%s" % REPO_DIR)

    node_path = segment_preflight()
    segment_contract_tests(node_path)
    segment_tsc(node_path, args)
    segment_source_recheck(node_path)
    segment_build(node_path, args)

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and (SEG.get("A") is None or SEG.get("B") is None):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

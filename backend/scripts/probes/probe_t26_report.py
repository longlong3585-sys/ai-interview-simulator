"""T-26 破坏性验证：把"评分输入退回客户端"的各种变体逐个注入，看测试是否抓得住。

T-26 的契约只有一句话：**评分输入只能来自服务端会话**。
但这一句话背后有好几种"看起来还行、其实已经退回客户端语义"的退化方式，
所以逐个注入，每一种都必须让 `tests.test_report_from_session` 变红：

  P1  未作答的题不再如实标注（当作有内容 -> 等于替候选人编造回答）
  P2  跳过与未作答混为一谈（"跳过"被算成"没来得及答"，评分口径失真）
  P3  丢掉 question_status（状态不下发给评分 -> ADR-007R 的前提没了）
  P4  没有会话也照样出报告（白嫖路径复活，且输入只能来自客户端）
  P5  题量写死（报告与真实面试轮数脱节 —— Bug 3A 的表现之一）
  P6  评分成功却不落库/不置终态（用户被锁到 TTL 结束，ADR-022R 回归）
  P7  AI 失败也置终态（用户失去重试机会）

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：运行前另存原始副本（不依赖 git），
并用状态文件检测"上次未正常收尾"。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t26_report.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTER = os.path.join(ROOT, "routers", "interview.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = ["tests.test_report_from_session"]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t26-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t26-backup")

PROBES = [
    ("P1  未作答的题不再标注（替候选人编造回答）",
     "        elif status == \"pending\" or not answer:",
     "        elif False:"),

    ("P2  跳过与未作答混为一谈（评分口径失真）",
     "        if status == \"skipped\":",
     "        if False:"),

    ("P3  丢掉 question_status（状态不下发给评分）",
     "        status = (snapshot.question_status[i]\n"
     "                  if i < len(snapshot.question_status) else \"pending\")",
     "        status = \"answered\""),

    ("P4  没有会话也照样出报告（白嫖路径复活）",
     "    前端不再有机会影响评分输入。\n"
     "    \"\"\"\n"
     "    snapshot = _active_session(current_user.id)\n"
     "    if snapshot is None:",
     "    前端不再有机会影响评分输入。\n"
     "    \"\"\"\n"
     "    snapshot = _active_session(current_user.id)\n"
     "    if snapshot is None:\n"
     "        return {\"expression_score\": 9, \"technical_score\": 9,\n"
     "                \"logic_score\": 9, \"overall_score\": 9.9,\n"
     "                \"answered_count\": 9, \"total_questions\": 9,\n"
     "                \"suggestion\": \"凭空生成的报告（白嫖路径复活）\",\n"
     "                \"details\": \"\"}"),

    ("P5  题量写死（报告与真实面试轮数脱节）",
     "    return \"\\n\".join(lines), len(snapshot.questions)",
     "    return \"\\n\".join(lines), 5"),

    ("P6  评分成功却不置终态（用户被锁到 TTL 结束）",
     "        _finish_session(snapshot, result)",
     "        pass"),

    ("P7  AI 失败也置终态（用户失去重试机会）",
     "    except Exception as e:\n"
     "        return {\n"
     "            \"expression_score\": 0,",
     "    except Exception as e:\n"
     "        _finish_session(snapshot, {\"error\": str(e)})\n"
     "        return {\n"
     "            \"expression_score\": 0,"),
]


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fit(text, sample):
    """把探针里写的换行符对齐到目标文件**真实**的换行符。

    探针的 needle 一律用 `\\n` 书写；而仓库文件可能是 CRLF（Windows 上
    `core.autocrlf` 的默认结果）。`read_text` 刻意用 `newline=""` 保留原样，
    所以多行 needle 必须在这里补齐 —— 否则会出现"探针无效（命中 0 次）"
    这种**看起来像代码坏了、其实是探针写错了**的假警报。
    """
    if "\r\n" in sample:
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def _cleanup_state():
    try:
        if os.path.exists(_STATE):
            os.remove(_STATE)
    except OSError:
        pass


def _check_previous_run(targets):
    if not os.path.exists(_STATE):
        return True
    try:
        with open(_STATE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (ValueError, OSError):
        saved = {}
    dirty = [p for p, before in saved.items()
             if os.path.exists(p) and sha(read_text(p)) != before]
    if not dirty:
        os.remove(_STATE)
        return True
    print("!! 检测到上一次探针**未正常收尾**，工作区可能仍带着注入的缺陷：")
    for path in dirty:
        print("     %s" % path)
    print("")
    print("   请先恢复（探针运行前自动留的原始副本）：")
    for path in dirty:
        print('     Copy-Item "%s" "%s" -Force'
              % (os.path.join(_BACKUP_DIR, os.path.basename(path)), path))
    print("")
    print("   恢复后删除状态文件再重跑：Remove-Item \"%s\"" % _STATE)
    return False


def _save_backups(originals):
    shutil.rmtree(_BACKUP_DIR, ignore_errors=True)
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    for path, text in originals.items():
        with open(os.path.join(_BACKUP_DIR, os.path.basename(path)),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    with open(_STATE, "w", encoding="utf-8") as fh:
        json.dump({p: sha(t) for p, t in originals.items()}, fh, indent=2)


def run_tests(timeout=180):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([PY, "-m", "unittest"] + TEST_MODULES,
                              cwd=ROOT, capture_output=True, env=env,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT（>%ds）" % timeout, ["<超时>"]
    text = (proc.stdout or b"").decode("utf-8", "replace") + \
           (proc.stderr or b"").decode("utf-8", "replace")
    verdict = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith(("OK", "FAILED"))]
    failing = sorted({
        ln.split(" (")[0].replace("FAIL: ", "").replace("ERROR: ", "").strip()
        for ln in text.splitlines()
        if ln.startswith(("FAIL: ", "ERROR: "))
    })
    return proc.returncode, (verdict[-1] if verdict else "(无结论行)"), failing


def main():
    targets = [ROUTER]
    if not _check_previous_run(targets):
        return 2

    originals = {p: read_text(p) for p in targets}
    _save_backups(originals)
    print("运行前已备份原始副本到: %s" % _BACKUP_DIR)
    for path in targets:
        print("%s sha256 BEFORE = %s" % (os.path.basename(path), sha(originals[path])))
    print("")
    print("=" * 74)
    print("BASELINE（无缺陷）")
    print("=" * 74)
    rc, line, _ = run_tests()
    print("  exit=%s  %s" % (rc, line))
    if rc != 0:
        print("  !! 基线未通过，后续探针结论无意义")
        _cleanup_state()
        return 1

    results = []
    try:
        for name, needle, replacement in PROBES:
            print("")
            print("=" * 74)
            print(name)
            print("=" * 74)
            for path, text in originals.items():
                write_text(path, text)

            needle_f = _fit(needle, originals[ROUTER])
            repl_f = _fit(replacement, originals[ROUTER])
            occurrences = originals[ROUTER].count(needle_f)
            if occurrences != 1:
                print("  !! 目标字符串命中 %d 次（应为 1），探针无效" % occurrences)
                results.append((name, "INVALID", []))
                continue

            write_text(ROUTER,
                       originals[ROUTER].replace(needle_f, repl_f, 1))
            rc, line, failing = run_tests()
            status = "CAUGHT" if rc != 0 else "MISSED"
            print("  exit=%s  %s  -> %s" % (rc, line, status))
            for f in failing[:5]:
                print("     失败用例: %s" % f)
            results.append((name, status, failing))
    finally:
        for path, text in originals.items():
            write_text(path, text)
        _cleanup_state()

    print("")
    print("=" * 74)
    print("RESTORED（还原后复测）")
    print("=" * 74)
    rc, line, _ = run_tests()
    print("  exit=%s  %s" % (rc, line))

    print("")
    print("=" * 74)
    print("SUMMARY")
    print("=" * 74)
    for name, status, failing in results:
        print("  %-8s %-54s 失败用例数=%d" % (status, name, len(failing)))
    print("")
    print("  restored exit=%s（应为 0）" % rc)
    for path in targets:
        print("  %s sha256 AFTER = %s" % (os.path.basename(path), sha(read_text(path))))

    missed = [r for r in results if r[1] != "CAUGHT"]
    ok = (not missed) and rc == 0
    print("")
    print("  结论: %s" % ("全部探针被抓住，且已还原 [OK]" if ok
                          else "存在漏网探针或还原失败 [BAD]"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

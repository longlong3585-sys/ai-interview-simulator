"""T-27 破坏性验证：把"未及作答当成答不上"的各种退化逐个注入。

T-27 要修的是 FR-5.2 的"**误导性 0 分**"：一个因为**超时**一道题都没答上的
候选人，拿到的报告却和"全部答错"一模一样。这个 Bug 可以由很多种写法复现，
所以逐个注入，每一种都必须让 `tests.test_report_timeout` 变红：

  P1  `_report_target` 不再回退到"最近结束的一场"（超时后 409，什么都拿不到）
  P2  `attach_report` 顺手把状态改成 `finished`（状态语义被报告路径污染）
  P3  去掉 `report IS NULL` 守卫（重复计费 + 覆盖用户已经看过的结论）
  P4  零作答超时的"未及作答，无法评分"标注被去掉（退回"只靠提示词"）
  P5  标注条件放宽成 `answered==0`（把"全部主动跳过"也标成"未及作答" —— 反向误导）
  P6  `ended_reason` 不再由服务端裁决（模型/客户端说了算）
  P7  计数不再由服务端裁决（报告里的题数由模型编）
  P8  提示词退回旧的无条件"全 0"规则（Bug 的原始形态）
  P9  未及作答那条规则被改写成"一律按 0 分计入"
  P10 主动跳过那条规则被改写成"不计入扣分"（与 pending 混为一谈）

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：运行前另存原始副本（不依赖 git），
并用状态文件检测"上次未正常收尾"。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t27_report.py
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
STORE = os.path.join(ROOT, "services", "stores", "sqlite_store.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

#: P3（`report IS NULL` 防覆盖守卫）的判别用例在存储层测试里 —— 只跑路由层的
#: `test_report_timeout` 会让它"漏网"，而漏网的其实是**探针没选对模块**。
TEST_MODULES = ["tests.test_report_timeout", "tests.test_sqlite_session_store"]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t27-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t27-backup")

#: (名称, 目标文件, needle, replacement)
PROBES = [
    ("P1  超时后不再回退找会话（超时永远拿不到报告）",
     ROUTER,
     "    last = get_session_store().get_last_ended(user_id)\n"
     "    if last is None or last.report:",
     "    last = None\n"
     "    if last is None or last.report:"),

    ("P2  写报告时顺手把状态改成 finished（ADR-022R 被违反）",
     STORE,
     "                \"SET report = :report, version = version + 1, updated_at = :now \"",
     "                \"SET report = :report, status = 'finished', \"\n"
     "                \"version = version + 1, updated_at = :now \""),

    ("P3  去掉 report IS NULL 守卫（重复计费 + 覆盖已有结论）",
     STORE,
     "                \"  AND status = :abandoned AND report IS NULL\",",
     "                \"  AND status = :abandoned\","),

    ("P4  去掉零作答超时的标注兜底（退回「只靠提示词」）",
     ROUTER,
     "        marker = \"未及作答，无法评分\"",
     "        marker = \"（注入：不标注）\""),

    ("P5  标注条件放宽成 answered==0（把主动跳过也说成未及作答）",
     ROUTER,
     "            and not counts[\"skipped\"]:",
     "            and True:"),

    ("P6  ended_reason 不再由服务端裁决（模型/客户端说了算）",
     ROUTER,
     "    result[\"ended_reason\"] = ended_reason\n",
     ""),

    ("P7  计数不再由服务端裁决（题数由模型编）",
     ROUTER,
     "    result[\"answered_count\"] = counts[\"answered\"]\n",
     ""),

    ("P8  提示词退回旧的无条件「全 0」规则（Bug 的原始形态）",
     ROUTER,
     "### 给全 0 分的**唯一**条件（口径比以往更窄，必须按新口径执行）：\n"
     "- 只有当\"**真正问过**的题\"（= answered + skipped）**全部**属于无效回答\n"
     "  （我不会/不知道/没学过/没接触过/跳过/敷衍）时，才可以给全 0 分。\n"
     "- ⚠️ 只要存在 [尚未作答] 的题，就**不得**因为\"没答满\"而清零，也**不得**\n"
     "  把 [尚未作答] 当作无效回答去凑满\"全部无效\"这个条件。",
     "### 无法回答问题（得 0 分）：\n"
     "- 如果候选人对你提出的问题一个都没有给出有效回答"
     "（全是我不会/不知道/没学过/没接触过/跳过等），则所有分数均为 0。"),

    ("P9  未及作答那条规则被改成「一律按 0 分计入」",
     ROUTER,
     "  它属于「**未及作答**」，**不是**「答不上」，**不得计入扣分**，",
     "  它属于「**未及作答**」，但一律按 0 分计入，"),

    ("P10 主动跳过那条规则被改成「不计入扣分」（与 pending 混为一谈）",
     ROUTER,
     "  \"不会/不知道/没学过\"）。这属于「**答不上**」，**照常计分** ——",
     "  \"不会/不知道/没学过\"）。这同样算未及作答，**不计入扣分** ——"),
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
    """把探针里写的换行符对齐到目标文件**真实**的换行符（T-26 踩过的坑）。"""
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
    targets = [ROUTER, STORE]
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
        for name, target, needle, replacement in PROBES:
            print("")
            print("=" * 74)
            print(name)
            print("=" * 74)
            for path, text in originals.items():
                write_text(path, text)

            needle_f = _fit(needle, originals[target])
            repl_f = _fit(replacement, originals[target])
            occurrences = originals[target].count(needle_f)
            if occurrences != 1:
                print("  !! 目标字符串命中 %d 次（应为 1），探针无效" % occurrences)
                results.append((name, "INVALID", []))
                continue

            write_text(target, originals[target].replace(needle_f, repl_f, 1))
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

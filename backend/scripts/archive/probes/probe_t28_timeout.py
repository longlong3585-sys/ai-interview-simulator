"""T-28 破坏性验证：把"服务端不再掌握超时时刻"的各种退化逐个注入。

T-28 修的是 FR-4.12 / Bug 3B：**"15 分钟"原先只活在前端的倒计时里** ——
归零之后只要再手工发一次 `/api/chat`，服务端照样把这一轮记进会话。
修法的核心是"死线由服务端裁定"，而这件事可以被很多种写法悄悄毁掉，
所以逐个注入，每一种都必须让测试变红：

  P1  兜底整体失效（`_enforce_interview_timeout` 直接返回 None）
      —— 回到 Bug 3B 的原始形态：超时后还能继续答题
  P2  结束原因写成 `manual`（超时说成"候选人主动放弃"，评分口径被污染）
  P3  `/api/chat` 不再兜底（只拦住"重开"这条路，主流程照旧能被绕过）
  P4  `/api/generate_report` 不再兜底（超时面试被出报告路径判成 completed）
  P5  `start_interview` 不再释放业务死线（超时用户被锁在门外 —— ADR-022R 违规）
  P6  死线改用 `expires_at`（把 15 分钟业务规则与 2 小时 TTL 混成一个字段）
  P7  超时响应丢掉 `interview_timeout` 标识（FR-4.12 第③条要求的明确反馈没了）
  P8  `/api/interview/abandon` 不再先兜底（超时被记成"主动放弃"）
  P9  `GET /api/interview/session` 不再自愈（刷新时既不结算也不告诉前端原因）
  P10 `/api/skip_question` 不再兜底（跳过是另一条写路径，同样能被绕过）
  P11 `/api/interview/config` 不再下发时长（前端只能继续硬编码 15 分钟）

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：运行前另存原始副本（不依赖 git），
并用状态文件检测"上次未正常收尾"。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t28_timeout.py
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
BASE = os.path.join(ROOT, "services", "stores", "base.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

#: 兜底改动横跨"路由 + 数据模型"两层，两个模块都要跑：
#: 只跑 T-28 用例会让 `test_report_timeout` 这条 T-27 回归线失去交叉检查。
TEST_MODULES = ["tests.test_interview_timeout_guard", "tests.test_report_timeout"]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t28-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t28-backup")

#: (名称, 目标文件, needle, replacement)
PROBES = [
    ("P1  兜底整体失效（超时后仍能继续答题 —— Bug 3B 原形）",
     ROUTER,
     "    now = now or utcnow_iso()\n"
     "    store = get_session_store()\n"
     "    snapshot = store.get_active(user_id, now)",
     "    return None\n"
     "    now = now or utcnow_iso()\n"
     "    store = get_session_store()\n"
     "    snapshot = store.get_active(user_id, now)"),

    ("P2  结束原因写成 manual（超时被说成主动放弃）",
     ROUTER,
     "    result = store.abandon(snapshot.session_id, snapshot.version,\n"
     "                           EndedReason.TIMEOUT, now)\n"
     "    if result.applied:",
     "    result = store.abandon(snapshot.session_id, snapshot.version,\n"
     "                           EndedReason.MANUAL, now)\n"
     "    if result.applied:"),

    ("P3  /api/chat 不再兜底（主流程仍可被绕过）",
     ROUTER,
     "    timed_out = _enforce_interview_timeout(current_user.id)\n"
     "    if timed_out is not None:\n"
     "        return _timeout_response(timed_out)\n"
     "\n"
     "    snapshot = _active_session(current_user.id)",
     "    snapshot = _active_session(current_user.id)"),

    ("P4  /api/generate_report 不再兜底（超时说成 completed）",
     ROUTER,
     "    _enforce_interview_timeout(current_user.id)\n"
     "    target = _report_target(current_user.id)",
     "    target = _report_target(current_user.id)"),

    ("P5  start_interview 不再释放业务死线（用户被锁在门外）",
     ROUTER,
     "    timed_out = _enforce_interview_timeout(current_user.id, now)\n"
     "    if timed_out is not None:\n"
     "        logger.info(\"start_interview 前释放了超时会话 %s（user_id=%s）\",\n"
     "                    timed_out.session_id, current_user.id)\n",
     ""),

    ("P6  死线改用 expires_at（业务规则与 2 小时 TTL 混为一谈）",
     BASE,
     "            origin = datetime.fromisoformat(self.created_at)",
     "            origin = datetime.fromisoformat(self.expires_at)"),

    ("P7  超时响应丢掉 interview_timeout 标识（FR-4.12 的明确反馈没了）",
     ROUTER,
     "            \"code\": \"interview_timeout\",",
     "            \"code\": \"no_active_session\","),

    ("P8  /api/interview/abandon 不再先兜底（超时被记成主动放弃）",
     ROUTER,
     "    timed_out = _enforce_interview_timeout(current_user.id)\n"
     "    if timed_out is not None:\n"
     "        return {\n"
     "            \"success\": True,\n"
     "            \"abandoned_session_id\": timed_out.session_id,",
     "    timed_out = None\n"
     "    if timed_out is not None:\n"
     "        return {\n"
     "            \"success\": True,\n"
     "            \"abandoned_session_id\": timed_out.session_id,"),

    ("P9  GET /api/interview/session 不再自愈（刷新时不结算、不报原因）",
     ROUTER,
     "    _enforce_interview_timeout(current_user.id)\n"
     "    snapshot = _active_session(current_user.id)\n"
     "    ended = get_session_store().get_last_ended(current_user.id)",
     "    snapshot = _active_session(current_user.id)\n"
     "    ended = get_session_store().get_last_ended(current_user.id)"),

    ("P10 /api/skip_question 不再兜底（跳过也是写路径）",
     ROUTER,
     "    timed_out = _enforce_interview_timeout(user_id)\n"
     "    if timed_out is not None:\n"
     "        return _timeout_response(timed_out)\n"
     "\n"
     "    snapshot = _active_session(current_user.id)",
     "    snapshot = _active_session(current_user.id)"),

    ("P11 /api/interview/config 不再下发时长（前端继续硬编码 15 分钟）",
     ROUTER,
     "        \"skip_words\": list(SKIP_WORDS),\n"
     "        \"duration_seconds\": INTERVIEW_DURATION_SECONDS,\n",
     "        \"skip_words\": list(SKIP_WORDS),\n"),
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


def run_tests(timeout=300):
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
    targets = [ROUTER, BASE]
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
        print("  %-8s %-52s 失败用例数=%d" % (status, name, len(failing)))
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

"""T-22 破坏性验证：向清理脚本与 systemd 单元注入缺陷。

T-22 有三块产物，都要证明"测试真的有判别力"：
  * `scripts/cleanup.py`               单实例锁 + 三类清理 + CLI
  * `deploy/systemd/cleanup.service`   oneshot / 退出码 3 / 可写路径
  * `deploy/systemd/cleanup.timer`     Unit= 名字 / Persistent

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：运行前另存原始副本（不依赖 git），
并用状态文件检测"上次未正常收尾"。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t22_cleanup.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLEANUP = os.path.join(ROOT, "scripts", "cleanup.py")
SERVICE = os.path.join(ROOT, "..", "deploy", "systemd", "cleanup.service")
TIMER = os.path.join(ROOT, "..", "deploy", "systemd", "cleanup.timer")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = [
    "tests.test_cleanup",
    "tests.test_systemd_units",
]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t22-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t22-backup")

PROBES = [
    # ---------------- 单实例锁 ----------------
    ("L1  锁不再互斥（去掉 O_EXCL）",
     CLEANUP,
     "        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)",
     "        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)"),
    ("L2  去掉遗留锁抢占（崩溃后永久停摆）",
     CLEANUP,
     "        if age < stale_after:",
     "        if True:"),
    ("L3  release_lock 变成空操作（锁永不释放）",
     CLEANUP,
     "def release_lock(lock_path):\n    try:\n        os.remove(lock_path)",
     "def release_lock(lock_path):\n    try:\n        pass"),
    ("L4  失败路径不释放锁（异常后 1 小时跑不了）",
     CLEANUP,
     "    finally:\n        if held:\n            release_lock(lock_path)",
     "    finally:\n        if held and False:\n            release_lock(lock_path)"),

    # ---------------- 三类清理的判据 ----------------
    ("C1  会话判据多减一次 TTL（过期会话要再等 2 小时才释放锁）",
     CLEANUP,
     "                SQLiteSessionStore(session_factory).abandon_all_expired(now),",
     "                SQLiteSessionStore(session_factory).abandon_all_expired(\n"
     "                    iso_after(-2 * 60 * 60, now)),"),
    ("C2  验证码判据多减一次 TTL（过期验证码清不掉）",
     CLEANUP,
     "                SQLiteCaptchaStore(session_factory).purge_expired(now),",
     "                SQLiteCaptchaStore(session_factory).purge_expired(\n"
     "                    iso_after(-300, now)),"),
    ("C3  限流判据不减窗口（把窗口内的失败也删了 -> 限流被清零）",
     CLEANUP,
     "    attempt_cutoff = iso_after(-ATTEMPT_WINDOW_SECONDS, now)",
     "    attempt_cutoff = now"),
    ("C4  dry-run 真的去改数据（预览不再安全）",
     CLEANUP,
     "        if dry_run:\n            with engine.connect() as conn:",
     "        if False:\n            with engine.connect() as conn:"),

    # ---------------- CLI ----------------
    ("C5  已有实例在跑时返回 0（跳过被当成成功，监控失去意义）",
     CLEANUP,
     '            print("结果: 跳过（已有另一个实例在运行）")\n'
     "            return 3",
     '            print("结果: 跳过（已有另一个实例在运行）")\n'
     "            return 0"),
    ("C6  --db 失效（build_session_factory 忽略传入路径）",
     CLEANUP,
     '        "sqlite:///" + os.path.abspath(db_path).replace("\\\\", "/"),',
     '        "sqlite:///" + os.path.abspath(\n'
     '            os.path.join(os.path.dirname(os.path.abspath(__file__)),\n'
     '                         "..", "interview.db")).replace("\\\\", "/"),'),

    # ---------------- systemd ----------------
    ("S1  service 不再 oneshot（去掉第二层防重入）",
     SERVICE, "Type=oneshot", "Type=simple"),
    ("S2  service 不再把退出码 3 当成功（跳过被记成 failed）",
     SERVICE, "SuccessExitStatus=0 3", "SuccessExitStatus=0"),
    ("S3  ProtectSystem=strict 但删掉 ReadWritePaths（清理会因只读失败）",
     SERVICE, "ReadWritePaths=/opt/interview/backend\n", ""),
    ("T1  timer 的 Unit= 写错名字（静默不触发）",
     TIMER, "Unit=cleanup.service", "Unit=cleanup-svc.service"),
    ("T2  timer 不再补跑停机关闭期间错过的触发",
     TIMER, "Persistent=true", "Persistent=false"),
]


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def run_tests(timeout=90):
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
    targets = [CLEANUP, SERVICE, TIMER]
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

            occurrences = originals[target].count(needle)
            if occurrences != 1:
                print("  !! 目标字符串命中 %d 次（应为 1），探针无效" % occurrences)
                results.append((name, "INVALID", []))
                continue

            write_text(target, originals[target].replace(needle, replacement, 1))
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

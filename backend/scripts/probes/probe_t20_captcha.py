"""T-20 破坏性验证：向验证码存储注入缺陷。

覆盖任务的两个关键词（**TTL** / **一次性**）以及三条从原内存实现继承的语义
（输错不消费 / 输入去空白 / 成功置 used 而非删除），外加装配点自检。

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：
  * 运行前把目标文件原始内容另存到临时目录（不依赖 git）；
  * 写状态文件记录改动前的 sha256，下次启动发现不符就**拒绝运行** ——
    因为进程被强杀时 `finally` 不会执行，缺陷会永久留在工作区。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t20_captcha.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAPTCHA = os.path.join(ROOT, "services", "stores", "sqlite_captcha_store.py")
FACTORY = os.path.join(ROOT, "services", "stores", "factory.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = [
    "tests.test_sqlite_captcha_store",
    "tests.test_stores_protocol",
]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t20-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t20-backup")

VERIFY_UPDATE = (
    '"UPDATE captcha_store SET used = 1 "\n'
    '                    "WHERE captcha_id = :cid "\n'
    '                    "  AND used = 0 "\n'
    '                    "  AND expires_at > :now "\n'
    '                    "  AND code = :code"'
)

PROBES = [
    ("C1  成功时不置 used（一次性失效）",
     CAPTCHA,
     '"UPDATE captcha_store SET used = 1 "',
     '"UPDATE captcha_store SET used = used "'),
    ("C2  去掉 used = 0 条件（已用过的还能再用）",
     CAPTCHA, VERIFY_UPDATE,
     VERIFY_UPDATE.replace('"  AND used = 0 "\n', "")),
    ("C3  去掉过期判断（过期验证码仍然有效）",
     CAPTCHA, VERIFY_UPDATE,
     VERIFY_UPDATE.replace('"  AND expires_at > :now "\n', "")),
    ("C4  去掉验证码比对（任意码都通过）",
     CAPTCHA, VERIFY_UPDATE,
     VERIFY_UPDATE.replace('\n                    "  AND code = :code"', "")),
    ("C5  rowcount 判断改成恒真",
     CAPTCHA,
     "return (result.rowcount or 0) == 1",
     "return True"),
    ("C6  purge 边界从 <= 改成 <（与 verify 判据不再互补）",
     CAPTCHA,
     'text("DELETE FROM captcha_store WHERE expires_at <= :now")',
     'text("DELETE FROM captcha_store WHERE expires_at < :now")'),
    ("C7  不再去除输入首尾空白",
     CAPTCHA,
     'normalized = (code or "").strip()',
     'normalized = code or ""'),
    ("C8  save 改成 INSERT OR REPLACE（重复 id 被静默覆盖）",
     CAPTCHA,
     '"INSERT INTO captcha_store (captcha_id, code, expires_at, used) "',
     '"INSERT OR REPLACE INTO captcha_store (captcha_id, code, expires_at, used) "'),
    ("C9  空输入不再提前拒绝",
     CAPTCHA,
     "        if not captcha_id or not normalized:\n"
     "            # 空输入直接拒，不产生任何写入（省一次往返，也避免把空串当匹配）\n"
     "            return False\n",
     ""),
    ("F1  装配点不再做协议自检",
     FACTORY,
     "    if not isinstance(store, protocol):",
     "    if False:"),
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


def run_tests(timeout=60):
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
    targets = [CAPTCHA, FACTORY]
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
        print("  %-8s %-50s 失败用例数=%d" % (status, name, len(failing)))
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

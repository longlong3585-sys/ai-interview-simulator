"""T-21 破坏性验证：向限流存储 / IP 解析 / 装配点注入缺陷。

两块产物都要证明"测试真的有判别力"：
  * `services/stores/sqlite_rate_limit_store.py` —— 失败计数 / 滑动窗口
  * `utils/client_ip.py`                        —— X-Forwarded-For 信任链
  * `services/stores/factory.py`                —— 表驱动装配

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加两道保险（T-19 事故后加的）：
  * 运行前把目标文件原始内容另存到临时目录（不依赖 git —— 新增的未跟踪文件
    用 `git checkout` 救不回来）；
  * 写状态文件记录改动前的 sha256，下次启动发现不符就**拒绝运行**，
    因为进程被强杀时 `finally` 不会执行，缺陷会永久留在工作区。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t21_rate_limit.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(ROOT, "services", "stores", "sqlite_rate_limit_store.py")
CLIENT_IP = os.path.join(ROOT, "utils", "client_ip.py")
FACTORY = os.path.join(ROOT, "services", "stores", "factory.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = [
    "tests.test_sqlite_rate_limit_store",
    "tests.test_client_ip",
    "tests.test_stores_protocol",
]

_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t21-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t21-backup")

PROBES = [
    # ---------------- 限流存储 ----------------
    ("R1  count_failures 边界从 >= 改成 >",
     STORE,
     '"WHERE ip = :ip AND attempted_at >= :since"',
     '"WHERE ip = :ip AND attempted_at > :since"'),
    ("R2  count_failures 不再按窗口过滤（退化成全量计数）",
     STORE,
     '"SELECT count(*) FROM auth_attempts "\n'
     '                     "WHERE ip = :ip AND attempted_at >= :since"',
     '"SELECT count(*) FROM auth_attempts WHERE ip = :ip"'),
    ("R3  record_failure 不提交（跨 worker 看不见）",
     STORE,
     '                text("INSERT INTO auth_attempts (ip, attempted_at) VALUES (:ip, :at)"),\n'
     '                {"ip": ip, "at": at},\n'
     '            )\n'
     '            s.commit()',
     '                text("INSERT INTO auth_attempts (ip, attempted_at) VALUES (:ip, :at)"),\n'
     '                {"ip": ip, "at": at},\n'
     '            )'),
    ("R4  clear 变成清空全表（成功登录把别人也放了）",
     STORE,
     's.execute(text("DELETE FROM auth_attempts WHERE ip = :ip"), {"ip": ip})',
     's.execute(text("DELETE FROM auth_attempts"))'),
    ("R5  purge 边界从 < 改成 <=（与窗口判据不再互补）",
     STORE,
     '"DELETE FROM auth_attempts WHERE attempted_at < :before"',
     '"DELETE FROM auth_attempts WHERE attempted_at <= :before"'),
    ("R6  purge 删掉全部记录（含窗口内的）",
     STORE,
     '"DELETE FROM auth_attempts WHERE attempted_at < :before"',
     '"DELETE FROM auth_attempts"'),

    # ---------------- 客户端 IP 解析 ----------------
    ("I1  取 XFF 最左段（攻击者可伪造 -> 限流可绕过）",
     CLIENT_IP,
     "    candidate = parts[-count]",
     "    candidate = parts[0]"),
    ("I2  无代理部署时也信任 XFF（可被伪造）",
     CLIENT_IP,
     "    if count <= 0:\n        return direct or \"unknown\"\n",
     "    if count <= 0:\n        pass\n"),
    ("I3  去掉链长不足的回退（层数配错时拿错段）",
     CLIENT_IP,
     "    if len(parts) < count:\n"
     "        # 链长不够：要么代理没转发，要么配置的层数不对。\n"
     "        # 不猜 —— 用直连对端（fail-closed）。\n"
     "        return direct or \"unknown\"\n",
     ""),
    ("I4  去掉 IP 合法性校验（坏值被原样当成桶键）",
     CLIENT_IP,
     "    if not _is_valid_ip(candidate):",
     "    if False:"),
    ("I5  默认信任层数从 1 改成 0（线上退化成代理 IP 共桶）",
     CLIENT_IP,
     'os.getenv("TRUSTED_PROXY_COUNT", "1")',
     'os.getenv("TRUSTED_PROXY_COUNT", "0")'),

    # ---------------- 装配点 ----------------
    ("F1  从 _REGISTRY 去掉限流存储那一行",
     FACTORY,
     '    "rate_limit": (\n'
     '        "RATE_LIMIT_STORE_BACKEND", RateLimitStore, "限流存储",\n'
     '        SQLiteRateLimitStore,\n'
     '    ),\n',
     ""),
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
    targets = [STORE, CLIENT_IP, FACTORY]
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

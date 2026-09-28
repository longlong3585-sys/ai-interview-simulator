"""T-19 破坏性验证：向存储实现 / 协议 / 迁移 004 注入缺陷。

T-19 有三层产物，都要证明"测试真的有判别力"：
  * `services/stores/sqlite_store.py` —— 乐观锁 / seq 幂等 / 状态机 / 短事务
  * `services/stores/base.py`        —— 协议修正（ReplayLookup / report 字段）
  * `migrations/versions/004_*.py`   —— 纯加列

每个探针：注入 -> 跑测试 -> 记录退出码 -> `try/finally` 还原 -> sha256 校验。
另加通用校验：**目标字符串必须恰好命中一次**，否则视为无效探针
（T-18 的 M9 就是因为命中了两处而误报"漏网"）。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t19_store.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(ROOT, "services", "stores", "sqlite_store.py")
BASE = os.path.join(ROOT, "services", "stores", "base.py")
MIG4 = os.path.join(ROOT, "migrations", "versions", "004_session_report.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

# --- 未正常收尾时的自救设施 -------------------------------------------------
#
# 教训（T-19 真实踩到）：探针用 `try/finally` 保证还原，但**进程被强杀时
# finally 不会执行**。我第一次跑探针时因为整体太慢，用 job_kill 掐掉了它，
# 结果 S7 的缺陷（`get()` 少了 `try/finally: close()`）被永久留在工作区里，
# 之后 `test_sqlite_session_store` 从 3.4 秒变成永久挂死 —— 而且因为
# "基线"本身就是坏的，探针脚本的基线保护也报不出真正原因。
#
# 更糟的是：`sqlite_store.py` / `base.py` 之外，被探针改的文件里有些是
# **未跟踪的新文件**，`git checkout --` 根本救不回来（T-18 的说明在这一点上是错的）。
#
# 因此这里加两道保险：
#   1. 改之前把原始内容另存一份到 BACKUP_DIR（不依赖 git）；
#   2. 留一个 STATE 文件记录"改之前的 sha256"。下次启动时若 STATE 还在
#      且当前内容与记录不符，说明上次没正常收尾 —— **直接拒绝运行**并
#      打印恢复命令，而不是在已经损坏的工作区上继续做实验。
_STATE = os.path.join(tempfile.gettempdir(), "dsh-probe-t19-state.json")
_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "dsh-probe-t19-backup")


def _cleanup_state():
    """正常收尾后清掉状态文件；下次启动就不会误报"未正常收尾"。"""
    try:
        if os.path.exists(_STATE):
            os.remove(_STATE)
    except OSError:
        pass


def _check_previous_run(targets):
    """若上次探针未正常收尾，拒绝运行并给出恢复办法。"""
    if not os.path.exists(_STATE):
        return True
    try:
        with open(_STATE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (ValueError, OSError):
        saved = {}
    dirty = []
    for path, before in saved.items():
        if not os.path.exists(path):
            continue
        if sha(read_text(path)) != before:
            dirty.append(path)
    if not dirty:
        os.remove(_STATE)
        return True
    print("!! 检测到上一次探针**未正常收尾**，工作区可能仍带着注入的缺陷：")
    for path in dirty:
        print("     %s" % path)
    print("")
    print("   请先恢复（任一方式）：")
    print("     1) 从备份复制（探针运行前自动留的原始副本）：")
    for path in dirty:
        print('        Copy-Item "%s" "%s" -Force'
              % (os.path.join(_BACKUP_DIR, os.path.basename(path)), path))
    print("     2) 若该文件已被 git 跟踪：git checkout -- <路径>")
    print("")
    print("   恢复并确认无误后，删除状态文件再重跑：")
    print("        Remove-Item \"%s\"" % _STATE)
    return False


def _save_backups(targets, originals):
    shutil.rmtree(_BACKUP_DIR, ignore_errors=True)
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    for path, text in originals.items():
        with open(os.path.join(_BACKUP_DIR, os.path.basename(path)),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    with open(_STATE, "w", encoding="utf-8") as fh:
        json.dump({p: sha(t) for p, t in originals.items()}, fh, indent=2)

TEST_MODULES = [
    "tests.test_sqlite_session_store",
    "tests.test_stores_protocol",
    "tests.test_migration_004",
    "tests.test_migrations",
]

COMMIT_SET = (
    '"SET current_index = :ci, question_status = :qs, "\n'
    '                "    user_answers = :ua, last_seq = :ls, last_reply = :lr, "\n'
    '                "    version = version + 1, updated_at = :updated_at "\n'
    '                "WHERE session_id = :sid "\n'
    '                "  AND version = :expected "\n'
    '                "  AND status = :active "\n'
    '                "  AND expires_at > :now",'
)

PROBES = [
    # ---------------- 乐观锁 ----------------
    ("S1  去掉乐观锁的 version 条件",
     STORE, COMMIT_SET,
     COMMIT_SET.replace('"  AND version = :expected "\n', "")),
    ("S2  去掉 status='active' 守卫（已结束的会话还能写）",
     STORE, COMMIT_SET,
     COMMIT_SET.replace('"  AND status = :active "\n', "")),
    ("S3  去掉 expires_at 过期守卫",
     STORE, COMMIT_SET,
     COMMIT_SET.replace('"  AND expires_at > :now"', '"  AND 1 = 1"')),
    ("S4  不再递增 version（乐观锁形同虚设）",
     STORE,
     '"    version = version + 1, updated_at = :updated_at "\n'
     '                "WHERE session_id = :sid "',
     '"    updated_at = :updated_at "\n'
     '                "WHERE session_id = :sid "'),

    # ---------------- seq 幂等（协议修正是否真的承重） ----------------
    ("S5  find_replay 退回 T-16 的裸 str 语义（空回复会重复计费）",
     STORE,
     "            if seq is not None and last_seq is not None and seq <= last_seq:\n"
     "                return ReplayLookup(is_replay=True, reply=last_reply)\n"
     "            return ReplayLookup(is_replay=False)",
     "            if seq is not None and last_seq is not None and seq <= last_seq:\n"
     "                return last_reply\n"
     "            return None"),

    # ---------------- 短事务 ----------------
    ("S6  commit_turn 不提交（写入丢失）",
     STORE,
     '                    "active": SessionStatus.ACTIVE,\n'
     '                },\n'
     '            )\n'
     '            s.commit()\n'
     '            if changed:',
     '                    "active": SessionStatus.ACTIVE,\n'
     '                },\n'
     '            )\n'
     '            if changed:'),
    ("S7  get() 不关闭 session（事务泄漏 -> 阻塞所有请求）",
     STORE,
     "        s = self._session()\n"
     "        try:\n"
     "            return self._fetch(s, session_id)\n"
     "        finally:\n"
     "            s.close()",
     "        s = self._session()\n"
     "        return self._fetch(s, session_id)"),

    # ---------------- 状态机 ----------------
    ("S8  abandon 加上过期守卫（超时兜底会永远失败）",
     STORE,
     '                "WHERE session_id = :sid AND version = :expected AND status = :active",\n'
     '                {\n'
     '                    "abandoned": SessionStatus.ABANDONED,',
     '                "WHERE session_id = :sid AND version = :expected AND status = :active "\n'
     '                "  AND expires_at > :now",\n'
     '                {\n'
     '                    "now": now,\n'
     '                    "abandoned": SessionStatus.ABANDONED,'),
    ("S9  不再校验 ended_reason（非法取值会被写进库）",
     STORE,
     "    @staticmethod\n"
     "    def _validate_reason(ended_reason: str):\n"
     "        if ended_reason not in EndedReason.ALL:",
     "    @staticmethod\n"
     "    def _validate_reason(ended_reason: str):\n"
     "        if False:"),

    # ---------------- 冲突判别不能靠异常文本 ----------------
    ("S10 create 把任何 IntegrityError 都当成 active 冲突",
     STORE,
     "            if self._has_active(s, draft.user_id):\n"
     "                raise ActiveSessionExists(draft.user_id)\n"
     "            raise StoreError(",
     "            raise ActiveSessionExists(draft.user_id)\n"
     "            raise StoreError("),

    # ---------------- JSON 严格解析 ----------------
    ("S11 JSON 解析改为容错回退（坏数据被静默降级）",
     STORE,
     "    try:\n"
     "        return json.loads(raw)\n"
     "    except (ValueError, TypeError) as exc:\n"
     "        raise StoreError(",
     "    try:\n"
     "        return json.loads(raw)\n"
     "    except (ValueError, TypeError) as exc:\n"
     "        return [] if False else _raise_disabled("),

    # ---------------- 协议 / 迁移 ----------------
    ("P1  快照去掉 report 字段（跨层断言应报错）",
     BASE,
     "    report: Optional[str] = None\n",
     ""),
    ("M1  004 不加 report 列",
     MIG4,
     '    "ALTER TABLE interview_sessions ADD COLUMN report TEXT",',
     ""),
    ("M2  004 把 report 设成 NOT NULL（既有行会被塞默认值）",
     MIG4,
     '"ALTER TABLE interview_sessions ADD COLUMN report TEXT"',
     '"ALTER TABLE interview_sessions ADD COLUMN report TEXT NOT NULL DEFAULT \'{}\'"'),
]


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_tests(timeout=60):
    """跑测试。**单个探针必须有超时**。

    为什么：有的缺陷会让测试**挂住**而不是快速失败 —— 例如"事务泄漏"
    会持着写锁，在 T-15 的 `BEGIN IMMEDIATE` 下后续每个操作都要等
    `busy_timeout=15000`（15 秒），几十个用例累加就能把整个探针跑挂死
    （首次运行就真的挂到 600 秒超时）。挂住本身**就是一种失败**，
    所以超时计为 CAUGHT，但必须标注清楚以免与"断言失败"混淆。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([PY, "-m", "unittest"] + TEST_MODULES,
                              cwd=ROOT, capture_output=True, env=env,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT（>%ds，疑似事务泄漏导致后续操作全部阻塞）" % timeout, \
               ["<超时：测试进程未在 %ds 内结束>" % timeout]
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
    targets = [STORE, BASE, MIG4]
    if not _check_previous_run(targets):
        return 2

    originals = {p: read_text(p) for p in targets}
    _save_backups(targets, originals)
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
        print("     （注意：若基线是『挂住』而非『断言失败』，多半是工作区里")
        print("       仍留着上一次未还原的缺陷 —— 先检查 git status 与未跟踪文件）")
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
            for f in failing[:6]:
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

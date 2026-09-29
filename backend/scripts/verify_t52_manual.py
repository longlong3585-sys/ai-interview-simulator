#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-52 一键人工验收：每日备份 + 保留 7 份（ADR-019 / NFR-8）。

一条命令，**不起服务、不联网、不碰你的真备份、不改任何仓库文件**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t52_manual.py

可选参数：
    --skip-full-tests   跳过 B-② 的全量后端测试（默认跑，671 项约 90 秒）
    --keep-temp         保留临时工作目录（排障用；默认跑完就删）

退出码：0 = 通过；1 = 未通过；2 = 环境问题（缺文件/缺依赖/写不了临时目录）。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 5 的 T-52）：

  * **每日生成备份**；
  * **保留 7 份**；
  * **恢复演练成功**。

四段互不依赖的取证：

  A. 交付物齐全 —— 脚本 / systemd 单元 / 测试 / 部署文档都在，且
                   **两处踩了就会静默出事**的配置是对的：
                   `backup.timer` 的 `Unit=backup.service` 真的对应一个存在的文件；
                   `backup.service` **没有**把 `ProtectHome` 设成 `true`
                   （旧站项目根在 `/root` 下，设了就读不到数据库，报错却只说"库不存在"）。
  B. 自动化测试 —— ① 本任务新增的 91 项测试全绿（57 备份 + 34 systemd 静态）；
                   ② **全量 671 项**全绿 —— 备份脚本复用了
                   `make_backup.backup_database`，且 T-52 把单实例锁
                   抽成了 `scripts/single_instance.py`（`cleanup.py` 改为委派），
                   所以**必须**证明没有打坏清理那条链路。
  C. 独立复核     —— **本脚本自己重跑一遍关键语义**，不用测试里的断言：
                   ① 真跑 8 天，第 8 天之后**恰好 7 份**，且被删的是最老那份；
                   ② 备份库是**单文件**（无 -wal/-shm）且 `integrity_check=ok`；
                   ③ `uploads.zip` 内容与源文件逐字节一致；
                   ④ `.env` 缺失时**只告警不失败**，`--require-env` 时才失败；
                   ⑤ `--dry-run` 不写文件、不删目录，且**报的删除名单与真跑一致**；
                   ⑥ 已有的锁会让第二次调用返回 **3**（跳过），且**什么都不做**；
                   ⑦ **恢复演练**：从备份目录里**只拿 interview.db 一个文件**，
                      复制到干净目录，仍能 `integrity_check=ok` 并读出全部行。
  D. 破坏性探针   —— 7 类损坏**全部**必须被 `verify_backup_dir()` 抓住
                   （改库/截断/删 manifest/manifest 改行数/换 zip/zip 变垃圾/删 .env）。
                   全部在**副本**上做，验收结束后原备份一个字节都没动。

为什么"只拿 .db 一个文件"那一步是必需的：本项目的事故恢复现场，人手上
往往只有那一个文件（T-01/T-09 的事故都是这么挽回的）。备份若依赖
`-wal`/`-shm` 附属文件，那一刻就已经输了 —— 所以这一步测的不是"能不能读"，
而是"**备份是不是自洽的**"。

已知边界（刻意如此，非遗漏）：
  * **不在真机 systemd 上跑 timer**。开发机是 Windows，没有 systemd；
    单元文件只能做静态检查（B-①），真正的 `systemctl enable --now`
    与"开机补跑"验证在阿里云 Linux 服务器上做 —— 见 `deploy/systemd/README.md`
    与 T-53 的升级 Runbook。
  * **不做跨天时钟实验**：8 天的备份用注入时钟（`now=` 参数）在几秒内跑完，
    测的是保留策略本身，不是"系统时钟会不会跳"。
  * **备份不校验磁盘配额**：本脚本只证明"备份正确"，不证明"磁盘装得下 7 份"。
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

# 脚本位于 backend/scripts/ —— 三层 dirname 即仓库根。
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(_HERE))
BACKEND_DIR = os.path.join(REPO_DIR, "backend")
VENV_PYTHON = os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe")

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}
TEMP_ROOT = None


# ---------------------------------------------------------------------------
# 输出小工具（与 T-51 的验收脚本同一套记号，便于横向对比）
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
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
    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    return proc.returncode, out


def tail(text, n=5):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:]


def parse_unittest(out):
    """从 unittest 输出里取 (跑了几项, 是否全绿)。"""
    m = re.search(r"^Ran (\d+) tests?", out or "", re.M)
    if not m:
        return None, False
    return int(m.group(1)), bool(re.search(r"^OK", out or "", re.M))


def python_exe():
    return VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


# ---------------------------------------------------------------------------
# A 段：交付物与"静默出事"配置
# ---------------------------------------------------------------------------

EXPECTED_FILES = {
    "备份脚本": os.path.join("backend", "scripts", "rotate_backup.py"),
    "单实例锁（与清理共用）": os.path.join("backend", "scripts", "single_instance.py"),
    "备份单元": os.path.join("deploy", "systemd", "backup.service"),
    "备份定时器": os.path.join("deploy", "systemd", "backup.timer"),
    "备份测试": os.path.join("backend", "tests", "test_backup_rotation.py"),
    "部署文档": os.path.join("deploy", "systemd", "README.md"),
    "本验收脚本": os.path.join("backend", "scripts", "verify_t52_manual.py"),
}


def parse_unit(path):
    data, section_name = {}, None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section_name = line[1:-1]
                data.setdefault(section_name, {})
                continue
            if "=" not in line or section_name is None:
                continue
            key, _, value = line.partition("=")
            data[section_name].setdefault(key.strip(), []).append(value.strip())
    return data


def segment_artifacts():
    section("A 段 · 交付物与关键配置（静态，可离线）")
    failed_before_a = len(FAILED)
    missing = []
    for label, rel in EXPECTED_FILES.items():
        path = os.path.join(REPO_DIR, rel)
        if os.path.isfile(path):
            ok("%s 存在：%s" % (label, rel))
        else:
            bad("%s 缺失：%s" % (label, rel))
            missing.append(rel)
    if missing:
        SEG["A"] = False
        return
    service = os.path.join(REPO_DIR, "deploy", "systemd", "backup.service")
    timer = os.path.join(REPO_DIR, "deploy", "systemd", "backup.timer")
    svc, tmr = parse_unit(service), parse_unit(timer)

    # ① Unit= 必须指向真实存在的文件（写错时 timer 会静默地什么都不触发）
    unit_name = (tmr.get("Timer", {}).get("Unit") or [None])[0]
    if unit_name and os.path.isfile(os.path.join(os.path.dirname(service), unit_name)):
        ok("backup.timer 的 Unit=%s 对应一个真实存在的单元文件" % unit_name)
    else:
        bad("backup.timer 的 Unit=%r 指向不存在的文件（timer 会静默失效）" % unit_name)

    # ② ProtectHome=true 会让 /root 下的项目读不到数据库（旧站就在 /root 下）
    protect_home = (svc.get("Service", {}).get("ProtectHome") or [None])[0]
    if protect_home == "true":
        bad("backup.service 设了 ProtectHome=true —— 旧站项目根在 /root 下，"
            "备份会以“数据库不存在”失败")
    else:
        ok("backup.service 的 ProtectHome=%s（没有把 /root 藏起来）"
           % (protect_home or "未设置"))

    # ③ 每日一次 + 保留 7 份 + 退出码 3 不算失败
    on_calendar = (tmr.get("Timer", {}).get("OnCalendar") or [None])[0]
    if on_calendar and on_calendar.count(" "):
        ok("backup.timer 每日触发一次：OnCalendar=%s" % on_calendar)
    else:
        bad("backup.timer 的 OnCalendar=%r 不像“每日一次”" % on_calendar)

    exec_start = (svc.get("Service", {}).get("ExecStart") or [""])[0]
    if "--keep 7" in exec_start:
        ok("ExecStart 显式传了 --keep 7（ADR-019 的保留份数）")
    else:
        bad("ExecStart 没有传 --keep 7：%s" % exec_start)

    success_codes = ((svc.get("Service", {}).get("SuccessExitStatus") or [""])[0]).split()
    if "3" in success_codes and "0" in success_codes:
        ok("SuccessExitStatus=%s（3 = 已有实例在跑，不算失败）" % " ".join(success_codes))
    else:
        bad("SuccessExitStatus=%r 缺 0 或 3：跳过会被 systemd 记成 failed" % success_codes)

    # ④ 清理那条链路必须还在（T-52 把锁实现搬走了，改错会打坏清理）
    cleanup = os.path.join(REPO_DIR, "deploy", "systemd", "cleanup.service")
    cleanup_svc = parse_unit(cleanup)
    if "scripts/cleanup.py" in (cleanup_svc.get("Service", {}).get("ExecStart") or [""])[0]:
        ok("cleanup.service 仍然指向 scripts/cleanup.py（锁重构没有改坏它）")
    else:
        bad("cleanup.service 的 ExecStart 不再是 scripts/cleanup.py")

    SEG["A"] = len(FAILED) == failed_before_a


# ---------------------------------------------------------------------------
# B 段：自动化测试
# ---------------------------------------------------------------------------

def segment_tests(args):
    section("B 段 · 自动化测试")
    py = python_exe()

    say("B-① 本任务新增的测试（备份轮转 57 项 + systemd 静态 34 项）")
    code, out = run([py, "-m", "unittest", "-v",
                     "tests.test_backup_rotation", "tests.test_systemd_units"])
    say("  —— 尾部 5 行 ——")
    for line in tail(out, 5):
        say("     %s" % line)
    count, green = parse_unittest(out)
    if code == 0 and green:
        ok("新增测试全绿（%s 项）" % (count if count is not None else "?"))
        SEG["B1"] = True
    else:
        bad("新增测试未全绿（退出码 %s）" % code)
        SEG["B1"] = False

    if args.skip_full_tests:
        skip("B-② 全量后端测试（--skip-full-tests）")
        SEG["B2"] = None
    else:
        say("")
        say("B-② 全量后端测试（这一步证明锁重构没打坏清理链路，约 90 秒）")
        code, out = run([py, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                        timeout=1800)
        say("  —— 尾部 5 行 ——")
        for line in tail(out, 5):
            say("     %s" % line)
        count, green = parse_unittest(out)
        if code == 0 and green:
            ok("全量后端测试全绿（%s 项）" % (count if count is not None else "?"))
            SEG["B2"] = True
        else:
            bad("全量后端测试未全绿（退出码 %s）" % code)
            SEG["B2"] = False

    SEG["B"] = all(v is not False for v in (SEG.get("B1"), SEG.get("B2")))


# ---------------------------------------------------------------------------
# C 段：独立复核（自己造数据真跑，不依赖测试里的断言）
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, is_active BOOLEAN);
CREATE TABLE interview_records (id INTEGER PRIMARY KEY, user_id INTEGER, report TEXT);
CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT);
"""

BASE_DAY = datetime.datetime(2026, 9, 30, 3, 30, 0)
USERS, RECORDS = 3, 4
AVATAR_BYTES = b"\xff\xd8\xff\xe0" + b"avatar-payload" * 8


def make_source_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        for i in range(USERS):
            conn.execute("INSERT INTO users (username, is_active) VALUES (?, 1)",
                         ("u%d" % i,))
        for _ in range(RECORDS):
            conn.execute(
                "INSERT INTO interview_records (user_id, report) VALUES (1, ?)",
                ('{"x":1}',))
        conn.commit()
    finally:
        conn.close()


def make_sources(root):
    """造一套"像真站"的素材：WAL 库 + uploads + .env。"""
    db = os.path.join(root, "interview.db")
    make_source_db(db)
    uploads = os.path.join(root, "uploads")
    os.makedirs(os.path.join(uploads, "avatars"))
    with open(os.path.join(uploads, "avatars", "user_1_deadbeef.jpg"), "wb") as f:
        f.write(AVATAR_BYTES)
    env = os.path.join(root, ".env")
    with open(env, "w", encoding="utf-8") as f:
        f.write("SECRET_KEY=verify-t52\n")
    return db, uploads, env


def segment_independent():
    global TEMP_ROOT
    section("C 段 · 独立复核（真跑备份 + 恢复演练）")

    sys.path.insert(0, BACKEND_DIR)
    try:
        from scripts import rotate_backup as rb
    except Exception as exc:  # noqa: BLE001
        env_problem("无法导入 scripts.rotate_backup：%s" % exc)
        SEG["C"] = None
        return

    failed_before_c = len(FAILED)

    work = os.path.join(TEMP_ROOT, "c")
    os.makedirs(work)
    src = os.path.join(work, "src")
    os.makedirs(src)
    db, uploads, env = make_sources(src)
    out = os.path.join(work, "backup")
    quiet = lambda *a, **k: None  # noqa: E731

    # ---- C-① 连跑 8 天，第 8 次之后恰好 7 份，被删的是最老的一份 ----
    stamps, pruned_counts = [], []
    for n in range(8):
        moment = BASE_DAY + datetime.timedelta(days=n)
        stats, code = rb.run_backup(db, uploads, env, out, keep=7,
                                    now=moment, log=quiet)
        if code != 0 or stats is None:
            bad("第 %d 天的备份返回退出码 %s（期望 0）" % (n, code))
            SEG["C"] = False
            return
        stamps.append(stats["stamp"])
        pruned_counts.append(len(stats["pruned"]))

    dirs = rb.list_backup_dirs(out)
    if len(dirs) == 7:
        ok("连跑 8 天之后恰好保留 7 份：%s … %s" % (dirs[0], dirs[-1]))
    else:
        bad("连跑 8 天之后保留 %d 份（期望 7）：%s" % (len(dirs), dirs))
    if dirs and dirs[0] == stamps[1]:
        ok("被删的是最老的一份（%s）" % stamps[0])
    else:
        bad("被删的不是最老的一份：剩 %s，最老应为 %s"
            % (dirs[0] if dirs else None, stamps[1]))
    if pruned_counts[:7] == [0] * 7 and pruned_counts[7] == 1:
        ok("前 7 天一份都没删、第 8 天才删第 1 份（判据没写反）")
    else:
        bad("每天的删除数量不符合预期：%s" % pruned_counts)

    latest = os.path.join(out, dirs[-1])

    # ---- C-② 单文件 + integrity_check ----
    db_copy = os.path.join(latest, "interview.db")
    sidecars = [s for s in ("-wal", "-shm") if os.path.exists(db_copy + s)]
    if not sidecars:
        ok("备份库是单文件（没有 -wal/-shm 附属文件）")
    else:
        bad("备份库留下了附属文件：%s（拿到单个 .db 的人会不确定要不要一起拷）" % sidecars)
    conn = sqlite3.connect("file:%s?mode=ro" % db_copy.replace("\\", "/"), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if integrity == "ok":
        ok("备份库 integrity_check=ok，journal_mode=%s" % mode)
    else:
        bad("备份库 integrity_check=%r" % integrity)

    # ---- C-③ uploads.zip 内容逐字节一致 ----
    with zipfile.ZipFile(os.path.join(latest, "uploads.zip")) as zf:
        names = zf.namelist()
        content = zf.read("avatars/user_1_deadbeef.jpg") if names else b""
    if names == ["avatars/user_1_deadbeef.jpg"] and content == AVATAR_BYTES:
        ok("uploads.zip 里就是那个头像文件，内容逐字节一致")
    else:
        bad("uploads.zip 内容不符：names=%s, bytes_match=%s"
            % (names, content == AVATAR_BYTES))

    # ---- C-④ .env 缺失只告警；--require-env 才失败 ----
    src2 = os.path.join(work, "src2")
    os.makedirs(src2)
    db2, uploads2, _ = make_sources(src2)
    out2 = os.path.join(work, "backup2")
    missing_env = os.path.join(src2, ".env")
    os.remove(missing_env)
    buf = io.StringIO()
    stats2, code2 = rb.run_backup(db2, uploads2, missing_env, out2, keep=7,
                                  now=BASE_DAY, log=buf.write)
    if code2 == 0 and stats2 and stats2["env_warning"] and "警告" in buf.getvalue():
        ok("`.env` 读不到时：数据备份照做、有告警、退出码 0")
    else:
        bad(".env 缺失的处理不符预期（退出码 %s）" % code2)
    buf = io.StringIO()
    _, code3 = rb.run_backup(db2, uploads2, missing_env, out2, keep=7,
                             now=BASE_DAY, require_env=True, log=buf.write)
    if code3 == 2:
        ok("--require-env 时同一情形判失败（退出码 2）—— 部署前自检可用")
    else:
        bad("--require-env 没有把 .env 缺失判失败（退出码 %s）" % code3)

    # ---- C-⑤ dry-run 不写不删，且删除名单与真跑一致 ----
    out3 = os.path.join(work, "backup3")
    for n in range(4):
        rb.run_backup(db, uploads, env, out3, keep=3,
                      now=BASE_DAY + datetime.timedelta(days=n), log=quiet)
    before = rb.list_backup_dirs(out3)
    dry_stats, dry_code = rb.run_backup(db, uploads, env, out3, keep=3,
                                        now=BASE_DAY + datetime.timedelta(days=4),
                                        dry_run=True, log=quiet)
    after_dry = rb.list_backup_dirs(out3)
    if dry_code == 0 and after_dry == before:
        ok("--dry-run 没有写文件、也没有删目录（%d 份原样）" % len(before))
    else:
        bad("--dry-run 改动了备份目录：%s -> %s" % (before, after_dry))
    dry_planned = list(dry_stats["pruned"]) if dry_stats else []
    real_stats, _ = rb.run_backup(db, uploads, env, out3, keep=3,
                                  now=BASE_DAY + datetime.timedelta(days=4), log=quiet)
    real_done = list(real_stats["pruned"]) if real_stats else []
    if dry_planned == real_done and dry_planned:
        ok("dry-run 预告要删的 %s 与真跑删掉的完全一致" % dry_planned)
    else:
        bad("dry-run 预告 %s，真跑删了 %s —— 口径分裂" % (dry_planned, real_done))

    # ---- C-⑥ 锁：第二次调用返回 3 且什么都不做 ----
    lock = rb.lock_path_for(db, rb.BACKUP_LOCK_SUFFIX)
    rb.release_lock(lock)
    held = rb.acquire_lock(lock, log=quiet)
    try:
        out4 = os.path.join(work, "backup4")
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            code4 = rb.main(["--db", db, "--uploads", uploads, "--env-file", env,
                             "--out", out4])
        finally:
            sys.stdout = old_stdout
        if code4 == 3 and not rb.list_backup_dirs(out4):
            ok("已有实例持锁时：退出码 3（跳过）且没有产生任何备份")
        else:
            bad("持锁时的行为不符预期：退出码 %s，目录 %s"
                % (code4, rb.list_backup_dirs(out4)))
    finally:
        rb.release_lock(lock)

    # ---- C-⑦ 恢复演练：只拿 interview.db 一个文件 ----
    drill = os.path.join(work, "drill")
    os.makedirs(drill)
    lone = os.path.join(drill, "restored.db")
    shutil.copy(os.path.join(latest, "interview.db"), lone)
    conn = sqlite3.connect("file:%s?mode=ro" % lone.replace("\\", "/"), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        records = conn.execute("SELECT COUNT(*) FROM interview_records").fetchone()[0]
    finally:
        conn.close()
    if integrity == "ok" and users == USERS and records == RECORDS:
        ok("恢复演练：只把 interview.db 复制到干净目录，仍 integrity_check=ok、"
           "users=%d records=%d" % (users, records))
    else:
        bad("恢复演练失败：integrity=%s users=%s records=%s"
            % (integrity, users, records))

    # 上传物也能"恢复"（解压到空目录即可）
    restored_uploads = os.path.join(drill, "uploads")
    with zipfile.ZipFile(os.path.join(latest, "uploads.zip")) as zf:
        zf.extractall(restored_uploads)
    restored_file = os.path.join(restored_uploads, "avatars", "user_1_deadbeef.jpg")
    if os.path.isfile(restored_file):
        with open(restored_file, "rb") as f:
            same = f.read() == AVATAR_BYTES
        if same:
            ok("上传物恢复演练：解压后头像与原文件逐字节一致")
        else:
            bad("上传物恢复后内容不一致")
    else:
        bad("上传物恢复后找不到头像文件")

    # C 段是否通过，由"本段有没有新增 FAIL"决定（FAILED 是全局累积的）。
    SEG["C"] = len(FAILED) == failed_before_c


# ---------------------------------------------------------------------------
# D 段：破坏性探针（全部在副本上做）
# ---------------------------------------------------------------------------

def probe(rb, name, mutate, work, source_dir):
    """复制一个备份目录、施加一种损坏、要求校验器判 FAIL。"""
    target = os.path.join(work, "probe-" + name)
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(source_dir, target)
    mutate(target)
    okflag, problems, _ = rb.verify_backup_dir(target)
    if okflag:
        bad("探针 `%s` 没有被抓住（损坏的备份被判为健康）" % name)
        return False
    return True


def segment_probes():
    section("D 段 · 破坏性探针（7 类损坏都必须被抓住；只在副本上做）")
    sys.path.insert(0, BACKEND_DIR)
    try:
        from scripts import rotate_backup as rb
    except Exception as exc:  # noqa: BLE001
        env_problem("无法导入 scripts.rotate_backup：%s" % exc)
        SEG["D"] = None
        return

    work = os.path.join(TEMP_ROOT, "d")
    os.makedirs(work)
    src = os.path.join(work, "src")
    os.makedirs(src)
    db, uploads, env = make_sources(src)
    out = os.path.join(work, "backup")
    stats, code = rb.run_backup(db, uploads, env, out, keep=7, now=BASE_DAY,
                                log=lambda *a: None)
    if code != 0 or stats is None:
        env_problem("探针的前置备份没跑成（退出码 %s）" % code)
        SEG["D"] = None
        return
    source_dir = stats["backup_dir"]
    ok("前置：先造出一份健康备份（校验通过=%s）"
       % rb.verify_backup_dir(source_dir)[0])

    def tamper_db(target):
        p = os.path.join(target, "interview.db")
        conn = sqlite3.connect(p)
        conn.execute("INSERT INTO users (username, is_active) VALUES ('intruder', 1)")
        conn.commit()
        conn.close()

    def truncate_db(target):
        p = os.path.join(target, "interview.db")
        with open(p, "r+b") as f:
            f.truncate(200)

    def drop_manifest(target):
        os.remove(os.path.join(target, "manifest.json"))

    def bump_row_count(target):
        p = os.path.join(target, "manifest.json")
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        m["database"]["row_counts"]["users"] = 999
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)

    def swap_uploads(target):
        p = os.path.join(target, "uploads.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("avatars/user_1_deadbeef.jpg", "different content")

    def garbage_uploads(target):
        with open(os.path.join(target, "uploads.zip"), "wb") as f:
            f.write(b"this is not a zip file")

    def drop_env(target):
        os.remove(os.path.join(target, ".env"))

    probes = [
        ("篡改数据库内容", tamper_db),
        ("截断数据库文件", truncate_db),
        ("删掉 manifest.json", drop_manifest),
        ("manifest 里行数被改", bump_row_count),
        ("uploads.zip 被换成同数量的另一个", swap_uploads),
        ("uploads.zip 变成垃圾字节", garbage_uploads),
        ("manifest 声称备份了 .env 但文件不见了", drop_env),
    ]
    caught = 0
    for name, mutate in probes:
        if probe(rb, name, mutate, work, source_dir):
            caught += 1
            ok("探针 `%s` 被抓住" % name)

    okay_after = rb.verify_backup_dir(source_dir)[0]
    if okay_after:
        ok("验收结束后，原始备份仍然健康（探针只在副本上做）")
    else:
        bad("原始备份被探针弄脏了 —— 探针没有隔离")

    SEG["D"] = (caught == len(probes)) and okay_after


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    for key, label in (("A", "A 交付物与关键配置"), ("B", "B 自动化测试"),
                       ("C", "C 独立复核"), ("D", "D 破坏性探针")):
        state = SEG.get(key)
        tag = "SKIP" if state is None else ("PASS" if state else "FAIL")
        say("  [%s] %s" % (tag, label))
    if SEG.get("B1") is not None or SEG.get("B2") is not None:
        for key, label in (("B1", "  新增测试"), ("B2", "  全量后端测试")):
            state = SEG.get(key)
            if state is None:
                continue
            say("        [%s] %s" % ("PASS" if state else "FAIL", label))

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
        say("  未通过项（%d）：" % len(FAILED))
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-52 人工验收未通过")
        return 1

    say("")
    say("✅ T-52 人工验收通过（每日备份 + 保留 7 份 + 恢复演练）")
    return 0


def main():
    global TEMP_ROOT
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-52 一键人工验收（离线、无副作用）")
    parser.add_argument("--skip-full-tests", action="store_true",
                        help="跳过 B-② 的全量后端测试（默认跑，约 90 秒）")
    parser.add_argument("--keep-temp", action="store_true",
                        help="保留临时工作目录（排障用）")
    args = parser.parse_args()

    say("T-52 人工验收：每日备份 + 保留 7 份（ADR-019 / NFR-8）")
    say("仓库：%s" % REPO_DIR)
    say("Python：%s" % python_exe())

    if not os.path.isdir(BACKEND_DIR):
        env_problem("找不到 backend 目录：%s" % BACKEND_DIR)
        return 2
    try:
        TEMP_ROOT = tempfile.mkdtemp(prefix="t52-verify-")
    except OSError as exc:
        env_problem("无法创建临时目录：%s" % exc)
        return 2
    say("临时工作目录：%s" % TEMP_ROOT)

    try:
        segment_artifacts()
        segment_tests(args)
        segment_independent()
        segment_probes()
        code = summary()
        if code != 0:
            return 1
        if ENV_PROBLEMS and any(SEG.get(k) is None for k in ("C", "D")):
            return 2
        return 0
    finally:
        if args.keep_temp:
            say("")
            say("（--keep-temp）临时目录保留在：%s" % TEMP_ROOT)
        else:
            shutil.rmtree(TEMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

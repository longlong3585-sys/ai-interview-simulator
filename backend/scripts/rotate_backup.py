"""T-52：每日备份 + 保留 7 份（ADR-019 / NFR-8）。

## 它和 `make_backup.py` 的分工（**别混用**）

| | `scripts/make_backup.py`（T-01） | `scripts/rotate_backup.py`（T-52，本文件） |
|---|---|---|
| 目的 | **手动**打一个"重构前的回滚点" | **每日自动**的持续性保护 |
| 产物 | `backup/` 根下散着 db + 源码 zip + manifest | 一个**自包含目录** `backup/YYYYMMDD_HHMMSS/` |
| 源码 zip | 有（回滚点要能回到那一刻的代码） | **无** —— 代码在 GitHub，本备份只管**数据** |
| 保留策略 | 永不自动删（回滚点可能要用很久） | **保留 7 份**，第 8 份落盘时删最老的 |
| 触发 | 人 | systemd `backup.timer`（每日 03:30） |

ADR-019 的原文只写了"每日 `sqlite3 .backup` + 保留 7 份 + 含 `uploads/`
与加密 `.env`"。本文件是它的落地，并**刻意沿用 T-17 的两个结论**：
备份产物必须是**单文件**（见 `make_backup.py::backup_database` 的长注释）、
备份必须**当场可校验**（不是"文件存在即成功"）。

## 一个备份目录里有什么

```
backup/20260930_033000/
├── interview.db          # sqlite3 官方 backup API 快照（WAL 安全、单文件、integrity_check=ok）
├── uploads.zip           # backend/uploads/ 全量（头像等用户上传物）
├── .env                  # 密钥与配置（**缺失只告警不失败**，见下）
└── manifest.json         # 每个文件的 sha256 + 行数 + 自校验结果 + 保留策略
```

## 为什么"恢复命令"要写进 manifest

事故现场没有人会去翻文档。manifest 里固化
`restore_hint`（原样可粘贴的命令），就是让**最后一个拿到这个目录的人**
不必猜"这四个文件哪个覆盖到哪"。同一条理由也是 T-01 把它写进
manifest 的原因。

## `.env` 的处理（**刻意不参与成败判定**）

`.env` 含 API Key 与 `SECRET_KEY`，必须备份；但它也**可能读不到**
（部署时属主/权限没给到位）。此时：

* 备份**继续**，`manifest.json` 记 `"env_file": null` 并给出原因；
* 控制台打 `WARNING`，退出码仍是 0；
* 想让它变成硬失败，就加 `--require-env`（部署前自检用）。

理由：数据库与上传物才是**不可再生**的；因为一个配置文件把整次备份
判失败，会让"备份有没有跑成"这个信号被污染 —— 而 ADR-019 保护的对象
是数据。反过来，若真的把 `.env` 当硬前置，第 3 天的权限漂移会静默
停掉全部备份，这才是真正的风险。

## 退出码（与 `cleanup.py` 保持同一套，便于统一监控）

| 码 | 含义 |
|---|---|
| 0 | 正常完成（含 `--dry-run`、含"`.env` 读不到但数据备份成功"） |
| 1 | `--verify` 发现**已有备份不健康**（校验失败） |
| 2 | 参数/环境错误（库不存在、写不进去、备份后 integrity_check 不是 ok） |
| 3 | 已有另一个实例在跑，本次跳过 —— **不算失败**（systemd `SuccessExitStatus=0 3`） |

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\rotate_backup.py --dry-run   # 看会做什么
    .\\venv\\Scripts\\python.exe scripts\\rotate_backup.py             # 真跑一次
    .\\venv\\Scripts\\python.exe scripts\\rotate_backup.py --verify    # 只校验已有备份
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.single_instance import (  # noqa: E402
    BACKUP_LOCK_SUFFIX,
    DEFAULT_STALE_AFTER_SECONDS,
    acquire_lock,
    lock_path_for,
    release_lock,
)
from scripts.make_backup import backup_database, inspect_db  # noqa: E402

#: ADR-019：保留 7 份。8 份意味着"连续 7 天没发现的数据损坏"仍有一份好快照，
#: 同时把磁盘占用钉死在一个可预测的上限（每份 ≈ 库大小 + uploads 大小）。
DEFAULT_KEEP = 7

#: 备份目录名的格式。**这个名字就是排序键**（`%Y%m%d` 字典序 = 时间序），
#: 因此轮转不需要读 manifest 就能判新旧，也就不会因为某个 manifest 损坏
#: 而算错该删谁。
STAMP_FORMAT = "%Y%m%d_%H%M%S"

#: 未完成目录的前缀。正常目录绝不以它开头，所以清理"上次崩溃留下的半成品"
#: 不可能误伤真备份。
TMP_PREFIX = ".incomplete-"

DB_NAME = "interview.db"
UPLOADS_ARCHIVE = "uploads.zip"
ENV_NAME = ".env"
MANIFEST_NAME = "manifest.json"

#: 备份库必须含有的业务表（与 `make_backup.py` / `verify_backup.py` 同口径）。
EXPECTED_TABLES = {"users", "interview_records", "notifications"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stamp_of(moment):
    return moment.strftime(STAMP_FORMAT)


def is_backup_dir_name(name):
    """判断目录名是否是一个备份目录（而不是半成品或别人放的东西）。"""
    try:
        datetime.datetime.strptime(name, STAMP_FORMAT)
    except ValueError:
        return False
    return True


def list_backup_dirs(out_dir):
    """列出已有的备份目录，**按名字排序（= 时间序）**。"""
    if not os.path.isdir(out_dir):
        return []
    names = [n for n in os.listdir(out_dir)
             if os.path.isdir(os.path.join(out_dir, n)) and is_backup_dir_name(n)]
    return sorted(names)


def list_incomplete_dirs(out_dir):
    if not os.path.isdir(out_dir):
        return []
    return sorted(n for n in os.listdir(out_dir)
                  if os.path.isdir(os.path.join(out_dir, n))
                  and n.startswith(TMP_PREFIX))


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 打包 uploads
# ---------------------------------------------------------------------------

def archive_uploads(uploads_dir, zip_path):
    """把整个 uploads 目录打进一个 zip。

    用 `zipfile` 而不是 `tarfile`/外部 `zip`：T-22 起本项目的规矩是
    "**开发机（Windows）也要能真跑一次**"，zip 是唯一天然跨平台、
    不依赖外部命令的容器。zip 的另一个好处是**能列出内容再校验**，
    恢复前可以先 `unzip -l` 看一眼。

    返回 (文件数, 未压缩字节数)。目录不存在时返回 (0, 0) —— 见
    `run_backup()` 里的说明：**空 uploads 不是错误**。
    """
    count = 0
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if not os.path.isdir(uploads_dir):
            return 0, 0
        for root, dirs, files in os.walk(uploads_dir):
            dirs[:] = sorted(dirs)
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, uploads_dir).replace("\\", "/")
                try:
                    zf.write(full, rel)
                except OSError:
                    # 单个文件读不到（权限/被占用）不该让整次备份失败：
                    # 记在返回里，由 manifest 的 warnings 显式暴露。
                    continue
                count += 1
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
    return count, total


def zip_manifest_of(zip_path):
    """读出 zip 的**内容凭证**：条目名列表 + 每条的 sha256。

    为什么不能只比"条目数"：把 `uploads.zip` 换成另一个**同数量**文件的
    zip（一次错误的覆盖、一次 rsync 半途中断都可能造成），条目数完全一样，
    校验器会报 OK —— 那是"看起来正常实则错误"的典型，正是本项目
    ADR-007R 要消除的东西。逐条 sha256 才是真正的判别器。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            entries = {}
            for name in sorted(zf.namelist()):
                if name.endswith("/"):
                    continue
                digest = hashlib.sha256()
                with zf.open(name) as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                entries[name] = digest.hexdigest()
            return entries
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def zip_entries(zip_path):
    """读回 zip 里的条目名（不含目录项）。读不开则返回 None。"""
    entries = zip_manifest_of(zip_path)
    if entries is None:
        return None
    return sorted(entries)


# ---------------------------------------------------------------------------
# 校验一个备份目录
# ---------------------------------------------------------------------------

def verify_backup_dir(backup_dir):
    """校验一个备份目录，返回 (ok, problems, info)。

    **校验的是"能不能拿来恢复"，不是"文件在不在"**：
      1. `interview.db` 能打开、`integrity_check == ok`、业务表齐全；
      2. 行数与 manifest 记的一致（证明 digest 之后没被人动过内容）；
      3. `interview.db` 的 sha256 与 manifest 一致；
      4. `uploads.zip` 能打开，且条目数与 manifest 一致；
      5. `.env` 若记了 sha256 也要一致。

    第 2/5 条是"manifest 被复制错"与"文件被截断"两类事故的判别器 ——
    T-02 就定过规矩：**只测正向不足以证明校验器有效**，
    因此本函数同样被 `tests/test_backup_rotation.py` 的负向用例钉住。
    """
    problems = []
    info = {}

    db_path = os.path.join(backup_dir, DB_NAME)
    manifest_path = os.path.join(backup_dir, MANIFEST_NAME)

    manifest = None
    if not os.path.isfile(manifest_path):
        problems.append("manifest.json 缺失")
    else:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as exc:
            problems.append("manifest.json 读不出/不是合法 JSON: %s" % exc)

    # --- 数据库 ---
    if not os.path.isfile(db_path):
        problems.append("%s 缺失" % DB_NAME)
    elif os.path.getsize(db_path) == 0:
        problems.append("%s 是空文件" % DB_NAME)
    else:
        try:
            db_info = inspect_db(db_path)
        except sqlite3.Error as exc:
            problems.append("%s 打不开: %s" % (DB_NAME, exc))
            db_info = None
        if db_info is not None:
            info["row_counts"] = db_info["row_counts"]
            info["journal_mode"] = db_info["journal_mode"]
            if db_info["integrity_check"] != "ok":
                problems.append("integrity_check = %r（应为 'ok'）"
                                % db_info["integrity_check"])
            missing = EXPECTED_TABLES - set(db_info["tables"])
            if missing:
                problems.append("缺少业务表: %s" % sorted(missing))
            if manifest:
                meta = (manifest.get("database") or {})
                if meta.get("sha256"):
                    actual = sha256_of(db_path)
                    if actual != meta["sha256"]:
                        problems.append(
                            "interview.db sha256 与 manifest 不符（被改动或截断）")
                for table, expected in (meta.get("row_counts") or {}).items():
                    actual = db_info["row_counts"].get(table)
                    if actual != expected:
                        problems.append(
                            "行数不符 %s: manifest=%s 实际=%s"
                            % (table, expected, actual))

    # --- uploads ---
    zip_path = os.path.join(backup_dir, UPLOADS_ARCHIVE)
    if not os.path.isfile(zip_path):
        problems.append("%s 缺失" % UPLOADS_ARCHIVE)
    else:
        entries = zip_manifest_of(zip_path)
        if entries is None:
            problems.append("%s 不是可读的 zip" % UPLOADS_ARCHIVE)
        else:
            info["uploads_files"] = len(entries)
            if manifest and manifest.get("uploads"):
                meta = manifest["uploads"]
                expected = meta.get("file_count")
                if expected is not None and expected != len(entries):
                    problems.append(
                        "uploads.zip 条目数不符: manifest=%s 实际=%s"
                        % (expected, len(entries)))
                recorded = meta.get("entries")
                if recorded is not None and recorded != entries:
                    changed = sorted(set(recorded) ^ set(entries))
                    problems.append(
                        "uploads.zip 内容与 manifest 不符（条目=%d，差异样本=%s）"
                        % (len(entries), changed[:5] or "(同名但内容被改)"))

    # --- .env（允许缺失，但"记了却对不上"必须报） ---
    if manifest is not None:
        env_meta = manifest.get("env_file")
        if env_meta:
            env_path = os.path.join(backup_dir, ENV_NAME)
            if not os.path.isfile(env_path):
                problems.append(".env 缺失（但 manifest 声称备份了它）")
            elif env_meta.get("sha256") and sha256_of(env_path) != env_meta["sha256"]:
                problems.append(".env sha256 与 manifest 不符")

    return (not problems), problems, info


# ---------------------------------------------------------------------------
# 轮转（保留策略）
# ---------------------------------------------------------------------------

def prune(out_dir, keep, protect=(), dry_run=False, log=print):
    """在**另有一份即将落盘**的前提下轮转：删除最老的若干份，使总数不超过 `keep`。

    ## "保留 7 份"到底指几份

    `keep` 指**这次运行结束之后磁盘上的目录总数**，于是 `keep=7` 时：
    每次新备份落盘后最老的那份被删掉，稳态正好 7 个目录（连续 7 天的快照）。
    这与 ADR-019 的"保留 7 份"字面一致，也让"本次要生成的那个目录"
    天然占一个名额 —— 判据因此**同时适用于真跑与 `--dry-run`**
    （一个已存在、一个还不存在），不会出现"预览说删 2 份、真跑删 3 份"
    的口径分裂。

    ## `protect` 是刚生成的那个目录名

    它必须免于删除：万一机器时钟被往前调过（或有人手工放了个未来时间戳
    的目录），"名义上最新的"未必是刚生成的这个 —— 那就是"备份完立刻把
    自己删了"，比不备份更危险：目录看起来还在轮转，实际一份都不剩。

    返回被删除的目录名列表（`dry_run=True` 时是"将要删除"的）。
    """
    existing = list_backup_dirs(out_dir)
    protected = set(protect)
    candidates = [n for n in existing if n not in protected]
    # 运行结束后的总数：
    #   真跑 —— `prune()` 在 rename **之后**调用，"本次这份"已在 existing 里；
    #   dry-run —— 它还不存在，但确实会占一个名额。
    # 于是：  删除数 = max(len(existing) - keep + (本次这份还不存在 ? 1 : 0), 0)
    # ⚠️ 首版写成 `keep - 受保护数`，keep=1 时第一轮就把唯一的历史备份删了
    #    （"留 1 份"被实现成"旧的一律不要"）—— 那是真实发生过的事故，
    #    因此把推导留在注释里，便于下一个人复核而不是凭感觉改。
    pending = 0 if all(p in existing for p in protect) else 1
    over = max(len(existing) + pending - keep, 0)
    victims = candidates[:min(over, len(candidates))]

    removed = []
    for name in victims:
        target = os.path.join(out_dir, name)
        if dry_run:
            log("  [dry-run] 将删除: %s" % name)
        else:
            shutil.rmtree(target, ignore_errors=True)
            log("  已删除最老的一份: %s" % name)
        removed.append(name)
    return removed


def cleanup_incomplete(out_dir, dry_run=False, log=print):
    """清掉上次崩溃留下的半成品目录。

    半成品目录**不参与保留计数**（名字不是时间戳，`list_backup_dirs`
    根本不列它），因此不会挤掉真备份。
    """
    removed = []
    for name in list_incomplete_dirs(out_dir):
        target = os.path.join(out_dir, name)
        if dry_run:
            log("  [dry-run] 将清理半成品: %s" % name)
        else:
            shutil.rmtree(target, ignore_errors=True)
            log("  已清理半成品目录: %s" % name)
        removed.append(name)
    return removed


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_backup(db_path, uploads_dir, env_path, out_dir, keep=DEFAULT_KEEP,
               dry_run=False, require_env=False, now=None, log=print):
    """做一次备份 + 轮转，返回 `(stats_dict, exit_code)`。

    `dry_run=True` 时**不写任何文件**，只报告"会备份到哪、会删谁"。
    计数与真实执行走的是同一段代码路径（`prune()` 的 `dry_run` 分支
    只换了动作、没换判据），避免"预览说删 1 份、真跑删 3 份"的口径偏差。

    `now` 是**测试用的时钟注入点**（不传则取本机当前时间）。为什么要开这个口子：
    备份目录名是排序键，要测"保留 7 份"就必须造出 8 个不同名字的备份，
    用真实时间连续跑 8 次只会在同一秒撞名 —— 那样测的是撞名，不是保留策略。
    与 `acquire_lock(now=...)` 是同一个做法，不必给 `datetime` 打补丁。
    """
    if not os.path.isfile(db_path):
        log("ERROR: 数据库不存在: %s" % db_path)
        return None, 2
    if keep < 1:
        log("ERROR: --keep 必须 >= 1（等于 1 表示只留最新一份历史备份）")
        return None, 2

    moment = now or datetime.datetime.now()
    stamp = stamp_of(moment)
    final_dir = os.path.join(out_dir, stamp)
    tmp_dir = os.path.join(out_dir, TMP_PREFIX + stamp)

    stats = {
        "stamp": stamp,
        "backup_dir": final_dir,
        "db_bytes": 0,
        "db_sha256": None,
        "uploads_files": 0,
        "uploads_bytes": 0,
        "env_backed_up": False,
        "env_warning": None,
        "keep": keep,
        "pruned": [],
        "incomplete_removed": [],
        "tmp_dir": tmp_dir,
        "dry_run": dry_run,
    }

    if dry_run:
        log("=== DRY-RUN（不写任何文件）===")
        log("备份目标: %s" % final_dir)
        log("  数据来源: %s" % db_path)
        log("  上传来源: %s" % uploads_dir)
        log("  配置来源: %s" % env_path)
        log("保留策略: 本次之后磁盘上最多 %d 个备份目录（ADR-019 要求保留 7 份）"
            % keep)
        if not os.path.isdir(uploads_dir):
            log("  注意: uploads 目录不存在（%s）—— 将备份成空归档，不算失败"
                % uploads_dir)
        if not os.path.isfile(env_path):
            log("  注意: .env 不存在（%s）—— 将记入 manifest 并告警"
                % env_path)
        db_bytes = os.path.getsize(db_path)
        log("  数据库大小: %.1f KB" % (db_bytes / 1024.0))
        stats["db_bytes"] = db_bytes
        # 预览三件事：要做几个目录、会删谁、半成品有几个
        existing = list_backup_dirs(out_dir)
        stats["incomplete_removed"] = cleanup_incomplete(out_dir, dry_run=True, log=log)
        stats["pruned"] = prune(out_dir, keep, protect=[stamp], dry_run=True, log=log)
        log("  已有备份 %d 份，本次新增 1 份、删除 %d 份 -> 之后共 %d 份"
            % (len(existing), len(stats["pruned"]),
               len(existing) + 1 - len(stats["pruned"])))
        log("结论: DRY-RUN 完成（未写任何文件、未删任何目录）")
        return stats, 0

    os.makedirs(out_dir, exist_ok=True)
    stats["incomplete_removed"] = cleanup_incomplete(out_dir, dry_run=False, log=log)

    # 半成品目录用独立名字 + 最后一步 rename，保证"出现在 out_dir 里的
    # 时间戳目录"永远是完整的。否则一次断电会留下一个**看起来像备份、
    # 实际缺文件**的目录 —— 那比没有备份更危险（恢复时才发现）。
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)

    try:
        log("[1/5] 备份数据库 ...")
        db_dst = os.path.join(tmp_dir, DB_NAME)
        backup_database(db_path, db_dst)          # 复用 T-01/T-17 的实现（WAL 安全 + 单文件）
        db_info = inspect_db(db_dst)
        if db_info["integrity_check"] != "ok":
            log("ERROR: 备份库 integrity_check = %r，放弃本次备份"
                % db_info["integrity_check"])
            return None, 2
        missing = EXPECTED_TABLES - set(db_info["tables"])
        if missing:
            log("ERROR: 备份库缺少业务表 %s，放弃本次备份" % sorted(missing))
            return None, 2
        stats["db_bytes"] = os.path.getsize(db_dst)
        stats["db_sha256"] = sha256_of(db_dst)
        log("      -> %s (%.1f KB, integrity_check=ok)"
            % (DB_NAME, stats["db_bytes"] / 1024.0))

        log("[2/5] 打包 uploads ...")
        if not os.path.isdir(uploads_dir):
            log("      警告: uploads 目录不存在（%s）—— 归档为空" % uploads_dir)
        zip_path = os.path.join(tmp_dir, UPLOADS_ARCHIVE)
        stats["uploads_files"], stats["uploads_bytes"] = archive_uploads(
            uploads_dir, zip_path)
        log("      -> %s (%d 个文件, %.1f KB 原始大小)"
            % (UPLOADS_ARCHIVE, stats["uploads_files"],
               stats["uploads_bytes"] / 1024.0))

        log("[3/5] 复制 .env ...")
        env_meta = None
        if not os.path.isfile(env_path):
            stats["env_warning"] = "文件不存在: %s" % env_path
            log("      警告: .env 不存在 —— 数据已备份，但配置未备份")
        else:
            try:
                env_dst = os.path.join(tmp_dir, ENV_NAME)
                shutil.copyfile(env_path, env_dst)
                env_meta = {
                    "sha256": sha256_of(env_dst),
                    "bytes": os.path.getsize(env_dst),
                }
                stats["env_backed_up"] = True
                log("      -> %s (%.1f KB)   ⚠️ 内含密钥，注意目录权限"
                    % (ENV_NAME, env_meta["bytes"] / 1024.0))
            except OSError as exc:
                stats["env_warning"] = "读取失败: %s" % exc
                log("      警告: .env 读不到（%s）—— 数据备份继续" % exc)
        if stats["env_warning"] and require_env:
            log("ERROR: --require-env 已指定，但 .env 未备份成功：%s"
                % stats["env_warning"])
            return None, 2

        log("[4/5] 写 manifest ...")
        manifest = {
            "created_at": moment.isoformat(timespec="seconds"),
            "stamp": stamp,
            "tool": "scripts/rotate_backup.py (T-52)",
            "policy": {
                "keep": keep,
                "reason": "ADR-019：每日备份、保留 7 份",
                "schedule": "systemd backup.timer（每日 03:30，见 deploy/systemd/）",
            },
            "database": {
                "file": DB_NAME,
                "source": os.path.abspath(db_path),
                "bytes": stats["db_bytes"],
                "sha256": stats["db_sha256"],
                "integrity_check": db_info["integrity_check"],
                "journal_mode": db_info["journal_mode"],
                "tables": db_info["tables"],
                "row_counts": db_info["row_counts"],
            },
            "uploads": {
                "file": UPLOADS_ARCHIVE,
                "source": os.path.abspath(uploads_dir),
                "file_count": stats["uploads_files"],
                "uncompressed_bytes": stats["uploads_bytes"],
                # 内容凭证：条目名 -> sha256。只记"几个文件"挡不住
                # "换成同数量的另一个 zip"，那是真正需要判 FAIL 的场景。
                "entries": zip_manifest_of(zip_path) or {},
            },
            "env_file": env_meta,
            "env_warning": stats["env_warning"],
            "restore_hint": (
                "恢复数据库（**先停后端**）：\n"
                "  sudo systemctl stop ai-interview\n"
                "  cp %s/%s %s\n"
                "  sudo chown interview:interview %s\n"
                "  sudo systemctl start ai-interview\n"
                "恢复上传物：unzip -o %s/%s -d <backend>/uploads/\n"
                "恢复配置：   cp %s/%s <backend>/.env && chmod 600 <backend>/.env"
                % (final_dir.replace("\\", "/"), DB_NAME, "<backend>/interview.db",
                   "<backend>/interview.db",
                   final_dir.replace("\\", "/"), UPLOADS_ARCHIVE,
                   final_dir.replace("\\", "/"), ENV_NAME)
            ),
        }
        with open(os.path.join(tmp_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        log("      -> %s" % MANIFEST_NAME)

        log("[5/5] 落盘 + 轮转 ...")
        # `os.replace()` 对"非空目录"会抛 ENOTEMPTY/OSError，因此同名目录
        # （同一秒内补跑第二次）必须先让路。刻意**不删**它：改名成 `.incomplete-`
        # 前缀交给下一轮的 `cleanup_incomplete()` 收拾，这样万一 rename 之后
        # 就崩了，旧的那份备份仍然完整地躺在磁盘上，新的一份也能重新生成 ——
        # 用"先删旧的再放新的"就会留下一个真实存在过的数据空窗。
        if os.path.exists(final_dir):
            superseded = os.path.join(out_dir, TMP_PREFIX + stamp + "-superseded")
            if os.path.exists(superseded):
                shutil.rmtree(superseded, ignore_errors=True)
            os.replace(final_dir, superseded)
            log("      同名目录已存在（同一秒内重复运行）—— 旧的一份改名为 %s，"
                "由下次运行清理" % os.path.basename(superseded))
        os.replace(tmp_dir, final_dir)
        log("      备份完成: %s" % final_dir)
        stats["pruned"] = prune(out_dir, keep, protect=[stamp], dry_run=False, log=log)
        remaining = list_backup_dirs(out_dir)
        log("      当前共 %d 份备份（上限 %d）: %s"
            % (len(remaining), keep,
               ", ".join(remaining) if remaining else "(无)"))
    except Exception as exc:  # noqa: BLE001
        # 失败时把半成品留在原地（不删！）—— 排障时那些文件是证据，
        # 而下次运行会先把它们清掉，不会无限堆积。
        log("ERROR: 备份失败: %s: %s" % (type(exc).__name__, exc))
        return None, 2

    log("")
    log("结论: BACKUP_OK dir=%s" % final_dir)
    if stats["env_warning"]:
        log("      ⚠️ .env 未备份：%s" % stats["env_warning"])
    return stats, 0


def run_verify(out_dir, log=print):
    """校验目录下**所有**备份，返回 (ok_count, bad_list)。"""
    names = list_backup_dirs(out_dir)
    if not names:
        log("没有找到任何备份目录（%s）" % out_dir)
        return 0, []
    log("校验 %d 份备份（%s）" % (len(names), out_dir))
    bad = []
    for name in names:
        ok, problems, info = verify_backup_dir(os.path.join(out_dir, name))
        mark = "OK  " if ok else "FAIL"
        rows = info.get("row_counts") or {}
        log("  [%s] %s  库行数=%s 上传=%s 份"
            % (mark, name, json.dumps(rows, ensure_ascii=False) if rows else "(未知)",
               info.get("uploads_files", "?")))
        if not ok:
            for p in problems:
                log("         - %s" % p)
            bad.append(name)
    log("校验结论: %d 份通过 / %d 份不健康" % (len(names) - len(bad), len(bad)))
    return len(names) - len(bad), bad


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        description="每日备份 + 保留 N 份（ADR-019 / NFR-8）")
    ap.add_argument("--db", default=os.path.join(here, "interview.db"),
                    help="目标数据库（默认 backend/interview.db）")
    ap.add_argument("--uploads", default=os.path.join(here, "uploads"),
                    help="上传物目录（默认 backend/uploads）")
    ap.add_argument("--env-file", default=os.path.join(here, ".env"),
                    help="配置文件（默认 backend/.env）")
    ap.add_argument("--out", default=None,
                    help="备份根目录（默认 <仓库根>/backup）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="保留份数（默认 %d，ADR-019）" % DEFAULT_KEEP)
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告将要做什么，不写文件、不删目录")
    ap.add_argument("--verify", action="store_true",
                    help="只校验已有备份是否可恢复，不做新备份")
    ap.add_argument("--require-env", action="store_true",
                    help=".env 备份失败即判失败（部署前自检用；默认只告警）")
    ap.add_argument("--no-lock", action="store_true",
                    help="不加单实例锁（仅供测试/排障，正常不要用）")
    ap.add_argument("--stale-after", type=int, default=DEFAULT_STALE_AFTER_SECONDS,
                    help="锁文件超过多少秒视为遗留并抢占（默认 %d）"
                         % DEFAULT_STALE_AFTER_SECONDS)
    return ap


def default_out_dir():
    """`<仓库根>/backup`（脚本在 backend/scripts/ 下，往上三层）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "backup")


def main(argv=None):
    # 先固定输出编码再打印：Python 在输出被管道/文件捕获时会按
    # `locale.getpreferredencoding()`（本机 cp936）编码，无视控制台代码页 ——
    # 迁移日志（T-14）当年就是这么变成乱码的。备份日志是取证材料，
    # 归属 journalctl（UTF-8）时更不能乱。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    db_path = os.path.abspath(args.db)
    uploads_dir = os.path.abspath(args.uploads)
    env_path = os.path.abspath(args.env_file)
    out_dir = os.path.abspath(args.out or default_out_dir())

    print("备份根目录: %s" % out_dir)
    print("数据库    : %s" % db_path)

    if args.verify:
        total, bad = run_verify(out_dir)
        return 1 if bad else 0

    if not os.path.isfile(db_path):
        print("ERROR: 数据库不存在", file=sys.stderr)
        return 2

    lock = lock_path_for(db_path, BACKUP_LOCK_SUFFIX)
    held = False
    if args.dry_run:
        # dry-run 不取锁：它是只读的，与正在跑的那次并存没有危害，
        # 而"想看看到底会做什么"往往正是怀疑备份卡住时最需要的操作。
        print("（dry-run 不取单实例锁：只读操作与正在跑的备份并不冲突）")
    elif not args.no_lock:
        held = acquire_lock(lock, args.stale_after)
        if not held:
            print("结果: 跳过（已有另一个备份实例在运行）")
            return 3

    try:
        stats, code = run_backup(
            db_path, uploads_dir, env_path, out_dir,
            keep=args.keep, dry_run=args.dry_run,
            require_env=args.require_env,
        )
        if code == 0 and not args.dry_run and stats:
            # 落盘后立刻回读校验：证明"我刚写下的这份**现在**就是可恢复的"，
            # 而不是等三个月后要恢复时才发现是坏的。
            ok, problems, _ = verify_backup_dir(stats["backup_dir"])
            print("")
            print("自校验: %s" % ("通过（这份备份现在就能恢复）" if ok else "失败"))
            for p in problems:
                print("  - %s" % p, file=sys.stderr)
            if not ok:
                return 2
        return code
    finally:
        if held:
            release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())

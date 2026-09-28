"""T-14：轻量迁移框架（核心库）。

为什么不用 Alembic（ADR-009R，详见 docs/02-architecture-v2.md）：
  本机 PyPI 与 5 个国内镜像全部 TLS 被重置，alembic / Mako / MarkupSafe 装不上。
  本项目迁移数量很少（基线 + 若干次结构变更），Alembic 的 autogenerate 优势
  体现不出来，而"把核心迁移能力押在一个装不上的依赖上"风险过高。
  因此自建约 200 行的迁移机制：**零新增依赖、离线可用、行为可测**。

能力对照 Alembic：
  版本化 ✓   顺序执行 ✓   幂等 ✓   已应用记录 ✓   事务 ✓   预检 ✓   备份 ✓
  autogenerate ✗（迁移手写 SQL，本项目量级可接受）
  downgrade ✗（本项目只前进；如需回滚，用 T-01 的备份恢复）

数据安全设计（见 run() 的多道闸）：
  1. 迁移前强制备份（可 --skip-backup，但真实库上默认开启）
  2. 只读预检：结构不符合预期则中止，不猜测
  3. 事务包裹：单个迁移失败整体回滚
  4. 只执行迁移脚本里显式写出的语句，框架自身绝不 DROP/DELETE
  5. 幂等：已记录的版本直接跳过

T-18 新增的三道闸（为"重建表"这类结构变更准备，全部在事务内、失败即整体回滚）：
  6. **行数不得减少**：迁移前后逐表比对行数，任何既有表少了行就中止。
     重建表（DROP + RENAME）最容易出的错就是 `INSERT INTO new SELECT` 少拷了行，
     而它在提交后**完全静默** —— 这道闸把它变成不可能提交。
  7. **声明式自检**：迁移可声明 `VERIFY_STATEMENTS = [(SQL, 期望值), ...]`，
     逐条执行比对。用于断言"列数正确 / 该死的列确实没了 / 没有残留 _new 表"。
  8. **foreign_key_check 必须为空**：在 COMMIT 之前跑，不通过就中止。

    这道闸尤其必要，因为**迁移期间外键强制是关闭的**（实测：runner 用的是裸
    `sqlite3.connect()`，`PRAGMA foreign_keys` 默认 0；只有应用引擎的连接才被
    T-15 的 connect 事件设为 ON）。而 `PRAGMA foreign_key_check` 是**诊断**，
    与那个开关无关，因此在迁移里可以照常当安全闸用。

三个可选的"例外声明"（不声明就走最严的默认值）：
    ALLOWS_DATA_LOSS = True        确实需要删行的迁移（如清理任务）必须显式声明
    ALLOWS_TABLE_REBUILD = True    允许脚本里出现 DROP TABLE（重建表的固有步骤）
    REBUILD_REASON = "..."         声明重建时**必须**给出理由，否则加载期直接报错
"""

import datetime
import importlib.util
import os
import sqlite3
from typing import Dict, List, NamedTuple, Optional, Tuple

VERSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "versions")

BASELINE_REVISION = "001"

# 基线迁移之前就已存在的表；用于判断"这是个既有库，应当 stamp 而不是建表"
PRE_BASELINE_TABLES = {"users", "interview_records", "notifications"}


class Migration(NamedTuple):
    revision: str
    description: str
    down_revision: Optional[str]
    upgrade: List[str]
    downgrade: List[str]
    #: T-18：声明式自检 —— [(SQL, 期望标量), ...]，在 COMMIT 前逐条比对。
    verify: List[Tuple[str, object]] = []
    #: T-18：确实需要删行时才声明（默认 False = 任何表行数减少都中止）。
    allows_data_loss: bool = False
    #: T-18：允许脚本里出现 DROP TABLE（重建表的固有步骤）。
    allows_table_rebuild: bool = False
    #: T-18：声明重建时的理由（非空才允许加载）。
    rebuild_reason: str = ""


class MigrationError(Exception):
    """迁移过程中的可预期错误（用于给出可读信息，而非堆栈）。"""


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

def load_migrations(versions_dir: str = VERSIONS_DIR) -> List[Migration]:
    """从 versions/ 目录加载全部迁移，按 revision 升序返回。"""
    if not os.path.isdir(versions_dir):
        raise MigrationError("迁移目录不存在：%s" % versions_dir)

    found: Dict[str, Migration] = {}
    for fname in sorted(os.listdir(versions_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(versions_dir, fname)
        spec = importlib.util.spec_from_file_location("mig_%s" % fname[:-3], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr in ("REVISION", "DESCRIPTION", "UPGRADE_STATEMENTS"):
            if not hasattr(module, attr):
                raise MigrationError("%s 缺少必需属性 %s" % (fname, attr))

        rev = module.REVISION
        if rev in found:
            raise MigrationError("revision 重复：%s（%s 与已有迁移冲突）" % (rev, fname))

        # T-18：重建表是"声明式例外"，必须给出理由 —— 否则加载期就拒绝。
        # 目的是让每一次 DROP TABLE 都在代码里留下"为什么"的痕迹。
        allows_rebuild = bool(getattr(module, "ALLOWS_TABLE_REBUILD", False))
        reason = (getattr(module, "REBUILD_REASON", "") or "").strip()
        if allows_rebuild and not reason:
            raise MigrationError(
                "%s 声明了 ALLOWS_TABLE_REBUILD 但未给出 REBUILD_REASON —— "
                "重建表必须写明理由" % fname
            )

        verify = []
        for item in getattr(module, "VERIFY_STATEMENTS", []) or []:
            if not (isinstance(item, (tuple, list)) and len(item) == 2):
                raise MigrationError(
                    "%s 的 VERIFY_STATEMENTS 每项必须是 (SQL, 期望值)：%r" % (fname, item)
                )
            verify.append((item[0], item[1]))

        found[rev] = Migration(
            revision=rev,
            description=module.DESCRIPTION,
            down_revision=getattr(module, "DOWN_REVISION", None),
            upgrade=list(module.UPGRADE_STATEMENTS),
            downgrade=list(getattr(module, "DOWNGRADE_STATEMENTS", [])),
            verify=verify,
            allows_data_loss=bool(getattr(module, "ALLOWS_DATA_LOSS", False)),
            allows_table_rebuild=allows_rebuild,
            rebuild_reason=reason,
        )
    return [found[r] for r in sorted(found)]


# --------------------------------------------------------------------------
# 数据库检查
# --------------------------------------------------------------------------

def _table_names(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn, table: str) -> List[str]:
    return [d[1] for d in conn.execute("PRAGMA table_info(%s)" % table)]


def _ensure_version_table(conn):
    """仅在**写路径**调用。只读路径（status/preflight）绝不能走到这里。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  revision TEXT PRIMARY KEY,"
        "  description TEXT,"
        "  applied_at TEXT NOT NULL"
        ")"
    )


def applied_revisions(conn) -> List[str]:
    """读取已应用的版本。**纯只读**：版本表不存在时返回空列表。

    （初版这里调用了 _ensure_version_table，导致只读连接上 status()
     报 "attempt to write a readonly database"。）
    """
    if "schema_migrations" not in _table_names(conn):
        return []
    rows = conn.execute("SELECT revision FROM schema_migrations ORDER BY revision").fetchall()
    return [r[0] for r in rows]


def detect_state(conn, migrations: List[Migration]) -> Tuple[str, str]:
    """返回 (state, detail)。

    state 取值：
      empty    —— 空库，需要完整执行基线
      legacy   —— 既有库但无版本记录，应当 stamp 基线（**不执行 DDL**）
      managed  —— 已有版本记录，走正常 upgrade
      partial  —— 只有部分业务表（异常，拒绝自动处理）
    """
    tables = _table_names(conn)
    business = tables & PRE_BASELINE_TABLES
    applied = applied_revisions(conn)
    has_version_table = "schema_migrations" in tables

    if applied:
        return "managed", "已应用：%s" % ", ".join(applied)

    if not business:
        return "empty", "空库（无业务表）"

    if business == PRE_BASELINE_TABLES:
        return "legacy", "既有库（3 张业务表齐备，但无版本记录）"

    return "partial", "结构不完整：只发现 %s" % ", ".join(sorted(business))


def preflight(conn, migrations: List[Migration]):
    """只读预检：把"将要做什么"算清楚，不修改任何数据。"""
    state, detail = detect_state(conn, migrations)
    applied = applied_revisions(conn)

    if state == "managed":
        pending = [m for m in migrations if m.revision not in applied]
    elif state == "empty":
        pending = list(migrations)
    elif state == "legacy":
        pending = [m for m in migrations if m.revision != BASELINE_REVISION]
    else:
        pending = []

    return {
        "state": state,
        "detail": detail,
        "applied": applied,
        "pending": [m.revision for m in pending],
        "will_stamp_baseline": state == "legacy",
    }


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _record(conn, migration: Migration):
    conn.execute(
        "INSERT INTO schema_migrations (revision, description, applied_at) VALUES (?, ?, ?)",
        (migration.revision, migration.description, datetime.datetime.utcnow().isoformat()),
    )


# --------------------------------------------------------------------------
# T-18 安全闸（全部在事务内执行，任何一项不通过 -> 抛错 -> 整体回滚）
# --------------------------------------------------------------------------

def _row_counts(conn) -> Dict[str, int]:
    """当前各业务表的行数（排除 sqlite_* 内部表）。"""
    return {
        t: conn.execute("SELECT count(*) FROM [%s]" % t).fetchone()[0]
        for t in sorted(_table_names(conn))
    }


def _check_no_row_loss(before: Dict[str, int], after: Dict[str, int],
                       migration: Migration) -> int:
    """安全闸 6：任何既有表的行数**不得减少**。

    重建表的典型事故是 `INSERT INTO new SELECT ...` 少拷了行（列顺序错、
    WHERE 写漏、类型不匹配被静默跳过），随后旧表被 DROP —— 数据就**永久**
    没了，而且迁移会"成功"返回。这道闸把这种事故变成"不可能提交"。
    """
    if migration.allows_data_loss:
        return len(before)

    lost = {}
    for table, n in before.items():
        m = after.get(table, 0)
        if m < n:
            lost[table] = (n, m)
    if lost:
        detail = "; ".join("%s: %d -> %d" % (t, a, b) for t, (a, b) in sorted(lost.items()))
        raise MigrationError(
            "迁移 %s 让既有表行数减少，已中止并回滚 —— %s。"
            "若确实需要删行，请在迁移里显式声明 ALLOWS_DATA_LOSS = True"
            % (migration.revision, detail)
        )
    return len(before)


def _run_verifications(conn, migration: Migration) -> int:
    """安全闸 7：逐条执行迁移声明的自检 SQL。"""
    for sql, expected in migration.verify:
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error as exc:
            raise MigrationError(
                "迁移 %s 的自检 SQL 执行失败：%s\n  SQL: %s"
                % (migration.revision, exc, " ".join(sql.split()))
            )
        actual = row[0] if row else None
        if actual != expected:
            raise MigrationError(
                "迁移 %s 自检未通过：期望 %r，实得 %r\n  SQL: %s"
                % (migration.revision, expected, actual, " ".join(sql.split()))
            )
    return len(migration.verify)


def _check_foreign_keys(conn, migration: Migration) -> int:
    """安全闸 8：`PRAGMA foreign_key_check` 必须无违规。

    为什么必须有：**迁移期间外键强制是关闭的**（runner 用裸 sqlite3.connect()，
    `PRAGMA foreign_keys` 默认 0）。也就是说迁移写进去的违约数据不会被当场拒绝。
    `foreign_key_check` 是诊断语句，与那个开关无关，因此可以在这里补上检查。
    """
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        sample = "; ".join(
            "%s(rowid=%s) -> %s" % (v[0], v[1], v[2]) for v in violations[:5]
        )
        raise MigrationError(
            "迁移 %s 产生了 %d 条外键违规，已中止并回滚：%s"
            % (migration.revision, len(violations), sample)
        )
    return 0


def run(db_path: str, *, backup: bool = True, dry_run: bool = False,
        versions_dir: str = VERSIONS_DIR, log=print) -> int:
    """把数据库升级到最新版本。返回已应用的迁移数量。"""
    migrations = load_migrations(versions_dir)
    if not migrations:
        raise MigrationError("没有找到任何迁移脚本")
    if migrations[0].revision != BASELINE_REVISION:
        raise MigrationError("第一个迁移必须是基线 %s" % BASELINE_REVISION)

    if not os.path.isfile(db_path):
        # 允许对不存在的库做初始化（SQLite 会自动创建文件）
        log("  [info] 目标数据库不存在，将新建：%s" % db_path)

    if backup and not dry_run and os.path.isfile(db_path):
        _do_backup(db_path, log)

    conn = sqlite3.connect(db_path)
    try:
        plan = preflight(conn, migrations)

        log("  状态      : %s（%s）" % (plan["state"], plan["detail"]))
        if plan["applied"]:
            log("  已应用    : %s" % ", ".join(plan["applied"]))
        if plan["will_stamp_baseline"]:
            log("  -> 既有库：将只 **记录** 基线 %s，不执行任何 DDL" % BASELINE_REVISION)
        if plan["pending"]:
            log("  待应用    : %s" % ", ".join(plan["pending"]))
        else:
            log("  待应用    : （无，已是最新）")

        if plan["state"] == "partial":
            raise MigrationError(
                "结构不完整，拒绝自动处理：%s。请人工确认后再迁移。" % plan["detail"]
            )

        if dry_run:
            log("  [dry-run] 未做任何修改")
            return 0

        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_version_table(conn)
            count = 0

            if plan["will_stamp_baseline"]:
                baseline = next(m for m in migrations if m.revision == BASELINE_REVISION)
                _record(conn, baseline)
                count += 1

            for migration in migrations:
                if migration.revision in applied_revisions(conn):
                    continue
                log("  应用 %s : %s" % (migration.revision, migration.description))
                # T-18：迁移前快照行数（用于安全闸 6）
                before_counts = _row_counts(conn)
                for stmt in migration.upgrade:
                    conn.execute(stmt)
                # 三道闸都在 COMMIT 之前，任一不通过即抛错 -> 整体回滚
                checked = _check_no_row_loss(before_counts, _row_counts(conn), migration)
                n_verify = _run_verifications(conn, migration)
                _check_foreign_keys(conn, migration)
                _record(conn, migration)
                count += 1
                log("     自检: 声明式 %d 项通过 | 行数无减少（%d 张表）| "
                    "foreign_key_check 无违规" % (n_verify, checked))

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log("  完成，应用了 %d 个迁移" % count)
        return count
    finally:
        conn.close()


def status(db_path: str, versions_dir: str = VERSIONS_DIR) -> dict:
    """只读查看迁移状态，不修改任何数据。"""
    migrations = load_migrations(versions_dir)
    if not os.path.isfile(db_path):
        return {"state": "missing", "detail": "数据库文件不存在", "applied": [], "pending": []}
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/"), uri=True)
    try:
        return preflight(conn, migrations)
    finally:
        conn.close()


def _do_backup(db_path: str, log=print):
    """迁移前强制备份；失败则中止（安全闸 1）。"""
    import sys

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from scripts.make_backup import backup_database, find_repo_root, inspect_db

    repo_root = find_repo_root()
    out_dir = os.path.join(repo_root, "backup")
    os.makedirs(out_dir, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(out_dir, "pre-migration_%s.db" % stamp)

    log("  [备份] 迁移前备份 -> %s" % os.path.basename(dst))
    backup_database(db_path, dst)
    info = inspect_db(dst)
    if info["integrity_check"] != "ok":
        raise MigrationError("迁移前备份完整性检查失败，已中止：%s" % info["integrity_check"])
    log("  [备份] 完成，integrity_check=ok，行数=%s" % info["row_counts"])

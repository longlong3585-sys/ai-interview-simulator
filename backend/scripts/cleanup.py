"""T-22：数据生命周期清理（单次执行 + 单实例互斥）。

## 它清理什么（依据 docs/02-architecture.md §6.3）

| 表 | 过期依据 | 清理动作 |
|---|---|---|
| `interview_sessions` | `expires_at <= now` | **置 `abandoned`**（不是删除）—— 释放 `UNIQUE(user_id) WHERE status='active'` 的部分唯一索引，让用户能开新面试 |
| `captcha_store` | `expires_at <= now` | **删除**行 |
| `auth_attempts` | `attempted_at < now - 窗口` | **删除**行 |

⚠️ **清理只传 `now`，不再自己减 TTL。** 会话的 2 小时、验证码的 300 秒
都已经写进了各自的 `expires_at`（创建时算好）。清理的职责只是
"把已经越过 `expires_at` 的处理掉"。首版在这里多减了一次 TTL
（`now - 2h`），后果是**过期会话要再等 2 小时才释放唯一锁** ——
正好废掉 ADR-022R"用户永远能开新面试"的承诺。

## 为什么不用 APScheduler

每个 uvicorn worker 都会跑一份调度器 → 同一个清理任务被并发触发 N 次。
本项目的做法是"**外部定时器 + 一次性进程**"：systemd timer（Linux）
或手动执行（Windows 开发机）。进程退出即结束，没有常驻调度状态。

## 单实例是怎么保证的（两层）

1. **应用层文件锁**（本文件，跨平台）：`O_CREAT|O_EXCL` 原子创建锁文件。
   拿不到锁就退出（退出码 3），**什么都不做**。
2. **systemd `Type=oneshot`**（Linux，见 `deploy/systemd/cleanup.service`）：
   systemd 在一个 unit 实例仍在运行时不会启动第二个。

只有第 1 层时 Windows 上也能防重入；只有第 2 层时手动执行可能撞车。
两层都在，是刻意的冗余。

### 锁的失效与抢占

锁文件里写 PID 只用于**排查**，**不用它判断进程存活** ——
`os.kill(pid, 0)` 在 Windows 上不是"检查是否存在"，而是会真的去终止进程；
拿它做存活探测是危险的（典型的"看着跨平台、其实语义不同"的 API）。

取而代之的是**按年龄判断**：锁文件比 `--stale-after`（默认 1 小时）还老，
就认为是上次崩溃留下的，直接抢占并在日志里说明。
代价：崩溃后最长 1 小时内不会再清理。可接受 —— 清理是幂等的、
每 15 分钟一次的兜底动作，晚一次没有正确性影响。

## 为什么每个动作都走 `services/stores` 而不是直接写 SQL

本项目的规矩是"业务代码只依赖协议"。清理脚本同样如此：四个动作分别调用
`SessionStore.abandon_all_expired` / `CaptchaStore.purge_expired` /
`RateLimitStore.purge_older_than` / `TokenBlacklistStore.purge_expired`。
将来换存储载体时这个脚本一行都不用改。

## 已知未覆盖项（如实声明，不假装完整）

T-51 起 `token_blacklist` **已纳入清理**（见上一节）；此前"刻意不清理"的理由
（协议未定义、表是空的）已随 T-51 落地而消失。

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\cleanup.py --dry-run     # 只报告，不改动
    .\\venv\\Scripts\\python.exe scripts\\cleanup.py               # 真正执行
    .\\venv\\Scripts\\python.exe scripts\\cleanup.py --db <路径>   # 指定库

退出码
    0 正常完成（含"本次没有需要清理的东西"）
    2 参数/环境错误
    3 已有另一个实例在跑（本次跳过，**不算失败** —— systemd 不该因此告警）
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.single_instance import (  # noqa: E402
    CLEANUP_LOCK_SUFFIX as LOCK_SUFFIX,
)
from scripts.single_instance import (  # noqa: E402
    DEFAULT_STALE_AFTER_SECONDS,
    acquire_lock,
    lock_path_for as _lock_path_for,
    release_lock,
)
from services.stores.base import iso_after, utcnow_iso  # noqa: E402

#: 限流窗口长度（§6.3：10 分钟）。清理只删**窗口外**的记录，
#: 与 `count_failures(since=now-窗口)` 的判据严格互补。
#: （会话的 2 小时与验证码的 300 秒不需要在这里出现：它们已经写进各自的
#:  `expires_at`，清理只按 `expires_at` 判断。）
ATTEMPT_WINDOW_SECONDS = 10 * 60


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------
#
# T-52 起，锁的实现**搬到了 `scripts/single_instance.py`**，因为 T-52 的
# 备份轮转脚本需要**完全一样的语义**（原子创建 / 按年龄抢占遗留锁 /
# 失败也放锁）。搬走时逐字保留了行为，这里只做委派：
#
#   * `DEFAULT_STALE_AFTER_SECONDS` 与 `acquire_lock` / `release_lock`
#     直接从 `single_instance` 导入（名字不变，老调用方与测试照常工作）；
#   * 下面两个薄封装保留，是为了让"锁文件叫 `.cleanup.lock`"这条 T-22
#     的既有约定不泄漏到本文件之外。

def lock_path_for(db_path):
    """清理专用的锁文件路径（后缀保持 `.cleanup.lock` 不变）。

    改后缀的后果不是"少了个文件"，而是**把上一轮仍在运行这件事忘掉** ——
    新老版本共存时会同时写同一个库。
    """
    return _lock_path_for(db_path, LOCK_SUFFIX)


# ---------------------------------------------------------------------------
# 引擎与仓储
# ---------------------------------------------------------------------------

def build_session_factory(db_path):
    """按 T-15 的 engine 契约建一个**指向指定库**的 sessionmaker。

    为什么不用 `os.environ["DATABASE_URL"]` 那种写法：`database.py` 在
    **import 期**就把 `DATABASE_URL` 读进模块变量了，之后再改环境变量无效 ——
    首版正是这么写的，导致 `--db` 其实指向默认库（静默跑错目标）。
    这里显式构造，目标库一目了然，也不依赖 import 顺序。
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    import database  # 只为复用它的 PRAGMA 实现与连接参数

    engine = create_engine(
        "sqlite:///" + os.path.abspath(db_path).replace("\\", "/"),
        connect_args=dict(database.SQLITE_CONNECT_ARGS),
        pool_pre_ping=True,
        isolation_level=None,
    )
    event.listen(engine, "connect", database._apply_sqlite_pragmas)

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def run_cleanup(db_path, dry_run=False, session_factory=None, engine=None, log=print):
    """执行三类清理，返回统计字典。

    `dry_run=True` 时**不改动任何数据**，只报告"将会清理多少"。
    计数用的判据与仓储内部**逐字相同**（同一段 WHERE），
    避免"预览说 3 条、真跑删 5 条"的口径偏差。

    `session_factory` / `engine` 可注入（测试用）；不传则按 `db_path` 自建。
    """
    from sqlalchemy import text

    from services.stores.sqlite_captcha_store import SQLiteCaptchaStore
    from services.stores.sqlite_rate_limit_store import SQLiteRateLimitStore
    from services.stores.sqlite_store import SQLiteSessionStore
    from services.stores.sqlite_token_blacklist_store import SQLiteTokenBlacklistStore

    own_engine = None
    if session_factory is None or engine is None:
        own_engine, built = build_session_factory(db_path)
        session_factory = session_factory or built
        engine = engine or own_engine

    now = utcnow_iso()
    attempt_cutoff = iso_after(-ATTEMPT_WINDOW_SECONDS, now)

    try:
        if dry_run:
            with engine.connect() as conn:
                return {
                    "sessions_abandoned": conn.execute(
                        text("SELECT count(*) FROM interview_sessions "
                             "WHERE status = 'active' AND expires_at <= :now"),
                        {"now": now}).scalar(),
                    "captchas_purged": conn.execute(
                        text("SELECT count(*) FROM captcha_store "
                             "WHERE expires_at <= :now"),
                        {"now": now}).scalar(),
                    "attempts_purged": conn.execute(
                        text("SELECT count(*) FROM auth_attempts "
                             "WHERE attempted_at < :before"),
                        {"before": attempt_cutoff}).scalar(),
                    "blacklist_purged": conn.execute(
                        text("SELECT count(*) FROM token_blacklist "
                             "WHERE expires_at <= :now"),
                        {"now": now}).scalar(),
                }

        return {
            "sessions_abandoned":
                SQLiteSessionStore(session_factory).abandon_all_expired(now),
            "captchas_purged":
                SQLiteCaptchaStore(session_factory).purge_expired(now),
            "attempts_purged":
                SQLiteRateLimitStore(session_factory).purge_older_than(attempt_cutoff),
            "blacklist_purged":
                SQLiteTokenBlacklistStore(session_factory).purge_expired(now),
        }
    finally:
        if own_engine is not None:
            own_engine.dispose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="数据生命周期清理（会话置 abandoned / 验证码、限流与令牌吊销记录清除）")
    ap.add_argument("--db", default=None,
                    help="目标数据库路径（默认 backend/interview.db）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告将要清理多少，不改动任何数据")
    ap.add_argument("--stale-after", type=int, default=DEFAULT_STALE_AFTER_SECONDS,
                    help="锁文件超过多少秒视为遗留并抢占（默认 %d）"
                         % DEFAULT_STALE_AFTER_SECONDS)
    ap.add_argument("--no-lock", action="store_true",
                    help="不加单实例锁（仅供测试/排障，正常不要用）")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.abspath(args.db or os.path.join(backend_dir, "interview.db"))

    print("清理目标库: %s" % db_path)
    if not os.path.exists(db_path):
        print("ERROR: 数据库不存在", file=sys.stderr)
        return 2

    print("模式: %s" % ("dry-run（不改动）" if args.dry_run else "执行"))

    lock_path = lock_path_for(db_path)
    held = False
    if args.dry_run:
        # dry-run 不取锁：它是只读的，与正在运行的清理并存没有危害，
        # 而"想看看到底有多少要清"往往正是清理疑似卡住时最需要的操作。
        print("（dry-run 不取单实例锁：只读操作与正在运行的清理并不冲突）")
    elif not args.no_lock:
        held = acquire_lock(lock_path, args.stale_after)
        if not held:
            print("结果: 跳过（已有另一个实例在运行）")
            return 3

    started = datetime.datetime.now()
    try:
        stats = run_cleanup(db_path, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print("ERROR: 清理失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    finally:
        if held:
            release_lock(lock_path)

    elapsed = (datetime.datetime.now() - started).total_seconds()
    print("")
    print("  过期会话置 abandoned : %d" % stats["sessions_abandoned"])
    print("  过期验证码清除       : %d" % stats["captchas_purged"])
    print("  窗口外失败记录清除   : %d" % stats["attempts_purged"])
    print("  失效吊销记录清除     : %d" % stats["blacklist_purged"])
    print("")
    print("耗时 %.2f 秒" % elapsed)
    print("结论: %s" % ("DRY-RUN 完成（未改动任何数据）" if args.dry_run
                        else "CLEANUP_OK"))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())

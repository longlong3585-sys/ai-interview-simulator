"""T-14 迁移入口（CLI）。

用法（在 backend/ 目录下执行）：

    python scripts/migrate.py --status              # 只读查看状态
    python scripts/migrate.py --dry-run             # 只报告将要做什么
    python scripts/migrate.py                       # 升级到最新（先自动备份）
    python scripts/migrate.py --db <path> --skip-backup   # 指定库且不备份（测试用）

安全默认值：
  - 对真实库默认**先备份**，备份失败即中止
  - 既有库的基线只 stamp，不执行 DDL
  - 结构不完整（partial）时拒绝自动处理
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrations.runner import MigrationError, run, status  # noqa: E402

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "interview.db"
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lightweight SQLite migration runner (T-14).")
    ap.add_argument("--db", default=DEFAULT_DB, help="target database path")
    ap.add_argument("--status", action="store_true", help="print status and exit (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="report planned work, change nothing")
    ap.add_argument("--skip-backup", action="store_true", help="do NOT back up before migrating")
    args = ap.parse_args(argv)

    print("database: %s" % args.db)

    try:
        if args.status:
            info = status(args.db)
            print("  state    : %s" % info["state"])
            print("  detail   : %s" % info["detail"])
            print("  applied  : %s" % (", ".join(info["applied"]) or "(none)"))
            print("  pending  : %s" % (", ".join(info["pending"]) or "(none)"))
            if info.get("will_stamp_baseline"):
                print("  note     : legacy database -> baseline will be STAMPED only")
            return 0

        run(args.db, backup=not args.skip_backup, dry_run=args.dry_run)
        return 0
    except MigrationError as exc:
        print("\nMIGRATION_ABORTED: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""T-01 回滚点备份脚本。

用途：在开始任何重构之前，生成可独立恢复的备份点：
  1. 数据库备份（使用 sqlite3 官方 backup API，**WAL 安全**）
  2. 源码归档（zip，排除依赖与产物目录）
  3. 清单文件（manifest.json，含 commit、行数、校验和）

用法：
    python backend/scripts/make_backup.py
    python backend/scripts/make_backup.py --label before-refactor
    python backend/scripts/make_backup.py --no-archive

注意：源码归档**刻意排除 .env**（内含 DeepSeek API Key 与 SECRET_KEY）。
      .env 不属于重构变更范围，恢复时请单独保留。
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile

# ---- 归档时排除的目录名 / 文件名 / 后缀 ----
EXCLUDE_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__",
    "dist", "build", "backup", ".pytest_cache", ".mypy_cache",
    ".vscode", ".idea", "htmlcov",
}
EXCLUDE_FILES = {".env", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".zip"}

# 备份中必须存在的业务表（用于校验完整性）
EXPECTED_TABLES = {"users", "interview_records", "notifications"}


def find_repo_root(start=None):
    """从脚本位置向上找到含 .git 的目录。"""
    here = os.path.abspath(start or os.path.dirname(__file__))
    cur = here
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise RuntimeError("找不到仓库根目录（未发现 .git）")


def git_head(repo_root):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_database(src_db, dst_db):
    """用 sqlite3 backup API 做一致性备份（对 WAL 模式安全）。"""
    if not os.path.isfile(src_db):
        raise FileNotFoundError("数据库不存在: %s" % src_db)
    src = sqlite3.connect(src_db)
    try:
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dst_db


def inspect_db(db_path):
    """读取表清单与行数，作为备份内容凭证。"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        counts = {}
        for t in tables:
            try:
                counts[t] = conn.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
            except sqlite3.Error as exc:
                counts[t] = "ERROR: %s" % exc
        return {
            "integrity_check": integrity,
            "journal_mode": journal,
            "tables": tables,
            "row_counts": counts,
        }
    finally:
        conn.close()


def _excluded(rel_posix):
    parts = rel_posix.split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in EXCLUDE_FILES:
        return True
    return os.path.splitext(name)[1].lower() in EXCLUDE_SUFFIX


def make_source_archive(repo_root, zip_path):
    """打包源码，排除依赖/产物/密钥。"""
    included = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo_root).replace("\\", "/")
                if _excluded(rel):
                    continue
                try:
                    zf.write(full, rel)
                    included += 1
                except OSError:
                    pass
    return included


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create a rollback backup point.")
    ap.add_argument("--label", default="", help="optional label, e.g. before-refactor")
    ap.add_argument("--out", default=None, help="output dir (default <repo>/backup)")
    ap.add_argument("--no-archive", action="store_true", help="skip source zip")
    args = ap.parse_args(argv)

    repo_root = find_repo_root()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ("_" + args.label) if args.label else ""
    out_dir = os.path.abspath(args.out or os.path.join(repo_root, "backup"))
    os.makedirs(out_dir, exist_ok=True)

    src_db = os.path.join(repo_root, "backend", "interview.db")

    print("[1/4] backing up database ...")
    db_name = "interview_%s%s.db" % (stamp, suffix)
    db_dst = os.path.join(out_dir, db_name)
    backup_database(src_db, db_dst)
    db_info = inspect_db(db_dst)
    db_size = os.path.getsize(db_dst)
    print("      -> %s (%.1f KB)" % (db_name, db_size / 1024.0))
    print("      integrity_check = %s" % db_info["integrity_check"])
    print("      row_counts       = %s" % json.dumps(db_info["row_counts"]))

    if db_info["integrity_check"] != "ok":
        print("ERROR: backup failed integrity_check", file=sys.stderr)
        return 2
    missing = EXPECTED_TABLES - set(db_info["tables"])
    if missing:
        print("ERROR: backup missing tables: %s" % sorted(missing), file=sys.stderr)
        return 2

    zip_name = None
    file_count = 0
    if not args.no_archive:
        print("[2/4] archiving source ...")
        zip_name = "source_%s%s.zip" % (stamp, suffix)
        zip_path = os.path.join(out_dir, zip_name)
        file_count = make_source_archive(repo_root, zip_path)
        print("      -> %s (%d files, %.1f KB)"
              % (zip_name, file_count, os.path.getsize(zip_path) / 1024.0))
    else:
        print("[2/4] archiving source ... skipped")

    print("[3/4] writing manifest ...")
    manifest = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "label": args.label or None,
        "repo_root": repo_root,
        "git_head": git_head(repo_root),
        "database": {
            "source": os.path.relpath(src_db, repo_root).replace("\\", "/"),
            "backup_file": db_name,
            "size_bytes": db_size,
            "sha256": sha256_of(db_dst),
            "integrity_check": db_info["integrity_check"],
            "journal_mode": db_info["journal_mode"],
            "tables": db_info["tables"],
            "row_counts": db_info["row_counts"],
        },
        "source_archive": (
            {"file": zip_name, "file_count": file_count} if zip_name else None
        ),
        "excluded_from_archive": {
            "dirs": sorted(EXCLUDE_DIRS),
            "files": sorted(EXCLUDE_FILES),
            "note": ".env excluded on purpose (contains API key / SECRET_KEY); keep it separately.",
        },
        "restore_hint": (
            "python backend/scripts/verify_backup.py --db backup/%s" % db_name
        ),
    }
    manifest_name = "manifest_%s%s.json" % (stamp, suffix)
    manifest_path = os.path.join(out_dir, manifest_name)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("      -> %s" % manifest_name)

    print("[4/4] done.")
    print("BACKUP_OK dir=%s db=%s" % (out_dir, db_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""使 `backend/scripts/` 成为可导入包，便于测试直接复用脚本逻辑。

脚本仍可独立运行，例如：
    python backend/scripts/make_backup.py
    python backend/scripts/verify_backup.py --db <path>
"""

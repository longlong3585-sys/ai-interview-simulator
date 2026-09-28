"""T-15 测试：SQLite engine 运行契约（WAL / busy_timeout / foreign_keys / BEGIN IMMEDIATE）。

修复前 engine 只设了 `check_same_thread=False`，缺了全部运行契约，后果：
  1. 默认 journal 模式读写互斥，多 worker 极易 database is locked
  2. **PRAGMA foreign_keys 默认为 0** —— 模型里的 ForeignKey 形同虚设
  3. 未指定 isolation_level 时 SQLAlchemy 发 "BEGIN (implicit)"，
     永远不发 BEGIN IMMEDIATE，而 ADR-004 的并发控制依赖它
"""

import os
import sqlite3
import sys
import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import database  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from database import InterviewRecord, SessionLocal, User  # noqa: E402


def pragma(conn, name):
    return conn.execute(text("PRAGMA " + name)).scalar()


class EngineConfigTests(unittest.TestCase):

    def test_isolation_level_is_none(self):
        """必须交出事务控制权，否则下面的 begin 事件发不出 BEGIN IMMEDIATE。"""
        self.assertIsNone(
            database.engine.dialect.isolation_level,
            "isolation_level 应为 None（None 表示由 begin 事件自行发 BEGIN）",
        )

    def test_dbapi_isolation_level_is_disabled(self):
        """直接断言底层 pysqlite 的隐式事务被关闭（isolation_level is None）。

        为什么要**直接断言配置**而不是靠行为推断：
        破坏性验证发现，去掉 engine 的 isolation_level=None 之后，
        其余 9 个用例**全部仍然通过** —— 因为 BEGIN IMMEDIATE 是由 `begin`
        事件发出的，与该项无关。该项真正防的是 pysqlite **legacy 模式**的副作用：
        legacy 模式会在 DDL 前自动 COMMIT、并自行隐式开启事务，
        与手工发的 BEGIN IMMEDIATE 混用会产生难以预期的边界行为。
        这类"配置正确性"无法用黑盒行为稳定区分，只能直接断言。
        """
        with database.engine.connect() as conn:
            dbapi_conn = conn.connection.dbapi_connection
            self.assertIsNone(
                dbapi_conn.isolation_level,
                "pysqlite 仍处于 legacy 隐式事务模式（isolation_level=%r）"
                % (dbapi_conn.isolation_level,),
            )

    def test_busy_timeout_single_source(self):
        """busy_timeout 与驱动层 timeout 必须同值（ADR-002），否则等待时间不一致。"""
        expected = database.SQLITE_BUSY_TIMEOUT_MS
        with database.engine.connect() as conn:
            self.assertEqual(pragma(conn, "busy_timeout"), expected)
        self.assertEqual(
            expected, 15000,
            "SQLITE_BUSY_TIMEOUT_MS 应为 15000；若有意修改请同步 docs 中的 ADR-002",
        )
        self.assertEqual(
            database.SQLITE_CONNECT_ARGS.get("timeout"), expected / 1000.0,
            "驱动层 timeout 与 PRAGMA busy_timeout 必须同源同值",
        )

    def test_all_pragmas_applied(self):
        with database.engine.connect() as conn:
            self.assertEqual(str(pragma(conn, "journal_mode")).lower(), "wal")
            self.assertEqual(pragma(conn, "busy_timeout"), database.SQLITE_BUSY_TIMEOUT_MS)
            self.assertEqual(pragma(conn, "synchronous"), 1, "应为 NORMAL(=1)")
            self.assertEqual(pragma(conn, "foreign_keys"), 1, "外键必须开启")

    def test_wal_persists_on_file(self):
        """WAL 是库文件属性：换一个**原始** sqlite3 连接也应看到 wal。"""
        db_path = database.DATABASE_URL.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal"
            )
        finally:
            conn.close()

    def test_every_new_connection_gets_pragmas(self):
        """PRAGMA 是**连接级**的，必须每个新连接都设置。

        注意：**逐个**开关连接，不能同时持有多个。
        因为 BEGIN IMMEDIATE 会让每个事务（含只读）都持写锁，
        同时持有两个连接的事务必然互相阻塞 —— 那是本设计的已知代价，
        由 test_concurrent_transactions_serialize 单独记录，不在这里混测。
        """
        for _ in range(3):
            conn = database.engine.connect()
            try:
                self.assertEqual(pragma(conn, "foreign_keys"), 1)
                self.assertEqual(pragma(conn, "busy_timeout"), database.SQLITE_BUSY_TIMEOUT_MS)
                self.assertEqual(str(pragma(conn, "journal_mode")).lower(), "wal")
            finally:
                conn.close()

    def test_concurrent_transactions_serialize(self):
        """**如实记录已知代价**：BEGIN IMMEDIATE 让每个事务（含只读）都申请写锁，
        因此**经由本引擎的并发事务会串行化**。

        精确表述（实测校正）：
          - WAL 下 RESERVED 锁**不阻塞**其它连接的纯读（读并发仍然存在）
          - 但本引擎的每个事务都会 BEGIN IMMEDIATE，所以两个 API 请求
            （即使都是只读）会互相排队
          - 外部只用 sqlite3 纯读的连接不受影响

        这是 ADR-002 的取舍：换来"读事务升级为写"时不会出现 SQLITE_BUSY
        且 busy_timeout 失效。本项目单机低并发、事务毫秒级，可接受。
        若将来读多写多，应改为"仅写路径显式 BEGIN IMMEDIATE"。
        """
        c1 = database.engine.connect()
        try:
            c1.execute(text("SELECT 1"))  # c1 已持写锁

            # 另一连接尝试申请写锁（模拟第二个 API 请求）
            raw = sqlite3.connect(
                database.DATABASE_URL.replace("sqlite:///", ""), timeout=0.2
            )
            try:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    raw.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(ctx.exception).lower())
            finally:
                raw.close()

            # 对照：纯读不受 RESERVED 锁影响（WAL 的读并发仍然有效）
            reader = sqlite3.connect(
                database.DATABASE_URL.replace("sqlite:///", ""), timeout=0.2
            )
            try:
                reader.execute("SELECT COUNT(*) FROM users").fetchone()
            finally:
                reader.close()
        finally:
            c1.close()


class BeginImmediateTests(unittest.TestCase):
    """核心：验证发出的是 BEGIN IMMEDIATE 而不是 BEGIN (deferred)。

    判别依据是**行为差异**，不依赖日志：
      DEFERRED  : 只读事务不持锁 → 另一连接此时能写入
      IMMEDIATE : 事务一开始就取 RESERVED 锁 → 另一连接写入被挡住
    """

    def setUp(self):
        self.db_path = database.DATABASE_URL.replace("sqlite:///", "")

    def _raw_insert(self, tag):
        """用一个短 busy_timeout 的原始连接尝试写入。"""
        conn = sqlite3.connect(self.db_path, timeout=0.2)
        try:
            conn.execute(
                "INSERT INTO users (username, email, role, is_active) VALUES (?,?,?,1)",
                (tag, tag + "@t.local", "user"),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def tearDown(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM users WHERE username LIKE 't15_%'")
            conn.commit()
        finally:
            conn.close()

    def test_readonly_transaction_holds_write_lock(self):
        sa_conn = database.engine.connect()
        try:
            sa_conn.execute(text("SELECT 1"))  # 开启事务
            blocked = not self._raw_insert("t15_blocked")
            self.assertTrue(
                blocked,
                "只读事务期间另一连接仍能写入 —— 说明发的是 BEGIN (deferred)，"
                "BEGIN IMMEDIATE 未生效；ADR-004 的并发控制会失去保障",
            )
        finally:
            sa_conn.close()

    def test_after_commit_other_connection_can_write(self):
        """对照组：事务结束后，其它连接应立刻能写（证明上一条不是别的原因造成的阻塞）。"""
        sa_conn = database.engine.connect()
        try:
            sa_conn.execute(text("SELECT 1"))
        finally:
            sa_conn.close()  # close 即回滚/释放
        self.assertTrue(
            self._raw_insert("t15_after"),
            "事务结束后另一连接仍无法写入，说明锁没有被释放",
        )


class ForeignKeyEnforcementTests(unittest.TestCase):
    """验证 foreign_keys=ON 真的生效 —— 修复前这些约束**完全不起作用**。"""

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def test_insert_with_nonexistent_user_is_rejected(self):
        self.db.add(InterviewRecord(
            user_id=999999, role="后端", messages="[]", report=None, status="pending",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()

    def test_deleting_user_with_records_is_rejected(self):
        """外键为 NO ACTION：删除仍有记录的用户应被数据库拒绝。

        两个关键点（都由首版失败反推出来）：
          1. 必须走**应用的连接**（SQLAlchemy session）来删 —— PRAGMA foreign_keys
             是**连接级**的，另开一个原始 sqlite3 连接默认是关闭的，
             那样测的其实是"没开外键时能删"，完全没有意义。
          2. 删之前不要把会话的连接借给别的操作（那会因 BEGIN IMMEDIATE 而互相阻塞）。
        """
        user = User(username="t15_fk_user", hashed_password="x",
                    email="t15_fk@test.local", role="user", is_active=True)
        self.db.add(user)
        self.db.commit()
        user_id = user.id

        self.db.add(InterviewRecord(
            user_id=user_id, role="后端", messages="[]", report=None, status="pending",
        ))
        self.db.commit()

        # 经由应用连接执行删除：应被外键拒绝
        with self.assertRaises(IntegrityError):
            self.db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
            self.db.commit()
        self.db.rollback()

        # 清理（先删记录再删用户）
        self.db.query(InterviewRecord).filter(
            InterviewRecord.user_id == user_id
        ).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        self.db.commit()


if __name__ == "__main__":
    unittest.main(verbosity=2)

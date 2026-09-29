"""T-19 测试：`SQLiteSessionStore`。

任务描述里的四个关键词就是本文件的四组核心用例：

  1. **乐观锁 `version`**：两请求拿同一 `version` 并发 → 一个成功、一个
     `applied=False`（上层据此 409），**绝不静默覆盖**。
  2. **`seq` 幂等**：同 `seq` 重发 → `is_replay=True` 且带回上次回复，
     调用方据此不调 AI（也不重复计费）。
  3. **状态机**：`active → finished / abandoned`；终态释放唯一锁。
  4. **短事务**：每个方法各自开关事务，**不把事务泄漏给调用方** ——
     在 T-15 的 `BEGIN IMMEDIATE` 下，泄漏的事务会直接把所有请求堵死。

另外覆盖：过期自愈、TTL 边界、严格 JSON 解析、非法 ended_reason、
`ActiveSessionExists` 的判别不依赖异常文本。

全部用例跑在 `tests/__init__.py` 建立的**临时测试库**上，不碰真库。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services.stores.base import (  # noqa: E402
    ActiveSessionExists,
    EndedReason,
    SessionDraft,
    SessionStatus,
    StoreError,
    TurnCommit,
    iso_after,
    utcnow_iso,
)
from services.stores.sqlite_store import SQLiteSessionStore  # noqa: E402
from tests.support import build_temp_db  # noqa: E402


def _noop(*args, **kwargs):
    pass


class StoreHarness(unittest.TestCase):
    """每个用例一个独立的临时库 + 一个独立的 engine。

    刻意**不使用** app 的全局 engine：那样会让用例之间通过连接池互相影响，
    且无法断言"事务是否泄漏"。独立 engine 仍然带 T-15 的全部 PRAGMA 契约
    （由 conftest 风格的 helper 复刻），所以测出来的行为与生产一致。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t19-store-")
        self.db = os.path.join(self.tmp, "s.db")
        self.engine = build_temp_db(self.db)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.store = SQLiteSessionStore(session_factory=self.Session)

        # 造一个真实用户（interview_sessions.user_id 有外键）
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (username, email, role, is_active) "
                "VALUES ('t19', 't19@x.y', 'user', 1)"))
        with self.engine.connect() as conn:
            self.uid = conn.execute(
                text("SELECT id FROM users WHERE username='t19'")).scalar()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------- 构造工具 ----------------

    def draft(self, session_id="s1", user_id=None, ttl=7200, questions=3):
        now = utcnow_iso()
        return SessionDraft(
            session_id=session_id,
            user_id=self.uid if user_id is None else user_id,
            role="backend",
            questions=["q%d" % i for i in range(questions)],
            question_status=["pending"] * questions,
            user_answers=[None] * questions,
            current_index=0,
            created_at=now,
            updated_at=now,
            expires_at=iso_after(ttl, now),
        )

    def make_session(self, session_id="s1", ttl=7200, questions=3, user_id=None):
        return self.store.create(
            self.draft(session_id, user_id=user_id, ttl=ttl, questions=questions))

    def turn(self, snap, seq, index=None, reply="回复", answers=None,
             expected_version=None, now=None):
        """构造一次写入意图。

        `expected_version` / `now` 可覆盖 —— 用来模拟"拿着陈旧快照的并发请求"
        与"已过期的会话"。注意 `TurnCommit` 是 frozen 的，
        所以只能在这里构造出来，不能先建再改（首版就是这么写错的）。
        """
        n = len(snap.questions)
        return TurnCommit(
            session_id=snap.session_id,
            expected_version=(snap.version if expected_version is None
                              else expected_version),
            current_index=snap.current_index + 1 if index is None else index,
            question_status=["answered"] * n if index is None else snap.question_status,
            user_answers=answers if answers is not None else snap.user_answers,
            last_seq=seq,
            last_reply=reply,
            updated_at=utcnow_iso(),
            now=now if now is not None else utcnow_iso(),
        )

    def raw(self, sql, params=None):
        """绕过 store 直接读库，用于校验真实落库内容。"""
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()


class CreateAndReadTests(StoreHarness):

    def test_create_returns_snapshot(self):
        snap = self.make_session()
        self.assertEqual(snap.session_id, "s1")
        self.assertEqual(snap.status, SessionStatus.ACTIVE)
        self.assertEqual(snap.version, 0)
        self.assertEqual(snap.last_seq, 0)
        self.assertEqual(len(snap.questions), 3)
        self.assertEqual(snap.question_status, ["pending"] * 3)
        self.assertIsNone(snap.report)
        self.assertIsNone(snap.ended_reason)

    def test_json_columns_round_trip(self):
        """中文 / 引号 / None 必须原样往返（JSON 列最容易在这里出错）。"""
        now = utcnow_iso()
        draft = SessionDraft(
            session_id="s-json", user_id=self.uid, role="后端",
            questions=[{"q": "讲讲 '索引' 与 \"事务\""}, "第二题"],
            question_status=["answered", "pending"],
            user_answers=["我的回答是 'A'", None],
            current_index=1, created_at=now, updated_at=now,
            expires_at=iso_after(60, now),
        )
        self.store.create(draft)
        got = self.store.get("s-json")
        self.assertEqual(got.questions, draft.questions)
        self.assertEqual(got.user_answers, ["我的回答是 'A'", None])
        self.assertEqual(got.question_status, ["answered", "pending"])

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_get_active_ignores_expired(self):
        """过期的行即使 status 仍是 active，也**不算活跃**（口径统一在这里）。"""
        self.make_session("s-exp", ttl=-10)  # 已过期
        self.assertIsNone(self.store.get_active(self.uid, utcnow_iso()))

    def test_get_active_returns_live_session(self):
        self.make_session("s-live", ttl=600)
        got = self.store.get_active(self.uid, utcnow_iso())
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "s-live")

    def test_create_twice_raises_active_session_exists(self):
        self.make_session("s1")
        with self.assertRaises(ActiveSessionExists) as ctx:
            self.make_session("s2")
        self.assertEqual(ctx.exception.user_id, self.uid)

    def test_active_conflict_is_not_detected_by_message_text(self):
        """判别不能依赖异常文本（不同 SQLite 版本措辞不同）。

        做法：先制造"有 active 会话"的事实，再插入一条**违反别的约束**的记录
        （user_id 不存在 -> 外键/NOT NULL），此时**不应**被误判成
        `ActiveSessionExists`，而应抛出带说明的 `StoreError`。
        """
        self.make_session("s1")
        with self.assertRaises(StoreError) as ctx:
            self.store.create(self.draft("s-bad", user_id=999999))
        self.assertNotIsInstance(ctx.exception, ActiveSessionExists)
        self.assertIn("999999", str(ctx.exception))

    def test_create_after_terminal_releases_lock(self):
        snap = self.make_session("s1")
        self.store.abandon("s1", snap.version, EndedReason.MANUAL, utcnow_iso())
        second = self.make_session("s2")   # 不应抛
        self.assertEqual(second.session_id, "s2")

    def test_many_terminal_sessions_coexist(self):
        snap = self.make_session("s1")
        self.store.abandon("s1", snap.version, EndedReason.MANUAL, utcnow_iso())
        for i in range(5):
            s = self.make_session("s-%d" % i)
            self.store.finish(s.session_id, s.version, "{}", EndedReason.COMPLETED,
                              utcnow_iso())
        self.assertEqual(len(self.raw(
            "SELECT session_id FROM interview_sessions WHERE status='finished'")), 5)


class SeqIdempotencyTests(StoreHarness):
    """任务关键词 2：`seq` 幂等 —— 这是"避免重复计费"的唯一关卡。"""

    def test_first_seq_is_not_replay(self):
        self.make_session()
        look = self.store.find_replay("s1", 1)
        self.assertFalse(look.is_replay)

    def test_same_seq_is_replay_with_reply(self):
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=1, reply="第一次的回复"))
        look = self.store.find_replay("s1", 1)
        self.assertTrue(look.is_replay)
        self.assertEqual(look.reply, "第一次的回复")

    def test_older_seq_is_also_replay(self):
        """`seq <= last_seq` 都算重发（乱序到达的旧请求也不该再调 AI）。"""
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=5, reply="第五次"))
        for seq in (5, 4, 1, 0):
            self.assertTrue(self.store.find_replay("s1", seq).is_replay,
                            "seq=%s 应当被判定为重发" % seq)

    def test_larger_seq_is_new(self):
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=3, reply="r3"))
        self.assertFalse(self.store.find_replay("s1", 4).is_replay)

    def test_replay_with_empty_reply_is_distinguishable(self):
        """**T-19 补的协议修正的核心用例。**

        上次回复为空（`last_reply=None`）时，"这是重发" 与 "这是新序号"
        必须仍可区分 —— 否则调用方会再次调用 AI，造成重复计费。
        """
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=1, reply=None))
        look = self.store.find_replay("s1", 1)
        self.assertTrue(look.is_replay, "空回复的重发被误判成新序号 -> 会重复计费")
        self.assertIsNone(look.reply)
        # 对照：新序号必须判成 False，两者不能撞在一起
        self.assertFalse(self.store.find_replay("s1", 2).is_replay)
        self.assertNotEqual(look, self.store.find_replay("s1", 2))

    def test_missing_session_returns_none(self):
        self.assertIsNone(self.store.find_replay("nope", 1))


class OptimisticLockTests(StoreHarness):
    """任务关键词 1：乐观锁 —— 冲突必须**显式暴露**，绝不静默覆盖。"""

    def test_commit_advances_version(self):
        snap = self.make_session()
        result = self.store.commit_turn(self.turn(snap, seq=1))
        self.assertTrue(result.applied)
        self.assertEqual(result.snapshot.version, snap.version + 1)
        self.assertEqual(result.snapshot.last_seq, 1)

    def test_stale_version_is_rejected(self):
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=1))     # version 0 -> 1

        # 另一个请求仍拿着 version 0 的快照（模拟并发）
        result = self.store.commit_turn(
            self.turn(snap, seq=2, reply="陈旧的写入", expected_version=snap.version))

        self.assertFalse(result.applied, "陈旧 version 竟然写入成功 —— 会静默覆盖")
        self.assertIsNotNone(result.snapshot, "冲突时必须带回最新快照供 409 使用")
        self.assertEqual(result.snapshot.last_seq, 1, "陈旧载荷覆盖了他人的提交")
        self.assertEqual(result.snapshot.last_reply, "回复")

    def test_conflict_does_not_partially_write(self):
        """冲突时**一个字段都不许变**（整列覆盖型的 UPDATE 最容易半写）。"""
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=1, reply="好的"))
        before = self.store.get("s1")

        self.store.commit_turn(self.turn(
            snap, seq=99, reply="不该出现", answers=["被污染的答案", None, None],
            expected_version=0))

        after = self.store.get("s1")
        self.assertEqual(after.user_answers, before.user_answers)
        self.assertEqual(after.last_reply, before.last_reply)
        self.assertEqual(after.last_seq, before.last_seq)
        self.assertEqual(after.version, before.version)

    def test_two_snapshot_race_only_one_wins(self):
        """两个请求拿同一 version → 恰好一个成功（ADR-004 的并发语义）。"""
        snap = self.make_session()
        first = self.store.commit_turn(self.turn(snap, seq=1, reply="A"))
        second = self.store.commit_turn(self.turn(snap, seq=2, reply="B"))
        self.assertEqual([first.applied, second.applied], [True, False])

    def test_commit_on_finished_session_is_rejected(self):
        snap = self.make_session()
        self.store.finish("s1", snap.version, "{}", EndedReason.COMPLETED, utcnow_iso())
        live = self.store.get("s1")
        result = self.store.commit_turn(self.turn(live, seq=1))
        self.assertFalse(result.applied, "已结束的会话不该还能写入")

    def test_commit_on_expired_session_is_rejected(self):
        """`expires_at > :now` 守卫：过期期间不得写入。"""
        snap = self.make_session("s-old", ttl=-10)
        commit = self.turn(snap, seq=1, now=utcnow_iso())   # 现在 > expires_at
        result = self.store.commit_turn(commit)
        self.assertFalse(result.applied, "已过期的会话不该还能写入")

    def test_finish_conflict_when_version_moved(self):
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=1))
        result = self.store.finish("s1", snap.version, '{"score":1}',
                                  EndedReason.COMPLETED, utcnow_iso())
        self.assertFalse(result.applied)


class StateMachineTests(StoreHarness):
    """任务关键词 3：状态机 —— 终态释放唯一锁。"""

    def test_finish_sets_report_and_reason(self):
        snap = self.make_session()
        result = self.store.finish("s1", snap.version, '{"score": 8}',
                                  EndedReason.COMPLETED, utcnow_iso())
        self.assertTrue(result.applied)
        self.assertEqual(result.snapshot.status, SessionStatus.FINISHED)
        self.assertEqual(result.snapshot.ended_reason, EndedReason.COMPLETED)
        self.assertEqual(result.snapshot.report, '{"score": 8}')
        self.assertEqual(result.snapshot.version, snap.version + 1)

    def test_abandon_sets_reason_without_report(self):
        snap = self.make_session()
        result = self.store.abandon("s1", snap.version, EndedReason.MANUAL,
                                    utcnow_iso())
        self.assertTrue(result.applied)
        self.assertEqual(result.snapshot.status, SessionStatus.ABANDONED)
        self.assertEqual(result.snapshot.ended_reason, EndedReason.MANUAL)
        self.assertIsNone(result.snapshot.report)

    def test_finish_then_abandon_is_rejected(self):
        """终态是**终态**：finished 不能再变 abandoned。"""
        snap = self.make_session()
        self.store.finish("s1", snap.version, "{}", EndedReason.COMPLETED, utcnow_iso())
        live = self.store.get("s1")
        result = self.store.abandon("s1", live.version, EndedReason.MANUAL, utcnow_iso())
        self.assertFalse(result.applied)

    def test_invalid_ended_reason_is_rejected(self):
        snap = self.make_session()
        with self.assertRaises(StoreError) as ctx:
            self.store.finish("s1", snap.version, "{}", "whatever", utcnow_iso())
        self.assertIn("ended_reason", str(ctx.exception))
        # 校验发生在写库之前：状态不该有任何变化
        self.assertEqual(self.store.get("s1").status, SessionStatus.ACTIVE)

    def test_abandon_allows_expired_session_to_be_released(self):
        """`abandon` **不带**过期守卫 —— 超时路径本就是在过期后调用。

        加守卫会让超时兜底永远失败，用户被锁到 2 小时 TTL 结束（ADR-022R 要消除的）。
        """
        snap = self.make_session("s-old", ttl=-10)
        result = self.store.abandon("s-old", snap.version, EndedReason.TIMEOUT,
                                    utcnow_iso())
        self.assertTrue(result.applied, "过期会话竟然无法被置为 abandoned")
        self.assertIsNone(self.store.get_active(self.uid, utcnow_iso()))


class ExpiryAndCleanupTests(StoreHarness):

    def test_abandon_expired_for_user_self_heals(self):
        """ADR-022 R-10：插入前自愈，避免"409 但 session 又返回 null"的自相矛盾。"""
        self.make_session("s-exp", ttl=-10)
        # 过期行仍占着部分唯一索引 -> 此刻直接插入会冲突
        with self.assertRaises(ActiveSessionExists):
            self.make_session("s-new")

        healed = self.store.abandon_expired_for_user(self.uid, utcnow_iso())
        self.assertEqual(healed, 1)
        self.assertIsNone(self.store.get_active(self.uid, utcnow_iso()))

        # 自愈后可以立刻重开（ADR-022R 的核心承诺）
        self.assertEqual(self.make_session("s-new").session_id, "s-new")

    def test_abandon_expired_for_user_leaves_live_sessions(self):
        self.make_session("s-live", ttl=600)
        self.assertEqual(self.store.abandon_expired_for_user(self.uid, utcnow_iso()), 0)
        self.assertIsNotNone(self.store.get_active(self.uid, utcnow_iso()))

    def test_expired_row_gets_timeout_reason(self):
        self.make_session("s-exp", ttl=-10)
        self.store.abandon_expired_for_user(self.uid, utcnow_iso())
        self.assertEqual(self.store.get("s-exp").ended_reason, EndedReason.TIMEOUT)

    def test_abandon_all_expired_across_users(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (username, email, role, is_active) VALUES "
                "('t19b', 't19b@x.y', 'user', 1), ('t19c', 't19c@x.y', 'user', 1)"))
        with self.engine.connect() as conn:
            uid2 = conn.execute(
                text("SELECT id FROM users WHERE username='t19b'")).scalar()
            uid3 = conn.execute(
                text("SELECT id FROM users WHERE username='t19c'")).scalar()

        self.make_session("s-a", ttl=-10)                    # uid1 过期
        self.make_session("s-b", user_id=uid2, ttl=-10)      # uid2 过期
        # 活跃会话必须挂在**第三个**用户上：过期的 s-a 仍占着 uid1 的部分唯一索引
        self.make_session("s-live", user_id=uid3, ttl=600)

        self.assertEqual(self.store.abandon_all_expired(utcnow_iso()), 2)
        self.assertEqual(self.store.get("s-live").status, SessionStatus.ACTIVE)

    def test_ttl_boundary_is_treated_as_expired(self):
        """`expires_at == now` 必须算过期（与 `SessionSnapshot.is_expired` 同一口径）。"""
        snap = self.make_session("s-edge", ttl=0)
        self.assertIsNone(self.store.get_active(self.uid, snap.expires_at))
        result = self.store.commit_turn(
            TurnCommit(session_id="s-edge", expected_version=0, current_index=1,
                       question_status=snap.question_status, user_answers=[],
                       last_seq=1, last_reply=None,
                       updated_at=snap.expires_at, now=snap.expires_at))
        self.assertFalse(result.applied, "边界时刻竟然允许写入")


class TransactionDisciplineTests(StoreHarness):
    """任务关键词 4：短事务 —— 绝不把事务泄漏给调用方。"""

    def _can_take_write_lock(self):
        """**独立**连接能否立刻取得写锁。

        在 T-15 的 `BEGIN IMMEDIATE` 契约下，任何未关闭的事务都持着写锁，
        这个探测会超时失败。因此它等价于"上一个方法有没有把事务留着不放"。
        """
        conn = sqlite3.connect(self.db, timeout=0.5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def test_every_read_method_releases_its_transaction(self):
        snap = self.make_session()
        for name, call in (
            ("get", lambda: self.store.get("s1")),
            ("get_active", lambda: self.store.get_active(self.uid, utcnow_iso())),
            ("find_replay", lambda: self.store.find_replay("s1", 1)),
            ("abandon_expired_for_user",
             lambda: self.store.abandon_expired_for_user(self.uid, utcnow_iso())),
        ):
            call()
            self.assertTrue(self._can_take_write_lock(),
                            "%s 之后事务没释放 —— 会把后续请求全部堵死" % name)
        self.assertIsNotNone(snap)

    def test_every_write_method_releases_its_transaction(self):
        snap = self.make_session()
        for name, call in (
            ("commit_turn", lambda: self.store.commit_turn(self.turn(
                self.store.get("s1"), seq=1))),
            ("abandon_all_expired", lambda: self.store.abandon_all_expired(utcnow_iso())),
        ):
            call()
            self.assertTrue(self._can_take_write_lock(),
                            "%s 之后事务没释放" % name)
        live = self.store.get("s1")
        for name, call in (
            ("finish", lambda: self.store.finish("s1", live.version, "{}",
                                                 EndedReason.COMPLETED, utcnow_iso())),
        ):
            call()
            self.assertTrue(self._can_take_write_lock(), "%s 之后事务没释放" % name)
        self.assertIsNotNone(snap)

    def test_failed_write_also_releases_transaction(self):
        """写失败（冲突）之后同样不能留事务 —— 否则一次 409 就把库锁住。"""
        snap = self.make_session()
        self.store.finish("s1", snap.version, "{}", EndedReason.COMPLETED, utcnow_iso())
        live = self.store.get("s1")
        self.store.abandon("s1", live.version, EndedReason.MANUAL, utcnow_iso())  # 冲突
        self.assertTrue(self._can_take_write_lock(),
                        "冲突之后事务没释放 —— 一次 409 会锁住整个库")

    def test_store_exposes_no_transaction_handle(self):
        """**结构性**约束：接口上根本没有"打开事务"的入口。

        没有可传入的 session 参数，也没有 begin()/commit() 暴露出去，
        调用方**写不出**长事务。这比"约定不要那样写"可靠。
        """
        public = [n for n in dir(self.store) if not n.startswith("_")]
        for forbidden in ("begin", "commit", "rollback", "session", "transaction"):
            self.assertNotIn(forbidden, public,
                             "存储层暴露了 %r，调用方就有可能持长事务" % forbidden)


class StrictJsonTests(StoreHarness):
    """与 `safe_json_loads` 的有意分歧：会话行损坏必须**当场炸掉**。"""

    def test_corrupt_json_raises_instead_of_silently_defaulting(self):
        snap = self.make_session()
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE interview_sessions SET question_status='{坏数据' "
                "WHERE session_id=:sid"), {"sid": snap.session_id})

        with self.assertRaises(StoreError) as ctx:
            self.store.get("s1")
        self.assertIn("question_status", str(ctx.exception))
        self.assertIn("s1", str(ctx.exception))

    def test_empty_json_column_raises(self):
        """空串同样算"会话数据不完整"。

        注意不能把列设成 NULL 来测 —— `user_answers` 是 `NOT NULL`
        （DDL 里就是如此），数据库会先一步拒绝；空串才是真正可能的脏数据形态。
        """
        snap = self.make_session()
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE interview_sessions SET user_answers='' WHERE session_id=:sid"),
                {"sid": snap.session_id})
        with self.assertRaises(StoreError) as ctx:
            self.store.get("s1")
        self.assertIn("user_answers", str(ctx.exception))


class RawStorageTests(StoreHarness):
    """直接读库，确认 store 真的把数据写进了正确的列（而不是只在自己内部自洽）。"""

    def test_commit_writes_expected_columns(self):
        snap = self.make_session()
        self.store.commit_turn(self.turn(snap, seq=7, reply="第七轮",
                                         answers=["答案1", None, None]))
        row = self.raw(
            "SELECT current_index, last_seq, last_reply, version, "
            "question_status, user_answers FROM interview_sessions "
            "WHERE session_id='s1'")[0]
        self.assertEqual(row[0], snap.current_index + 1)
        self.assertEqual(row[1], 7)
        self.assertEqual(row[2], "第七轮")
        self.assertEqual(row[3], 1)
        self.assertIn("answered", row[4])
        self.assertIn("答案1", row[5])

    def test_finish_persists_report_in_same_row(self):
        snap = self.make_session()
        self.store.finish("s1", snap.version, '{"overall_score": 7.5}',
                         EndedReason.COMPLETED, utcnow_iso())
        row = self.raw("SELECT report, status, ended_reason "
                       "FROM interview_sessions WHERE session_id='s1'")[0]
        self.assertEqual(row[0], '{"overall_score": 7.5}')
        self.assertEqual(row[1], "finished")
        self.assertEqual(row[2], "completed")


class TimeoutReportTests(StoreHarness):
    """T-27 补的两个方法：超时后仍要能出报告，且**不能**把状态改成 finished。

    ADR-007R（超时仍出报告）与 ADR-022R（超时必须是 abandoned、不是 finished）
    这两条要求是**同时**成立的，靠的就是"报告与状态是两个正交事实"这个设计。
    """

    def _timeout(self, session_id="s1", ttl=-60):
        """造一场"已超时并已被自愈为 abandoned"的会话（真实代码路径）。"""
        snap = self.make_session(session_id=session_id, ttl=ttl)
        now = utcnow_iso()
        n = self.store.abandon_expired_for_user(self.uid, now)
        self.assertEqual(n, 1, "过期行自愈应当命中 1 行")
        return self.store.get(session_id)

    # ---------------- get_last_ended ----------------

    def test_get_last_ended_is_none_without_any_session(self):
        self.assertIsNone(self.store.get_last_ended(self.uid))

    def test_active_session_is_not_last_ended(self):
        """进行中的会话不算"已结束" —— 两个方法的分工不能混。"""
        self.make_session()
        self.assertIsNone(self.store.get_last_ended(self.uid))
        self.assertIsNotNone(self.store.get_active(self.uid, utcnow_iso()))

    def test_returns_the_timed_out_session(self):
        """**T-27 的根因用例**：超时后 `get_active` 取不到，`get_last_ended` 必须能。

        修复前少了这个方法，`generate_report` 在超时后只能返回 409 ——
        ADR-007R 的"超时仍要出报告"就落不了地。
        """
        dead = self._timeout()
        self.assertIsNone(self.store.get_active(self.uid, utcnow_iso()),
                          "超时后不应再有活跃会话")
        last = self.store.get_last_ended(self.uid)
        self.assertIsNotNone(last, "超时会话必须能被取到，否则永远出不了报告")
        self.assertEqual(last.session_id, dead.session_id)
        self.assertEqual(last.status, SessionStatus.ABANDONED)
        self.assertEqual(last.ended_reason, EndedReason.TIMEOUT)

    def test_returns_the_most_recently_ended_one(self):
        """按**结束时刻**取，不是按创建时刻。"""
        self._timeout("old")
        self._timeout("new")
        self.assertEqual(self.store.get_last_ended(self.uid).session_id, "new")

    def test_finished_session_is_also_last_ended(self):
        snap = self.make_session()
        self.store.finish("s1", snap.version, '{"a":1}', EndedReason.COMPLETED,
                          utcnow_iso())
        last = self.store.get_last_ended(self.uid)
        self.assertEqual(last.status, SessionStatus.FINISHED)

    def test_does_not_leak_another_users_session(self):
        self._timeout()
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (username, email, role, is_active) "
                "VALUES ('t27other', 'o@x.y', 'user', 1)"))
        other = self.raw("SELECT id FROM users WHERE username='t27other'")[0][0]
        self.assertIsNone(self.store.get_last_ended(other),
                          "不能把别人的会话当成自己的最近一场")

    # ---------------- attach_report ----------------

    def test_attach_report_writes_report_but_keeps_abandoned(self):
        """**核心不变量**：报告落库了，状态仍然是 abandoned（不是 finished）。"""
        dead = self._timeout()
        res = self.store.attach_report("s1", dead.version, '{"overall_score": 0.0}',
                                       utcnow_iso())
        self.assertTrue(res.applied)
        row = self.raw("SELECT report, status, ended_reason, version "
                       "FROM interview_sessions WHERE session_id='s1'")[0]
        self.assertEqual(row[0], '{"overall_score": 0.0}')
        self.assertEqual(row[1], SessionStatus.ABANDONED,
                         "写报告把超时会话变成了 finished —— ADR-022R 被违反")
        self.assertEqual(row[2], EndedReason.TIMEOUT,
                         "ended_reason 不该被报告路径改写")
        self.assertEqual(row[3], dead.version + 1)

    def test_attach_report_refuses_to_overwrite_an_existing_report(self):
        """报告一旦写出就不再重写：重算要多花一次 AI 调用，还会覆盖用户看过的结论。"""
        dead = self._timeout()
        self.assertTrue(self.store.attach_report(
            "s1", dead.version, '{"n":1}', utcnow_iso()).applied)
        again = self.store.attach_report("s1", dead.version + 1, '{"n":2}',
                                         utcnow_iso())
        self.assertFalse(again.applied)
        self.assertEqual(self.raw("SELECT report FROM interview_sessions "
                                  "WHERE session_id='s1'")[0][0], '{"n":1}')

    def test_attach_report_uses_optimistic_lock(self):
        """拿着陈旧 version 的并发请求不能写进去。"""
        dead = self._timeout()
        stale = self.store.attach_report("s1", dead.version - 1, '{"n":1}',
                                        utcnow_iso())
        self.assertFalse(stale.applied)

    def test_attach_report_refuses_active_session(self):
        """`active` 会话必须走 `finish()` —— 否则会造出"进行中却有报告"的怪状态。"""
        snap = self.make_session()
        res = self.store.attach_report("s1", snap.version, '{"n":1}', utcnow_iso())
        self.assertFalse(res.applied)
        self.assertEqual(self.raw("SELECT report, status FROM interview_sessions "
                                  "WHERE session_id='s1'")[0][0], None)

    def test_attach_report_does_not_release_or_take_any_lock(self):
        """超时行本来就已是终态；补报告不该影响"用户能立刻开新面试"。"""
        dead = self._timeout()
        self.store.attach_report("s1", dead.version, '{"n":1}', utcnow_iso())
        fresh = self.make_session(session_id="s2")
        self.assertEqual(fresh.session_id, "s2")

    def test_attach_report_unknown_session_is_not_applied(self):
        res = self.store.attach_report("nope", 1, '{"n":1}', utcnow_iso())
        self.assertFalse(res.applied)


if __name__ == "__main__":
    unittest.main()

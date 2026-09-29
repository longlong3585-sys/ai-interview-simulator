"""T-25 测试：`start_interview` 唯一入口 + 过期行自愈 + 409 携带会话摘要（Bug 2）。

三件事各自对应一个真实的用户可见故障：

  1. **唯一入口**：会话只能由 `start_interview` 创建，且创建前必须过"自愈 + 冲突检查"
     这一关。没有这一条，任何人都可能绕过它插一条会话。
  2. **过期行自愈（ADR-022 R-10）**：一行可能 `expires_at` 已过、`status` 仍是 `active`，
     它**仍占着** `UNIQUE(user_id) WHERE status='active'` 索引。于是用户看到
     自相矛盾的一幕：
         POST /api/start_interview -> 409「你已有进行中的面试」
         GET  /api/interview/session -> null「你没有在面试」
     用户完全无从下手。**这是本任务要修的核心缺陷。**
  3. **409 携带会话摘要**：前端收到 409 要能立刻给出"继续 / 放弃重开"，
     不该再等一次 `GET session` 往返（ADR-022 R-10 明确要求）。
"""

import os
import re
import sqlite3
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from tests.support import bearer, create_test_user, delete_users, mint_token  # noqa: E402

USER = "t25_alice"
OTHER = "t25_bob"
DB_PATH = None


def _db_path():
    import database
    return database.DATABASE_URL.replace("sqlite:///", "")


def _expire_session(session_id, seconds_ago=60):
    """把某条会话的 expires_at 改到过去 —— 制造"过期但仍 active"的行。

    刻意直接改库：这是**数据状态**，不是任何接口能产生的
    （`abandon_expired_for_user` 正是为清理这种历史遗留状态而存在）。
    """
    import datetime
    past = (datetime.datetime.utcnow()
            - datetime.timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("UPDATE interview_sessions SET expires_at = ? WHERE session_id = ?",
                     (past, session_id))
        conn.commit()
        row = conn.execute("SELECT status, expires_at FROM interview_sessions "
                           "WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return row


def _session_row(session_id):
    conn = sqlite3.connect(_db_path())
    try:
        return conn.execute(
            "SELECT status, ended_reason, current_index, version FROM interview_sessions "
            "WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()


class StartInterviewHarness(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        delete_users([USER, OTHER])

    def setUp(self):
        delete_users([USER, OTHER])
        create_test_user(USER)
        self.h = bearer(mint_token(USER))

    def start(self, headers=None, questions='["Q1","Q2"]'):
        return self.client.post("/api/start_interview", headers=headers or self.h,
                                data={"role": "后端开发",
                                      "questions_json": questions})

    def get_session(self, headers=None):
        return self.client.get("/api/interview/session", headers=headers or self.h)


class SelfHealTests(StartInterviewHarness):
    """核心缺陷：过期但仍 active 的行占着唯一锁，造成 409 与 null 自相矛盾。"""

    def test_expired_row_would_block_before_healing(self):
        """先证明这个矛盾**真实存在**：过期行的确让 GET 返回 null、却占着索引。

        （这条不调用接口，直接看数据 —— 否则测不出"如果没有自愈会怎样"。）
        """
        first = self.start().json()["session_id"]
        _expire_session(first)

        # GET 侧：过期行不算活跃 -> null（用户看到"我没有在面试"）
        self.assertIsNone(self.get_session().json()["session"])
        # 数据侧：它仍然是 active -> 仍占着部分唯一索引
        self.assertEqual(_session_row(first)[0], "active")

    def test_start_succeeds_after_expiry(self):
        """**本任务的核心断言**：过期行的存在不得阻止用户开新面试。"""
        first = self.start().json()["session_id"]
        _expire_session(first)

        r = self.start(questions='["新题"]')
        self.assertEqual(r.status_code, 200,
                         "过期会话挡住了新面试 —— 用户被锁在门外：%s" % r.text)
        self.assertNotEqual(r.json()["session_id"], first)

    def test_healed_row_is_abandoned_with_timeout(self):
        """自愈的语义：置 `abandoned` + `ended_reason=timeout`（与超时兜底同口径）。"""
        first = self.start().json()["session_id"]
        _expire_session(first)
        self.start(questions='["新题"]')

        status, reason, _, _ = _session_row(first)
        self.assertEqual(status, "abandoned")
        self.assertEqual(reason, "timeout")

    def test_live_session_is_not_healed(self):
        """**对照组**：没过期的活跃会话**不能**被自愈掉 —— 否则就成了静默覆盖。"""
        first = self.start().json()["session_id"]
        r = self.start(questions='["新题"]')
        self.assertEqual(r.status_code, 409, "未过期的会话被误伤了")
        status, reason, _, _ = _session_row(first)
        self.assertEqual(status, "active")
        self.assertIsNone(reason)

    def test_healing_only_touches_this_users_rows(self):
        create_test_user(OTHER)
        other_h = bearer(mint_token(OTHER))
        other_sid = self.start(other_h).json()["session_id"]
        _expire_session(other_sid)

        mine = self.start().json()["session_id"]
        _expire_session(mine)
        self.start(questions='["新题"]')          # 触发自愈

        # 别人的过期会话不该被我的请求改掉
        self.assertEqual(_session_row(other_sid)[0], "active")

    def test_two_expired_rows_for_same_user_are_both_healed(self):
        """理论上同一用户最多一条 active，但历史脏数据可能不止一条 —— 全部自愈。"""
        first = self.start().json()["session_id"]
        _expire_session(first)
        second = self.start(questions='["B"]').json()["session_id"]
        _expire_session(second)

        r = self.start(questions='["C"]')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_session_row(first)[0], "abandoned")
        self.assertEqual(_session_row(second)[0], "abandoned")
        self.assertEqual(r.json()["session_id"] not in (first, second), True)


class ConflictSummaryTests(StartInterviewHarness):
    """409 必须自带摘要，前端一次往返就能给出"继续 / 放弃重开"。"""

    def test_conflict_returns_409_with_session_summary(self):
        first = self.start().json()["session_id"]
        r = self.start(questions='["新题"]')
        self.assertEqual(r.status_code, 409)
        body = r.json()

        self.assertEqual(body["code"], "active_session_exists")
        self.assertIsInstance(body["detail"], str, "detail 必须是字符串（前端直接渲染）")
        self.assertIn("session", body, "409 没有携带会话摘要 —— 前端还得再问一次")

        s = body["session"]
        self.assertEqual(s["session_id"], first)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["current_index"], 0)
        self.assertIn("last_seq", s)
        self.assertIn("remaining_seconds", s)

    def test_summary_reflects_real_progress(self):
        """摘要是**实时**的：推进过进度后，409 里应当看到最新 current_index。"""
        sid = self.start(questions='["Q1","Q2","Q3"]').json()["session_id"]
        from services.stores.base import TurnCommit, utcnow_iso
        from services.stores.factory import get_session_store
        store = get_session_store()
        snap = store.get(sid)
        store.commit_turn(TurnCommit(
            session_id=sid, expected_version=snap.version, current_index=2,
            question_status=["answered", "skipped", "pending"],
            user_answers=["答", "[跳过]", None], last_seq=snap.last_seq,
            last_reply=None, updated_at=utcnow_iso(), now=utcnow_iso()))

        body = self.start(questions='["新题"]').json()
        self.assertEqual(body["session"]["current_index"], 2)
        self.assertEqual(body["session"]["question_status"][1], "skipped")

    def test_actions_name_the_recovery_endpoints(self):
        """`actions` 要明确指向 T-24 的两个接口，前端照着做就行。"""
        self.start()
        body = self.start(questions='["新题"]').json()
        self.assertIn("get_session", body["actions"])
        self.assertIn("abandon", body["actions"])

    def test_summary_does_not_leak_user_id_or_report(self):
        self.start()
        s = self.start(questions='["新题"]').json()["session"]
        self.assertNotIn("user_id", s)
        self.assertNotIn("report", s)


class SingleEntryPointTests(StartInterviewHarness):
    """唯一入口：会话只能由 `start_interview` 创建，且必须过"自愈 + 冲突检查"。"""

    def test_only_start_interview_creates_sessions(self):
        """静态约束：会话的创建调用在整个路由文件里**只出现一次**。

        新增第二条创建路径（例如某个接口偷偷插会话）会绕过自愈与唯一锁检查，
        这条断言就是拦住它的地方。

        注意匹配要精确到"用 draft 建会话"：`client.chat.completions.create(`
        是 AI SDK 的调用，与本次要守的东西无关（首版用宽泛的 `.create(` 误伤了它）。
        """
        with open(os.path.join(BACKEND_DIR, "routers", "interview.py"),
                  "r", encoding="utf-8") as fh:
            src = fh.read()
        calls = re.findall(r"\.create\(draft\)", src)
        self.assertEqual(len(calls), 1,
                         "路由层出现了 %d 处会话创建调用，应当只有 start_interview 一处"
                         % len(calls))

    def test_self_heal_is_called_before_create(self):
        """顺序断言：自愈必须**在** create **之前**。

        顺序反了的话，冲突仍会发生（过期行还在占锁），
        而且异常路径会把 409 抛给用户 —— 正是本任务要修的现象。
        """
        with open(os.path.join(BACKEND_DIR, "routers", "interview.py"),
                  "r", encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def start_interview")
        end = src.index("@router.post(\"/skip_question\")")
        body = src[start:end]
        heal_at = body.index("abandon_expired_for_user")
        create_at = body.index(".create(")
        self.assertLess(heal_at, create_at,
                        "自愈调用在 create 之后 —— 过期行仍会挡住插入")

    def test_response_carries_session_id(self):
        body = self.start().json()
        self.assertIn("session_id", body)
        self.assertTrue(body["session_id"])

    def test_two_different_users_can_both_start(self):
        create_test_user(OTHER)
        a = self.start()
        b = self.start(bearer(mint_token(OTHER)))
        self.assertEqual(a.status_code, 200)
        self.assertEqual(b.status_code, 200)
        self.assertNotEqual(a.json()["session_id"], b.json()["session_id"])


if __name__ == "__main__":
    unittest.main()

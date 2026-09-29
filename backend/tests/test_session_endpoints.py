"""T-24 测试：`GET /api/interview/session` + `POST /api/interview/abandon`（Bug 2）。

这两个接口是"刷新页面进度不丢"的后端核心：
  * 会话**落库**只解决了"数据还在"，但前端刷新后手里没有任何标识，
    必须有一个"问服务端我现在有没有在面试"的入口 —— 即 GET session；
  * 用户回不来时（崩溃/换设备）必须能主动结束旧会话，否则被唯一锁关在
    门外 2 小时 —— 即 POST abandon（ADR-022R：**用户永远能开新面试**）。
"""

import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from tests.support import bearer, create_test_user, delete_users, mint_token  # noqa: E402

USER_A = "t24_alice"
USER_B = "t24_bob"
ADMIN = "admin"


class SessionEndpointHarness(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        delete_users([USER_A, USER_B])

    def setUp(self):
        delete_users([USER_A, USER_B])          # 每个用例从干净状态开始
        create_test_user(USER_A)
        create_test_user(USER_B)
        self.a = bearer(mint_token(USER_A))
        self.b = bearer(mint_token(USER_B))

    # ---------------- 工具 ----------------

    def get_session(self, headers):
        return self.client.get("/api/interview/session", headers=headers)

    def abandon(self, headers):
        return self.client.post("/api/interview/abandon", headers=headers)

    def start(self, headers, questions='["Q1","Q2","Q3"]', role="后端开发"):
        return self.client.post("/api/start_interview", headers=headers,
                                data={"role": role, "questions_json": questions})


class GetSessionTests(SessionEndpointHarness):

    def test_no_session_returns_200_null(self):
        """**无会话是正常业务状态，不是错误** —— 返回 200 + null。

        若这里返回 404/409，前端就没法区分"我还没开始面试"与"出错了"，
        只能一律当异常处理，反而会误报。
        """
        r = self.get_session(self.a)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["session"])

    def test_returns_active_session(self):
        started = self.start(self.a).json()
        body = self.get_session(self.a).json()["session"]
        self.assertIsNotNone(body)
        self.assertEqual(body["session_id"], started["session_id"])
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["current_index"], 0)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["questions"], ["Q1", "Q2", "Q3"])

    def test_payload_does_not_leak_user_id_or_report(self):
        """不下发 `user_id`（会造成"可指定他人"的误导）与 `report`（大且属评分结果）。"""
        self.start(self.a)
        body = self.get_session(self.a).json()["session"]
        self.assertNotIn("user_id", body)
        self.assertNotIn("report", body)

    def test_expiry_is_exposed(self):
        self.start(self.a)
        body = self.get_session(self.a).json()["session"]
        self.assertIsNotNone(body["remaining_seconds"])
        self.assertGreater(body["remaining_seconds"], 0)
        self.assertLessEqual(body["remaining_seconds"], 2 * 60 * 60)

    def test_progress_is_visible_after_chat(self):
        """**这就是"刷新不丢进度"的后端证据**：换一次 HTTP 请求也看得到进度。

        （真正的跨进程验证由 `scripts/verify_t24_manual.py` 做：
          它会更进一步直接读库比对。这里只断言接口口径。）
        """
        self.start(self.a)
        # 直接改库推进进度，避免依赖 AI 调用
        from services.stores.factory import get_session_store
        from tests.support import create_test_user as _c  # noqa: F401
        from database import SessionLocal, User
        db = SessionLocal()
        try:
            uid = db.query(User).filter(User.username == USER_A).first().id
        finally:
            db.close()
        snap = get_session_store().get_active(uid, __import__("services.stores.base",
                                                              fromlist=["utcnow_iso"]).utcnow_iso())
        from services.stores.base import TurnCommit, utcnow_iso
        get_session_store().commit_turn(TurnCommit(
            session_id=snap.session_id, expected_version=snap.version,
            current_index=1, question_status=["answered", "pending", "pending"],
            user_answers=["我的回答", None, None], last_seq=snap.last_seq,
            last_reply=None, updated_at=utcnow_iso(), now=utcnow_iso()))

        body = self.get_session(self.a).json()["session"]
        self.assertEqual(body["current_index"], 1)
        self.assertEqual(body["question_status"][0], "answered")

    def test_sessions_are_isolated_per_user(self):
        """A 的会话不能被 B 看到（会话键按令牌解析，不接受任何客户端指定）。"""
        self.start(self.a)
        self.assertIsNone(self.get_session(self.b).json()["session"])
        self.assertIsNotNone(self.get_session(self.a).json()["session"])

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/api/interview/session").status_code, 401)

    def test_admin_is_rejected(self):
        """管理员不参与面试 -> 403（`require_user` 的既有规则）。"""
        r = self.get_session(bearer(mint_token(ADMIN, role="admin")))
        self.assertEqual(r.status_code, 403)


class AbandonTests(SessionEndpointHarness):

    def test_abandon_marks_abandoned_manual(self):
        self.start(self.a)
        r = self.abandon(self.a)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "abandoned")
        self.assertEqual(body["ended_reason"], "manual")

    def test_abandon_releases_the_unique_lock(self):
        """**ADR-022R 的核心承诺**：放弃后用户能立刻开新面试。

        修复前没有任何放弃入口 —— 用户一旦回不来（崩溃/换设备），
        就必须等 2 小时 TTL 才被允许重开。
        """
        first = self.start(self.a).json()
        self.abandon(self.a)
        second = self.start(self.a).json()
        self.assertNotEqual(second["session_id"], first["session_id"],
                            "重开应当是一个**新**会话")

    def test_abandoned_session_is_no_longer_active(self):
        self.start(self.a)
        self.abandon(self.a)
        self.assertIsNone(self.get_session(self.a).json()["session"])

    def test_row_is_kept_not_deleted(self):
        """置终态而不是删除 —— 会话内容要留给后续的历史/报告。"""
        started = self.start(self.a).json()
        self.abandon(self.a)
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            row = db.execute(text("SELECT status, ended_reason FROM interview_sessions "
                                  "WHERE session_id = :sid"),
                             {"sid": started["session_id"]}).fetchone()
        finally:
            db.close()
        self.assertIsNotNone(row, "会话行被删除了")
        self.assertEqual(tuple(row), ("abandoned", "manual"))

    def test_abandon_without_session_returns_409_with_guidance(self):
        r = self.abandon(self.a)
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["code"], "no_active_session")
        self.assertIsInstance(body["detail"], str)   # 前端直接渲染 detail
        self.assertIn("hint", body)

    def test_abandon_does_not_touch_other_users(self):
        a_sid = self.start(self.a).json()["session_id"]
        self.start(self.b)
        self.abandon(self.b)
        still = self.get_session(self.a).json()["session"]
        self.assertIsNotNone(still)
        self.assertEqual(still["session_id"], a_sid)

    def test_second_abandon_returns_409(self):
        self.start(self.a)
        self.assertEqual(self.abandon(self.a).status_code, 200)
        self.assertEqual(self.abandon(self.a).status_code, 409)

    def test_requires_auth(self):
        self.assertEqual(
            self.client.post("/api/interview/abandon").status_code, 401)


class RestartFlowTests(SessionEndpointHarness):
    """把 Bug 2 的用户动线走一遍：刷新 -> 发现会话 -> 放弃 -> 重开。"""

    def test_full_recovery_flow(self):
        started = self.start(self.a).json()

        # 1) "刷新页面"：前端发 GET 就能拿回同一场会话
        recovered = self.get_session(self.a).json()["session"]
        self.assertEqual(recovered["session_id"], started["session_id"])
        self.assertEqual(recovered["total"], 3)

        # 2) 用户选择"放弃并重新开始"
        self.assertEqual(self.abandon(self.a).status_code, 200)
        self.assertIsNone(self.get_session(self.a).json()["session"])

        # 3) 重开成功
        again = self.start(self.a).json()
        self.assertTrue(again["success"])
        self.assertNotEqual(again["session_id"], started["session_id"])

    def test_recovery_is_not_affected_by_other_users(self):
        mine = self.start(self.a).json()["session_id"]
        self.start(self.b)
        self.abandon(self.b)
        self.start(self.b)              # B 反复折腾
        self.assertEqual(
            self.get_session(self.a).json()["session"]["session_id"], mine)


if __name__ == "__main__":
    unittest.main()

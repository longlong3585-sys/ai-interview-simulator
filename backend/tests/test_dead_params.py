"""T-11 测试：死参数清理（FR-4.9）。

背景：
  - `ChatRequest.action`      前端传 'start'，**后端从未读取**
  - `ChatRequest.user_id`     已于 T-04 随鉴权修复移除
  - `ReportRequest.user_id`   前端传 `_userId`，**后端从未读取**
    （generate_report 只用 req.messages 与 current_user.id）

死参数的危害不只是"冗余"：它们会让调用方误以为可以指定用户/动作，
属于误导性接口设计（尤其 user_id 会让人以为能操作他人数据）。

清理后仍保持**向后兼容**：Pydantic v2 默认 extra='ignore'，
旧客户端继续发送这些字段不会 422。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from models.schemas import ChatRequest, ReportRequest
from tests.support import bearer, create_test_user, delete_users, mint_token

USERNAME = "t11_user"
ADMIN = "t11_admin"


class DeadParamSchemaTests(unittest.TestCase):
    """模型层：字段必须真的不存在。"""

    def test_chat_request_has_no_dead_fields(self):
        fields = set(ChatRequest.model_fields)
        self.assertNotIn("action", fields, "ChatRequest.action 应已移除")
        self.assertNotIn("user_id", fields, "ChatRequest.user_id 应已移除")
        # T-23：role / resume_context / resume_questions 也成了死参数 ——
        # 它们原先只服务于"/api/chat 无会话时走通用 AI 对话"那条分支，
        # 该分支已被 409 + 指引取代（否则登录用户能白嫖 AI 额度）。
        self.assertNotIn("role", fields, "ChatRequest.role 应随通用分支一起移除")
        self.assertNotIn("resume_context", fields)
        self.assertNotIn("resume_questions", fields)
        self.assertEqual(fields, {"message"})

    def test_report_request_has_no_dead_fields(self):
        fields = set(ReportRequest.model_fields)
        self.assertNotIn("user_id", fields, "ReportRequest.user_id 应已移除")
        self.assertEqual(fields, {"messages"})

    def test_extra_fields_are_ignored_not_rejected(self):
        """旧客户端仍发送死参数时不得 422（向后兼容）。"""
        req = ChatRequest(message="hi", user_id=999, action="start", whatever=1)
        self.assertEqual(req.message, "hi")
        self.assertFalse(hasattr(req, "user_id"))
        self.assertFalse(hasattr(req, "action"))


class DeadParamEndpointTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        clear_sessions()
        self.user_id = create_test_user(USERNAME, role="user")
        create_test_user(ADMIN, role="admin")
        self.token = mint_token(USERNAME, role="user")

    def tearDown(self):
        clear_sessions()
        delete_users([USERNAME, ADMIN])

    def _seed_session(self):
        seed_session(self.user_id, {
            "user_id": self.user_id,
            "role": "后端开发",
            "questions": ["Q1", "Q2"],
            "current_index": 0,
            "question_status": ["pending", "pending"],
            "user_answers": ["", ""],
            "finished": False,
        })

    def test_chat_works_without_dead_params(self):
        self._seed_session()
        with patch("routers.interview.client.chat.completions.create") as fake:
            fake.return_value.choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]
            r = self.client.post(
                "/api/chat", json={"message": "回答"}, headers=bearer(self.token)
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["current_index"], 1)

    def test_chat_still_tolerates_legacy_dead_params(self):
        """模拟旧前端：仍然发 user_id 和 action —— 必须照常工作，且身份仍来自令牌。"""
        self._seed_session()
        with patch("routers.interview.client.chat.completions.create") as fake:
            fake.return_value.choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]
            r = self.client.post(
                "/api/chat",
                json={"message": "回答", "user_id": 123456, "action": "start"},
                headers=bearer(self.token),
            )
        self.assertEqual(r.status_code, 200, r.text)
        # 身份来自令牌：推进的是自己的会话
        self.assertEqual(get_session(self.user_id).current_index, 1)
        # 伪造的 user_id 未创建任何会话
        self.assertIsNone(get_session(123456))

    def test_generate_report_works_without_user_id(self):
        r = self.client.post(
            "/api/generate_report",
            json={"messages": [{"role": "user", "content": "答"}, {"role": "assistant", "content": "问"}]},
            headers=bearer(self.token),
        )
        # AI 可能不可用（返回降级结果），但契约层不应因缺少 user_id 而 4xx/5xx
        self.assertIn(r.status_code, (200, 503), r.text)

    def test_generate_report_tolerates_legacy_user_id(self):
        r = self.client.post(
            "/api/generate_report",
            json={
                "messages": [{"role": "user", "content": "答"}],
                "user_id": 123456,
            },
            headers=bearer(self.token),
        )
        self.assertIn(r.status_code, (200, 503), r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --- T-23：会话改为经持久化存储读写（原先直接操作 `interview_sessions` 字典）---

def seed_session(user_id, d):
    """按旧字典的形状造一条会话，返回快照。"""
    import uuid
    from services.stores.base import SessionDraft, iso_after, utcnow_iso
    from services.stores.factory import get_session_store

    now = utcnow_iso()
    questions = d.get("questions") or ["T23 占位题"]
    draft = SessionDraft(
        session_id=d.get("session_id") or uuid.uuid4().hex,
        user_id=user_id,
        role=d.get("role", "后端开发"),
        questions=list(questions),
        question_status=list(d.get("question_status",
                                   ["pending"] * len(questions))),
        user_answers=list(d.get("user_answers", [None] * len(questions))),
        current_index=d.get("current_index", 0),
        created_at=now, updated_at=now, expires_at=iso_after(7200, now),
    )
    return get_session_store().create(draft)


def get_session(user_id):
    """取该用户当前活跃会话的快照；没有返回 None。"""
    from services.stores.base import utcnow_iso
    from services.stores.factory import get_session_store
    return get_session_store().get_active(user_id, utcnow_iso())


def clear_sessions():
    """清掉测试用户的所有会话（等价于旧的 `interview_sessions.clear()`）。

    直接删表内容而不是逐个 abandon —— 测试要的是"干净起点"。
    """
    from sqlalchemy import text
    import database
    with database.engine.begin() as conn:
        conn.execute(text("DELETE FROM interview_sessions"))

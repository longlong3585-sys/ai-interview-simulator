"""T-04 测试：/api/chat 的鉴权与身份来源（FR-2.4 / Bug 1）。

覆盖的核心风险：
  1. 无令牌 / 令牌无效 / 令牌过期 → 必须 401（修复前该接口完全裸奔）
  2. **请求体里伪造他人 user_id → 必须完全无效**（修复前可串号读写他人会话）
  3. 身份只来自 JWT（sub = username → 查库得到 current_user.id）
  4. admin 被 require_user 拒绝（403，既有设计，纳入回归）

AI 调用一律被 mock，测试**不发真实网络请求**，保证确定性与速度。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from tests.support import bearer, create_test_user, delete_users, mint_token

QUESTIONS = ["Q1 项目经历？", "Q2 技术栈？", "Q3 难点？"]

USER_A = "t04_alice"
USER_B = "t04_bob"
USER_ADMIN = "t04_admin"


class _FakeMessage:
    content = "（mock）回答得不错，我们继续。"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


def _fake_ai_create(*args, **kwargs):
    """替身 AI：不发网络请求，立即返回固定反馈。"""
    return _FakeResponse()


def _seed_session(user_id, current_index=0):
    seed_session(user_id, {
        "user_id": user_id,
        "role": "后端开发",
        "questions": list(QUESTIONS),
        "current_index": current_index,
        "question_status": ["pending"] * len(QUESTIONS),
        "user_answers": [""] * len(QUESTIONS),
        "finished": False,
    })


class ChatAuthTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()  # 触发 startup（与生产一致）

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        clear_sessions()
        self.id_a = create_test_user(USER_A, role="user")
        self.id_b = create_test_user(USER_B, role="user")
        create_test_user(USER_ADMIN, role="admin")
        self.token_a = mint_token(USER_A, role="user")
        self.token_admin = mint_token(USER_ADMIN, role="admin")

    def tearDown(self):
        clear_sessions()
        delete_users([USER_A, USER_B, USER_ADMIN])

    def _post_chat(self, body, token=None):
        return self.client.post("/api/chat", json=body, headers=bearer(token))

    # ---------- 1. 鉴权 ----------

    def test_chat_without_token_returns_401(self):
        """无令牌必须 401（修复前为 200，属严重漏洞）。"""
        r = self._post_chat({"message": "你好", "role": "后端开发"})
        self.assertEqual(r.status_code, 401, "无令牌竟然被放行：%s" % r.text)

    def test_chat_with_malformed_token_returns_401(self):
        r = self._post_chat({"message": "你好"}, token="not-a-real-jwt")
        self.assertEqual(r.status_code, 401)

    def test_chat_with_expired_token_returns_401(self):
        expired = mint_token(USER_A, role="user", minutes=-5)
        r = self._post_chat({"message": "你好"}, token=expired)
        self.assertEqual(r.status_code, 401)

    def test_chat_with_token_of_deleted_user_returns_401(self):
        ghost = mint_token("t04_ghost_does_not_exist", role="user")
        r = self._post_chat({"message": "你好"}, token=ghost)
        self.assertEqual(r.status_code, 401)

    def test_admin_is_rejected_by_require_user(self):
        """admin 不能进行面试（既有设计，纳入回归）。"""
        r = self._post_chat({"message": "你好"}, token=self.token_admin)
        self.assertEqual(r.status_code, 403)

    # ---------- 2. 伪造 user_id（核心） ----------

    def test_forged_user_id_cannot_advance_another_users_session(self):
        """用 A 的令牌 + 伪造 B 的 user_id：只能动 A 的会话，B 的必须原封不动。"""
        _seed_session(self.id_a)
        _seed_session(self.id_b)

        with patch("routers.interview.client.chat.completions.create", side_effect=_fake_ai_create):
            r = self._post_chat(
                {"message": "我是 A 的回答", "role": "后端开发", "user_id": self.id_b},
                token=self.token_a,
            )

        self.assertEqual(r.status_code, 200, r.text)

        sess_a = get_session(self.id_a)
        sess_b = get_session(self.id_b)

        # A 的会话被推进
        self.assertEqual(sess_a.current_index, 1, "A 的会话未被推进")
        self.assertEqual(sess_a.question_status[0], "answered")
        self.assertEqual(sess_a.user_answers[0], "我是 A 的回答")

        # B 的会话必须完全没被动过 —— 这是修复前最严重的串号问题
        self.assertEqual(sess_b.current_index, 0, "B 的会话被伪造请求推进了！")
        self.assertEqual(sess_b.question_status[0], "pending", "B 的题目状态被改写！")
        self.assertEqual(sess_b.user_answers[0], "", "B 的作答被覆盖！")

    def test_forged_user_id_does_not_change_response_identity(self):
        """伪造 user_id 时，响应里的 current_index 必须取自令牌用户自己的会话。

        注：A 从 index=1 作答后变成 2（题目共 3 道，未结束），响应才带 current_index；
        若答到 >= 题目数，既有实现会返回 finished 且**不含** current_index
        （该契约怪癖由 T-23 重构会话接口时统一处理）。
        """
        _seed_session(self.id_a, current_index=1)  # A 已答 1 题
        _seed_session(self.id_b, current_index=0)  # B 在第 1 题

        with patch("routers.interview.client.chat.completions.create", side_effect=_fake_ai_create):
            r = self._post_chat(
                {"message": "回答", "user_id": self.id_b},
                token=self.token_a,
            )

        data = r.json()
        # A 的进度是 2；若代码错误地用了 B，会得到 1
        self.assertEqual(
            data.get("current_index"), 2,
            "返回的进度不是令牌用户(A)的进度：%s" % data,
        )

    def test_body_user_id_is_simply_ignored_when_absent(self):
        """请求体不带 user_id 时行为不变（身份仍来自令牌）。"""
        _seed_session(self.id_a)
        with patch("routers.interview.client.chat.completions.create", side_effect=_fake_ai_create):
            r = self._post_chat({"message": "回答"}, token=self.token_a)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(get_session(self.id_a).current_index, 1)

    def test_unknown_extra_body_fields_are_tolerated(self):
        """Pydantic 默认忽略多余字段：旧前端仍发送 user_id 不应导致 422。"""
        _seed_session(self.id_a)
        with patch("routers.interview.client.chat.completions.create", side_effect=_fake_ai_create):
            r = self._post_chat(
                {"message": "回答", "user_id": self.id_b, "some_future_field": 123},
                token=self.token_a,
            )
        self.assertEqual(r.status_code, 200, "多余字段不应导致 422：%s" % r.text)


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

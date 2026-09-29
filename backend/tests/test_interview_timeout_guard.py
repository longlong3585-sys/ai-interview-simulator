"""T-28 测试：后端超时兜底 —— 服务端自己掌握超时时刻（FR-4.12 / Bug 3B）。

## 要修的到底是什么

FR-4.12 的两条要求是**分开**的：

  ① 前端 15 分钟归零后必须锁死 UI（T-42/T-43 负责）；
  ② **服务端必须同步兜底** —— 因为"锁定 UI"拦不住手工请求。

修复前只有 ①：「15 分钟」只活在前端的一个 `setInterval` 里。任何人只要
在归零后再发一次 `POST /api/chat`（或干脆不刷新页面、用脚本刷接口），
服务端照样把这一轮记进会话 —— 会话仍是 `active`，业务规则形同虚设。

本文件逐条钉住 ② 的具体含义：

  A. **到点判定由服务端做**：死线 = 会话行 `created_at`（服务端写入）
     + 时长上限。客户端**无法**通过任何请求字段声称或否认超时。
  B. **到点后写路径一律 409**：`/api/chat`、`/api/skip_question` 都不能再推进会话。
  C. **会话置 `abandoned` 且 `ended_reason='timeout'`**（不是 `finished` ——
     ADR-022R 裁决"超时不是正常完成"）。
  D. **唯一锁随释放**：超时后能**立刻**开新面试（ADR-022R：修完本 Bug
     不得变成"把用户锁死在门外"）。
  E. **不误伤**：没到点的会话一个字节都不许动（业务死线与 2 小时 TTL 是
     两个语义，混在一起会制造"刷新一次面试就作废"的事故）。
  F. **报告口径不回归**（T-27）：兜底必须在判定 `ended_reason` **之前**跑，
     否则一场超时的面试会被出报告这条路径判成 `completed`。
"""

import contextlib
import datetime
import json
import os
import sys
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import main  # noqa: E402
from database import SessionLocal  # noqa: E402
import routers.interview as interview_router  # noqa: E402
from tests.support import bearer, create_test_user, delete_users, mint_token  # noqa: E402

USER = "t28_alice"
OTHER = "t28_bob"

#: 假 AI 的报告：`answered_count` 故意写 7（会话里根本没那么多），
#: 用来确认服务端仍会用会话事实覆盖模型自报值（T-27 的防线不许被本次改动削弱）。
FAKE_REPORT = {
    "expression_score": 0, "technical_score": 0, "logic_score": 0,
    "overall_score": 0.0, "answered_count": 7, "total_questions": 99,
    "suggestion": "假 AI 的固定建议。",
    "details": "假 AI 的固定总结（故意不含任何超时说明）。",
}

#: 服务端补的标注（FR-4.5：零作答超时不得当成"全错"）。
LABEL = "未及作答，无法评分"

DURATION = interview_router.INTERVIEW_DURATION_SECONDS
QUESTIONS = '["T28-Q1","T28-Q2","T28-Q3"]'


class _FakeResponse(object):
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class TimeoutGuardHarness(unittest.TestCase):

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
        create_test_user(OTHER)
        self.h = bearer(mint_token(USER))
        self.h2 = bearer(mint_token(OTHER))

    # ---------------- 工具 ----------------

    @contextlib.contextmanager
    def _fake_ai(self, sink=None):
        """本机假 AI：**绝不联网**（离线时用例仍必须通过）。

        不 mock 的话每个用例都会真的去调 DeepSeek —— 既花钱，又会让
        "离线"伪装成"代码有问题"。
        """
        def create(**kwargs):
            msgs = kwargs.get("messages") or []
            system = next((m.get("content", "") for m in msgs
                           if m.get("role") == "system"), "")
            user = next((m.get("content", "") for m in msgs
                         if m.get("role") == "user"), "")
            if sink is not None:
                sink.append({"system": system, "user": user})
            if "overall_score" in user or "面试评估专家" in system:
                return _FakeResponse(json.dumps(FAKE_REPORT, ensure_ascii=False))
            return _FakeResponse("【假面试官】收到，请继续。")

        with patch("routers.interview.client.chat.completions.create",
                   side_effect=create):
            yield

    def start(self, headers=None, questions=QUESTIONS):
        r = self.client.post("/api/start_interview", headers=headers or self.h,
                             data={"role": "后端开发", "questions_json": questions})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["session_id"]

    def chat(self, message="我的回答", headers=None):
        with self._fake_ai():
            return self.client.post("/api/chat", headers=headers or self.h,
                                    json={"message": message})

    def skip(self, headers=None):
        return self.client.post("/api/skip_question", headers=headers or self.h)

    def report(self, sink=None, headers=None):
        with self._fake_ai(sink=sink):
            return self.client.post("/api/generate_report", headers=headers or self.h)

    def get_session(self, headers=None):
        return self.client.get("/api/interview/session", headers=headers or self.h)

    def abandon(self, headers=None):
        return self.client.post("/api/interview/abandon", headers=headers or self.h)

    # ---- 直接改库：制造"服务端时钟已经走过死线"的**真实状态** ----
    #
    # 刻意改 `created_at`（服务端写入的那一列）而不是任何请求参数 ——
    # 这正是"15 分钟过去了"在库里的样子，且客户端无法伪造它。

    def age(self, session_id, seconds):
        when = (datetime.datetime.utcnow()
                - datetime.timedelta(seconds=seconds)).isoformat()
        db = SessionLocal()
        try:
            db.execute(text("UPDATE interview_sessions SET created_at=:c "
                            "WHERE session_id=:s"), {"c": when, "s": session_id})
            db.commit()
        finally:
            db.close()

    def past_deadline(self, session_id):
        """把会话推到"业务死线已过，但 2 小时 TTL 还没到"。"""
        self.age(session_id, DURATION + 60)

    def row(self, session_id):
        """返回 `(status, ended_reason, report, created_at, expires_at)`。"""
        db = SessionLocal()
        try:
            return db.execute(
                text("SELECT status, ended_reason, report, created_at, expires_at "
                     "FROM interview_sessions WHERE session_id=:s"),
                {"s": session_id}).fetchone()
        finally:
            db.close()


class DeadlineTests(TimeoutGuardHarness):
    """A/B/C：到点判定归服务端；写路径一律 409；状态与原因正确。"""

    def test_fresh_session_is_not_timed_out(self):
        """**不误伤**：刚开的会话必须照常能答题。

        倒计时边界写错（例如拿 `expires_at` 当死线、或符号写反）时，
        最先红的就是这一条。
        """
        sid = self.start()
        r = self.chat()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.row(sid)[0], "active")

    def test_business_deadline_is_independent_of_ttl(self):
        """业务死线（15 分钟）与 2 小时 TTL **是两件事**。

        到点后、任何请求到来之前，库里应当仍是 `active` + `expires_at` 仍在未来
        —— 这证明本任务修的不是"TTL 到期"（那是 T-22/T-25 的领域），
        而是**另一条业务规则**。若有人把两者压成一个字段，这条用例会红。
        """
        sid = self.start()
        self.past_deadline(sid)
        status, _, _, _, expires_at = self.row(sid)
        self.assertEqual(status, "active")
        self.assertGreater(expires_at, datetime.datetime.utcnow().isoformat())

    def test_chat_after_deadline_returns_409_timeout(self):
        """**Bug 3B 的主现场**：到点后 `/api/chat` 必须 409（修复前是 200）。"""
        sid = self.start()
        self.past_deadline(sid)

        r = self.chat("超时之后我还想继续答")
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertEqual(body["code"], "interview_timeout")
        self.assertEqual(body["ended_reason"], "timeout")
        self.assertIsInstance(body["detail"], str)   # 前端直接渲染 detail
        self.assertIn("generate_report", body["actions"])
        self.assertIn("start_interview", body["actions"])

        status, reason, _, _, _ = self.row(sid)
        self.assertEqual((status, reason), ("abandoned", "timeout"))

    def test_second_chat_after_timeout_is_still_409(self):
        """兜底之后会话已不在：再发一次仍然是 409，**不可能**被答上。"""
        sid = self.start()
        self.past_deadline(sid)
        self.assertEqual(self.chat().status_code, 409)
        again = self.chat()
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["code"], "no_active_session")
        self.assertEqual(self.row(sid)[0], "abandoned")

    def test_skip_question_after_deadline_returns_409(self):
        """`skip_question` 也是写路径 —— 只拦 chat 会留下同一类漏洞。"""
        sid = self.start()
        self.past_deadline(sid)
        r = self.skip()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "interview_timeout")
        self.assertEqual(self.row(sid)[1], "timeout")

    def test_deadline_is_derived_from_server_created_at(self):
        """死线取 `created_at`：只是"接近"到点（还差 1 分钟）时必须照常答题。"""
        sid = self.start()
        self.age(sid, DURATION - 60)
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.row(sid)[0], "active")

    def test_other_users_session_is_untouched(self):
        """兜底按令牌解析到**自己的**会话，不碰别人的（会话键统一的前提）。"""
        other_sid = self.start(headers=self.h2)
        self.past_deadline(self.start())
        self.assertEqual(self.chat().status_code, 409)
        self.assertEqual(self.row(other_sid)[0], "active")
        self.assertEqual(self.chat(headers=self.h2).status_code, 200)


class LockReleaseTests(TimeoutGuardHarness):
    """D：唯一锁必须随超时释放 —— 不得把用户锁死在门外（ADR-022R）。"""

    def test_start_interview_directly_after_deadline_succeeds(self):
        """**ADR-022R 的核心承诺**：超时后立刻能开新面试。

        刻意**不**先打 `/api/chat`：用户在超时后最自然的动作就是点"重新开始"，
        这条路径必须自己不依赖任何其它请求（修复前要等 2 小时 TTL）。
        """
        first = self.start()
        self.past_deadline(first)

        r = self.client.post("/api/start_interview", headers=self.h,
                             data={"role": "后端开发", "questions_json": QUESTIONS})
        self.assertEqual(r.status_code, 200, r.text)
        second = r.json()["session_id"]
        self.assertNotEqual(second, first, "重开应当是一个**新**会话")

        # 旧行被**置终态**（不是删除）：内容要留给报告/历史，原因记 timeout。
        status, reason, _, _, _ = self.row(first)
        self.assertEqual((status, reason), ("abandoned", "timeout"))

    def test_abandon_click_after_deadline_records_timeout(self):
        """到点后用户才点"放弃并重开" → 真实原因是**超时**，不是主动放弃。

        `ended_reason` 决定评分口径（T-27 提示词区分 timeout / manual），
        写错会把一场超时的面试说成"候选人自己放弃了"。
        """
        sid = self.start()
        self.past_deadline(sid)

        r = self.abandon()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], "timeout")
        self.assertEqual(self.row(sid)[1], "timeout")

    def test_session_endpoint_self_heals_on_read_and_reports_last_ended(self):
        """刷新页面也算"服务端结算超时"的时机，且必须告诉前端**为什么**结束。

        超时兜底后 `session` 为 null；若不同时下发 `last_ended`，前端只能显示
        "你没有任何面试"，用户会以为进度丢了（FR-4.12 要的是明确反馈）。
        """
        sid = self.start()
        self.past_deadline(sid)

        body = self.get_session().json()
        self.assertIsNone(body["session"])
        self.assertIsNotNone(body["last_ended"])
        self.assertEqual(body["last_ended"]["session_id"], sid)
        self.assertEqual(body["last_ended"]["status"], "abandoned")
        self.assertEqual(body["last_ended"]["ended_reason"], "timeout")
        self.assertFalse(body["last_ended"]["has_report"])
        # 读接口不下发大字段（沿用 T-24 的口径）
        self.assertNotIn("report", body["last_ended"])

    def test_session_endpoint_before_deadline_keeps_session_untouched(self):
        sid = self.start()
        body = self.get_session().json()
        self.assertEqual(body["session"]["session_id"], sid)
        self.assertEqual(body["session"]["status"], "active")

    def test_session_payload_exposes_business_deadline(self):
        """前端倒计时应以服务端下发的死线为准，而不是自己硬编码 15 分钟。"""
        self.start()
        body = self.get_session().json()["session"]
        self.assertEqual(body["duration_seconds"], DURATION)
        self.assertIsNotNone(body["deadline_at"])
        self.assertLessEqual(body["interview_remaining_seconds"], DURATION)
        self.assertGreater(body["interview_remaining_seconds"], DURATION - 120)

    def test_config_exposes_duration(self):
        r = self.client.get("/api/interview/config", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["duration_seconds"], DURATION)


class ReportPathTests(TimeoutGuardHarness):
    """F：报告口径不回归（T-27）—— 兜底必须发生在判定 `ended_reason` 之前。"""

    def test_report_after_deadline_is_timeout_and_keeps_abandoned(self):
        """到点后直接出报告：必须是 `timeout` 口径、状态保持 `abandoned`。

        修复前（T-28 之前）`generate_report` 不看死线：它看到一行仍 active 的
        会话就判成 `completed` 并把状态写成 `finished` —— 一场超时的面试被
        说成"正常完成"，正是 T-27 要消除的误导。
        """
        sid = self.start()
        self.past_deadline(sid)

        sink = []
        r = self.report(sink=sink)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["ended_reason"], "timeout")
        self.assertEqual(body["answered_count"], 0)
        self.assertEqual(body["total_questions"], 3)
        self.assertIn(LABEL, body["details"])

        status, reason, report_json, _, _ = self.row(sid)
        self.assertEqual((status, reason), ("abandoned", "timeout"))
        self.assertTrue(report_json, "报告没有落库")

        # 真正喂给模型的 prompt 里必须写明"因超时自动结束"（口径由服务端给定）
        prompt = sink[-1]["user"]
        self.assertIn("ended_reason = timeout", prompt)
        self.assertIn("因超时自动结束", prompt)

    def test_report_without_timeout_stays_completed(self):
        """**不误伤**：正常答完的报告仍是 `completed` + `finished`。

        这条同时证明 `ended_reason` 由服务端时钟裁定 —— 客户端没有任何
        请求字段能声明"我超时了"来换一份宽松口径的分数。
        """
        sid = self.start()
        self.assertEqual(self.chat().status_code, 200)

        r = self.report()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], "completed")
        status, reason, report_json, _, _ = self.row(sid)
        self.assertEqual((status, reason), ("finished", "completed"))
        self.assertTrue(report_json)

    def test_finished_session_is_never_re_abandoned(self):
        """已正常结束的会话不会被兜底改写（兜底只对 `active` 行生效）。"""
        sid = self.start()
        self.chat()
        self.report()
        _, _, report_before, _, _ = self.row(sid)

        self.age(sid, DURATION + 60)          # 让它"看起来"早就超时了
        body = self.get_session().json()
        self.assertIsNone(body["session"])
        self.assertEqual(body["last_ended"]["status"], "finished")
        self.assertEqual(body["last_ended"]["ended_reason"], "completed")
        self.assertTrue(body["last_ended"]["has_report"])
        self.assertEqual(self.row(sid)[2], report_before, "报告被覆盖了")


if __name__ == "__main__":
    unittest.main()

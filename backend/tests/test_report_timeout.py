"""T-27 测试：评分提示词区分「未及作答 / 答不上」+ 报告新增 `ended_reason`（FR-4.5）。

## 要修的到底是什么

FR-5.2 的"**误导性 0 分**"：一个候选人因为**超时**一道题都没答上，
拿到的报告却和"全部答错"长得一模一样 —— 全 0 分，没有任何说明。
用户看到的是"我很差"，事实是"面试根本没进行下去"。

ADR-007R 的裁决把它拆成三件事，本文件逐条钉住：

  1. **超时也要出报告**：超时后会话是 `abandoned`，`get_active()` 取不到 ——
     修复前 `generate_report` 直接 409，用户拿不到任何结果。
  2. **`ended_reason` 由服务端写入**（completed / timeout / manual），
     **不采信模型、也不接受客户端自报** —— 否则前端说一句"我超时了"
     就能换到一份按宽松口径打的分数。
  3. **未及作答 ≠ 答不上**：`pending` 的题不得计入扣分；`skipped` 才是
     主动放弃、照常计分。零作答超时的报告必须显式标注"未及作答，无法评分"，
     而且这条**不能只靠提示词**——模型偶尔不遵守就会原样复现这个 Bug。
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from services.stores.base import EndedReason, SessionStatus, utcnow_iso  # noqa: E402
from services.stores.factory import get_session_store  # noqa: E402
from tests.support import bearer, create_test_user, delete_users, mint_token  # noqa: E402

USER = "t27_alice"

#: 假 AI 的固定回复。注意它**故意**把 answered_count 写成 7（会话里根本没那么多），
#: 用来验证服务端会用会话的事实覆盖模型的自报值。
FAKE_REPORT = {
    "expression_score": 4, "technical_score": 3, "logic_score": 4,
    "overall_score": 3.5, "answered_count": 7, "total_questions": 99,
    "suggestion": "假 AI 的固定建议。", "details": "假 AI 的固定总结。",
}


class _FakeMessage(object):
    def __init__(self, content):
        self.content = content


class _FakeResponse(object):
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _FakeMessage(content)})()]


class TimeoutReportHarness(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        delete_users([USER])

    def setUp(self):
        delete_users([USER])
        create_test_user(USER)
        self.uid = self._uid()
        self._purge_sessions()
        self.h = bearer(mint_token(USER))
        self.prompts = []
        self.calls = 0

    def _uid(self):
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return db.execute(text("SELECT id FROM users WHERE username=:u"),
                              {"u": USER}).scalar()
        finally:
            db.close()

    def _purge_sessions(self):
        """每个用例都从"这个用户没有任何会话"开始。

        不能只依赖 `delete_users` 的级联：若某次改动让 `PRAGMA foreign_keys`
        没生效，残留的 `abandoned` 行会被 `get_last_ended` 取到，
        于是"没有任何会话必须是 409"这条用例会因为**上一个用例的残留**而失败 ——
        那种失败指向的是测试污染，不是产品缺陷。显式清一次，代价可以忽略。
        """
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM interview_sessions WHERE user_id=:u"),
                       {"u": self.uid})
            db.commit()
        finally:
            db.close()

    # ---------------- 工具 ----------------

    def start(self, questions='["T27-Q1","T27-Q2","T27-Q3"]'):
        r = self.client.post("/api/start_interview", headers=self.h,
                             data={"role": "后端开发", "questions_json": questions})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["session_id"]

    def answer(self, text):
        """作答一轮。

        **必须**把面试官的 AI 调用也 mock 掉：`/api/chat` 每轮都会真的去调
        DeepSeek。不 mock 的话每个用例都会产生真实的网络请求与费用，
        而且一旦离线整批判定为失败 —— 那是"测试依赖外部服务"，
        不是"代码有问题"，两者必须分清。
        """
        with patch("routers.interview.client.chat.completions.create",
                   side_effect=lambda **k: _FakeResponse("【假面试官】收到，请继续。")):
            return self.client.post("/api/chat", headers=self.h,
                                    json={"message": text})

    def skip(self):
        return self.client.post("/api/skip_question", headers=self.h)

    def force_ended(self, session_id, reason):
        """把会话推到"已结束"状态 —— 用**真实存储方法**，不手改数据库。

        这就是 T-28 的超时兜底将要走的那条路（`abandon` + `TIMEOUT`），
        也是 T-22 的定时清理已经走过的路（`abandon_all_expired`）。
        """
        store = get_session_store()
        snap = store.get(session_id)
        res = store.abandon(session_id, snap.version, reason, utcnow_iso())
        self.assertTrue(res.applied, "把会话置为终态失败")

    def generate(self, payload=None, expect_calls=None):
        """调用 generate_report，并把喂给 AI 的 prompt 抓下来。"""
        def fake_create(**kwargs):
            self.calls += 1
            for m in kwargs.get("messages", []):
                self.prompts.append(m.get("content", ""))
            return _FakeResponse(json.dumps(FAKE_REPORT, ensure_ascii=False))

        with patch("routers.interview.client.chat.completions.create",
                   side_effect=fake_create):
            if payload is None:
                r = self.client.post("/api/generate_report", headers=self.h)
            else:
                r = self.client.post("/api/generate_report", headers=self.h,
                                     json=payload)
        if expect_calls is not None:
            self.assertEqual(self.calls, expect_calls,
                             "AI 调用次数不符（重复计费 / 该调没调）")
        return r

    @property
    def prompt(self):
        return "\n".join(self.prompts)

    def bullet(self, marker):
        """取出提示词里以 `marker` 开头的那一条规则（含其续行）。

        为什么要按"条"取，而不是在整段 prompt 里 `assertIn` 关键词：
        同一个词在提示词里往往出现多次（例如"不得计入扣分"在规则区和
        统计区各有一份）。只查关键词的话，把规则区那条改坏了，
        断言仍会因为"别处还有这个词"而**假通过** —— 这正是探针 P4 首轮
        会漏网的原因。规则类断言必须锁定到**那一条**。
        """
        lines = self.prompt.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith(marker):
                block = [ln]
                for nxt in lines[i + 1:]:
                    if not nxt.startswith("  "):
                        break
                    block.append(nxt)
                return "\n".join(block)
        self.fail("提示词里找不到以 %r 开头的规则" % marker)

    def row(self, session_id):
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return db.execute(text(
                "SELECT status, ended_reason, report FROM interview_sessions "
                "WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        finally:
            db.close()


class TimeoutPathTests(TimeoutReportHarness):
    """1. 超时后**仍能**出报告（修复前：409，用户什么都拿不到）。"""

    def test_timed_out_session_can_still_get_a_report(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)

        r = self.generate(expect_calls=1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], EndedReason.TIMEOUT)

    def test_report_row_stays_abandoned_not_finished(self):
        """**核心不变量**：报告落库了，但状态仍是 `abandoned`。

        ADR-022R 裁决"超时 → abandoned，不是 finished（面试并未正常完成）"。
        若为了写报告把状态改成 finished，库里就会出现"用户从未完成的面试
        被记成已完成" —— 状态语义被报告路径污染。
        """
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()

        row = self.row(sid)
        self.assertEqual(row[0], SessionStatus.ABANDONED)
        self.assertEqual(row[1], EndedReason.TIMEOUT, "ended_reason 被报告路径改写了")
        self.assertIsNotNone(row[2], "报告没有落库")
        self.assertIn('"overall_score"', row[2])

    def test_manual_abandon_is_reported_as_manual(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.MANUAL)

        r = self.generate()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], EndedReason.MANUAL)
        self.assertEqual(self.row(sid)[1], EndedReason.MANUAL)

    def test_active_session_is_still_completed(self):
        """回归：正常路径的行为不能因为 T-27 而改变。"""
        self.start()
        self.answer("正常作答")

        r = self.generate()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], EndedReason.COMPLETED)
        self.assertEqual(self.row_of_active()[0], SessionStatus.FINISHED)

    def row_of_active(self):
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return db.execute(text(
                "SELECT status, ended_reason FROM interview_sessions "
                "WHERE user_id=:u"), {"u": self.uid}).fetchone()
        finally:
            db.close()

    def test_second_request_does_not_re_score(self):
        """已经出过报告就不再重算：不重复计费，也不覆盖用户看过的结论。"""
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate(expect_calls=1)

        again = self.generate(expect_calls=1)     # 计数不变 = 没有再调 AI
        self.assertEqual(again.status_code, 409, "已出过报告还能再出一份")
        self.assertEqual(again.json()["code"], "no_active_session")

    def test_no_session_at_all_is_still_409(self):
        r = self.generate(expect_calls=0)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "no_active_session")

    def test_lock_is_free_so_a_new_interview_can_start(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()
        r = self.client.post("/api/start_interview", headers=self.h,
                             data={"role": "后端开发",
                                   "questions_json": '["新一轮"]'})
        self.assertEqual(r.status_code, 200, "超时出报告后用户被锁住了：%s" % r.text)


class EndedReasonIsServerAuthoritativeTests(TimeoutReportHarness):
    """2. `ended_reason` 只能由服务端写，客户端与模型都无权决定。"""

    def test_client_cannot_claim_timeout(self):
        """前端自报 `ended_reason=timeout` 必须无效 —— 否则可换取宽松评分。

        这里是一场**正常作答**的面试：服务端知道它是 completed，
        客户端在请求体里写什么都不该改变这个事实。
        """
        self.start()
        self.answer("正常作答")
        r = self.generate({"ended_reason": "timeout", "messages": [
            {"role": "user", "content": "T27-FORGED"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ended_reason"], EndedReason.COMPLETED)
        self.assertNotIn("T27-FORGED", self.prompt)

    def test_model_cannot_decide_ended_reason(self):
        """模型输出里的同名字段必须被服务端覆盖。"""
        self.start()
        self.force_ended(self._sid_of_active(), EndedReason.TIMEOUT)
        with patch("routers.interview.client.chat.completions.create",
                   side_effect=lambda **k: _FakeResponse(json.dumps(
                       dict(FAKE_REPORT, ended_reason="completed"),
                       ensure_ascii=False))):
            r = self.client.post("/api/generate_report", headers=self.h)
        self.assertEqual(r.json()["ended_reason"], EndedReason.TIMEOUT)

    def _sid_of_active(self):
        from database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return db.execute(text(
                "SELECT session_id FROM interview_sessions WHERE user_id=:u "
                "ORDER BY created_at DESC LIMIT 1"), {"u": self.uid}).scalar()
        finally:
            db.close()

    def test_counts_come_from_session_not_from_model(self):
        """模型把 answered_count 编成 7、total 编成 99，报告必须用会话的事实。"""
        self.start()                       # 3 道题
        self.answer("答第一题")             # answered = 1

        r = self.generate()
        body = r.json()
        self.assertEqual(body["total_questions"], 3)
        self.assertEqual(body["answered_count"], 1)

    def test_prompt_tells_the_model_the_ended_reason(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()
        self.assertIn("timeout", self.prompt)
        self.assertIn("因超时自动结束", self.prompt)


class AnswerStatusSemanticsTests(TimeoutReportHarness):
    """3. 提示词必须区分「未及作答」与「答不上」。"""

    def test_prompt_forbids_scoring_pending_as_wrong(self):
        sid = self.start()
        self.answer("答第一题")
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()

        text = self.prompt
        self.assertIn("[尚未作答]", text, "未作答的题没有如实标注")
        self.assertIn("未及作答", text)

        rule = self.bullet("- [尚未作答]")
        self.assertIn("不得计入扣分", rule,
                      "未及作答那条规则里没有「不得计入扣分」")
        self.assertIn("答不上", rule, "没有明确否定「这是答不上」")

    def test_prompt_says_skipped_is_scored_normally(self):
        """`skipped` = 主动放弃 = 答不上，必须照常计分 —— 与 pending 相反。

        断言锁定到**这一条规则**：光查"照常计分"会因为统计区里也有一份而假通过。
        """
        sid = self.start()
        self.skip()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()

        rule = self.bullet("- [跳过此题]")
        self.assertIn("照常计分", rule, "主动跳过那条规则里没有「照常计分」")
        self.assertIn("答不上", rule)

    def test_old_blind_zero_rule_is_narrowed(self):
        """修复前那条"一个都没答上就给全 0"的规则必须被收窄。

        旧文本（`interview.py` 原第 205 行）无条件生效，正是"误导性 0 分"的源头；
        新文本必须把"全 0"限制在"**真正问过**的题全部无效"这一条件下。
        """
        self.start()
        self.generate()

        text = self.prompt
        self.assertIn("真正问过", text)
        self.assertNotIn("如果候选人对你提出的问题一个都没有给出有效回答"
                         "（全是我不会/不知道/没学过/没接触过/跳过等），"
                         "则所有分数均为 0。", text)

    def test_prompt_carries_per_question_status(self):
        sid = self.start()
        self.answer("答第一题")
        self.skip()
        self.force_ended(sid, EndedReason.TIMEOUT)     # Q3 保持 pending
        self.generate()

        text = self.prompt
        self.assertIn("Q1: T27-Q1 -> answered", text)
        self.assertIn("Q2: T27-Q2 -> skipped", text)
        self.assertIn("Q3: T27-Q3 -> pending", text)


class ZeroAnswerTimeoutTests(TimeoutReportHarness):
    """4. 零作答超时**不得**被当成"全错"，必须有明确标注。"""

    def test_server_guarantees_the_label_even_if_model_omits_it(self):
        """**核心用例**：假 AI 返回一份"看起来像全错"的报告（无任何标注），
        服务端必须自己补上「未及作答，无法评分」。

        这条要求是 MUST，不能只写在提示词里指望模型遵守 ——
        模型偶尔不遵守，就会原样复现"误导性 0 分"。
        """
        sid = self.start()                      # 3 道题，一道都没答
        self.force_ended(sid, EndedReason.TIMEOUT)

        r = self.generate()
        body = r.json()
        self.assertEqual(body["ended_reason"], EndedReason.TIMEOUT)
        self.assertEqual(body["answered_count"], 0)
        self.assertIn("未及作答，无法评分", body["details"],
                      "零作答超时的报告没有任何标注 —— 这就是误导性 0 分")

    def test_label_is_persisted_too(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()
        self.assertIn("未及作答，无法评分", self.row(sid)[2])

    def test_prompt_forbids_calling_it_all_wrong(self):
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)
        self.generate()
        self.assertIn("未及作答，无法评分", self.prompt)

    def test_skipped_only_timeout_is_not_labelled_unanswered(self):
        """**反向误导**的防线：全部主动跳过时 answered_count 也是 0，
        但那是候选人明确拒绝回答 —— 贴上"未及作答，无法评分"等于把
        放弃说成没机会。ADR-007R 的条件要按"未及作答"的本义收窄。
        """
        sid = self.start('["T27-S1","T27-S2"]')
        self.skip()
        self.skip()
        self.force_ended(sid, EndedReason.TIMEOUT)

        r = self.generate()
        body = r.json()
        self.assertEqual(body["answered_count"], 0)
        self.assertEqual(body["ended_reason"], EndedReason.TIMEOUT)
        self.assertNotIn("未及作答，无法评分", body["details"],
                         "把「全部主动跳过」标成「未及作答」是反向误导")

    def test_completed_session_is_never_labelled_unanswered(self):
        """正常结束（非超时）不套用超时口径。"""
        self.start()
        r = self.generate()
        self.assertEqual(r.json()["ended_reason"], EndedReason.COMPLETED)
        self.assertNotIn("未及作答，无法评分", r.json()["details"])


class DegradedPathTests(TimeoutReportHarness):
    """评分服务异常时的降级结果也要带服务端事实。"""

    def test_degraded_report_still_carries_ended_reason_and_counts(self):
        sid = self.start()
        self.answer("答第一题")
        self.force_ended(sid, EndedReason.TIMEOUT)

        def boom(**kwargs):
            raise RuntimeError("模拟 AI 不可用")

        with patch("routers.interview.client.chat.completions.create",
                   side_effect=boom):
            r = self.client.post("/api/generate_report", headers=self.h)

        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["ended_reason"], EndedReason.TIMEOUT,
                         "降级报告缺 ended_reason —— 前端会把超时显示成正常完成")
        self.assertEqual(body["total_questions"], 3)
        self.assertEqual(body["answered_count"], 1)

    def test_degraded_timeout_does_not_persist_a_report(self):
        """降级结果不落库：它没有评分，落库会让"已有报告"挡住后续重试。"""
        sid = self.start()
        self.force_ended(sid, EndedReason.TIMEOUT)

        def boom(**kwargs):
            raise RuntimeError("模拟 AI 不可用")

        with patch("routers.interview.client.chat.completions.create",
                   side_effect=boom):
            self.client.post("/api/generate_report", headers=self.h)
        self.assertIsNone(self.row(sid)[2], "降级结果不该当成正式报告落库")


if __name__ == "__main__":
    unittest.main()

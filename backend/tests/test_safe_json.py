"""T-10 测试：JSON 文本列的容错解析（FR-6.4 / FR-2.5）。

漏洞背景：
  `interview_records.report` 是 TEXT 列。修复前 `user.py` 的历史列表与详情
  直接写 `json.loads(r.report)` —— report 为 NULL 时抛 TypeError，
  **单条坏记录即可让整个列表接口 500**。
  同文件 get_user_stats() 却包了 try/except，口径不统一。

修复后统一走 utils/safe_json.safe_json_loads()。
"""

import json
import unittest

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from database import InterviewRecord, SessionLocal
from tests.support import bearer, create_test_user, delete_users, mint_token
from utils.safe_json import safe_json_loads

USERNAME = "t10_user"
ADMIN = "t10_admin"


class SafeJsonLoadsUnitTests(unittest.TestCase):
    """纯函数层。"""

    def test_none_and_empty(self):
        self.assertIsNone(safe_json_loads(None))
        self.assertIsNone(safe_json_loads(""))
        self.assertEqual(safe_json_loads(None, default={}), {})

    def test_valid_json(self):
        self.assertEqual(safe_json_loads('{"a": 1}'), {"a": 1})
        self.assertEqual(safe_json_loads('[1, 2, 3]'), [1, 2, 3])
        self.assertEqual(safe_json_loads('"text"'), "text")

    def test_invalid_json_falls_back_without_raising(self):
        """关键：非法 JSON 必须返回默认值，而不是把异常抛给调用方。"""
        for bad in ['{not json', '{"a": }', 'undefined', "{'single': 'quotes'}", "\x00\x01"]:
            with self.subTest(text=bad):
                self.assertIsNone(safe_json_loads(bad))
                self.assertEqual(safe_json_loads(bad, default="FALLBACK"), "FALLBACK")

    def test_already_parsed_passthrough(self):
        self.assertEqual(safe_json_loads({"a": 1}), {"a": 1})
        self.assertEqual(safe_json_loads([1, 2]), [1, 2])

    def test_non_text_input_is_tolerated(self):
        """非文本输入（如误传数字）不应抛异常。"""
        self.assertIsNone(safe_json_loads(123))
        self.assertIsNone(safe_json_loads(object()))


class CorruptReportEndpointTests(unittest.TestCase):
    """端点层：含 NULL / 脏 report 的记录不得让接口 500。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.user_id = create_test_user(USERNAME, role="user")
        self.admin_id = create_test_user(ADMIN, role="admin")
        self.token = mint_token(USERNAME, role="user")
        self.admin_token = mint_token(ADMIN, role="admin")

        # 三条记录覆盖三种数据形态
        db = SessionLocal()
        try:
            self.ids = {}
            for label, report in [
                ("null", None),
                ("corrupt", "{ this is not json"),
                ("valid", json.dumps({"overall_score": 7.5, "details": "ok"})),
            ]:
                rec = InterviewRecord(
                    user_id=self.user_id, role="后端开发",
                    messages="[]", report=report, status="pending",
                )
                db.add(rec)
                db.commit()
                db.refresh(rec)
                self.ids[label] = rec.id
        finally:
            db.close()

    def tearDown(self):
        db = SessionLocal()
        try:
            db.query(InterviewRecord).filter(
                InterviewRecord.user_id == self.user_id
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        delete_users([USERNAME, ADMIN])

    def _cleanup_ids(self):
        return [self.ids["null"], self.ids["corrupt"], self.ids["valid"]]

    # ---------- 历史列表 ----------

    def test_history_does_not_500_on_null_or_corrupt_report(self):
        """修复前：report 为 NULL 会让整个列表 500（一条坏记录拖垮全部）。"""
        r = self.client.get("/api/history", headers=bearer(self.token))
        self.assertEqual(r.status_code, 200, "历史列表 500 了：%s" % r.text)

        by_id = {item["id"]: item for item in r.json()}
        self.assertIsNone(by_id[self.ids["null"]]["report"], "NULL report 应返回 null")
        self.assertIsNone(by_id[self.ids["corrupt"]]["report"], "脏 report 应返回 null")
        self.assertEqual(by_id[self.ids["valid"]]["report"]["overall_score"], 7.5)

    def test_history_item_does_not_500_on_null_or_corrupt_report(self):
        for label in ("null", "corrupt"):
            with self.subTest(case=label):
                r = self.client.get(
                    f"/api/history_item/{self.ids[label]}", headers=bearer(self.token)
                )
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIsNone(r.json()["report"])

    def test_history_item_valid_report_still_parsed(self):
        r = self.client.get(
            f"/api/history_item/{self.ids['valid']}", headers=bearer(self.token)
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["report"]["overall_score"], 7.5)

    # ---------- 个人统计 ----------

    def test_user_stats_survives_null_and_corrupt_reports(self):
        """修复前该接口虽有 try/except，但用的是裸 except: pass，口径不一致。"""
        r = self.client.get("/api/user/stats", headers=bearer(self.token))
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["total_interviews"], 3)
        # 只有 valid 那条贡献分数
        self.assertEqual(data["avg_score"], 7.5)

    # ---------- 管理端列表 ----------

    def test_admin_interviews_survives_null_and_corrupt_reports(self):
        r = self.client.get("/api/admin/interviews", headers=bearer(self.admin_token))
        self.assertEqual(r.status_code, 200, r.text)
        by_id = {item["id"]: item for item in r.json()}
        self.assertIsNone(by_id[self.ids["null"]]["report"])
        self.assertIsNone(by_id[self.ids["corrupt"]]["report"])

    # ---------- 表单里的非法 JSON ----------

    def test_start_interview_rejects_malformed_questions_json_with_400(self):
        """修复前 json.loads 直接抛 ValueError → 500；现在应为可读的 400。"""
        r = self.client.post(
            "/api/start_interview",
            data={"role": "backend", "resume_text": "", "questions_json": "{not json"},
            headers=bearer(self.token),
        )
        self.assertEqual(r.status_code, 400, "非法 questions_json 应得 400，实际 %s" % r.status_code)

    def test_start_interview_rejects_non_array_questions_json(self):
        r = self.client.post(
            "/api/start_interview",
            data={"role": "backend", "resume_text": "", "questions_json": '{"a": 1}'},
            headers=bearer(self.token),
        )
        self.assertEqual(r.status_code, 400)

    def test_start_interview_accepts_valid_questions(self):
        r = self.client.post(
            "/api/start_interview",
            data={"role": "backend", "resume_text": "", "questions_json": '["Q1","Q2"]'},
            headers=bearer(self.token),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

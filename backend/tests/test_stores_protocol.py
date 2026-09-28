"""T-16 测试：存储抽象层的协议定义。

本文件刻意**不走"调用一下看看"**的路子 —— 协议层没有行为可跑，
所以测的是**契约本身**：

1. 依赖面：只允许标准库；不得出现 redis / sqlalchemy / 项目模块；
2. 不泄露实现：不得出现 SQL 关键字（docstring 与注释除外）；
3. 协议完整性：方法名与参数名齐备（漏一个方法就是一个 T-19 的坑）；
4. 可鸭子类型实现：不继承也能通过 `isinstance`（证明"调用方只依赖协议"可行）；
5. 数据类型不可变：`frozen=True`（挡住"改了内存对象以为已持久化"）；
6. 时间契约：ISO 串的**字典序必须等于时间序**（这是 SQL 里直接比较的前提）；
7. 边界语义：`is_expired` 用 `<=`，等号算过期。
"""

import ast
import inspect
import os
import sys
import unittest
from dataclasses import fields, is_dataclass
from datetime import datetime

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services import stores  # noqa: E402
from services.stores import base as stores_base  # noqa: E402
from services.stores.base import (  # noqa: E402
    ActiveSessionExists,
    CaptchaStore,
    CommitResult,
    EndedReason,
    RateLimitStore,
    ReplayLookup,
    SessionDraft,
    SessionNotFound,
    SessionSnapshot,
    SessionStatus,
    SessionStore,
    StoreError,
    TERMINAL_STATUSES,
    iso_after,
    utcnow_iso,
)

BASE_PY = os.path.join(BACKEND_DIR, "services", "stores", "base.py")

#: 协议层只允许这些顶层模块（全部是标准库）。
ALLOWED_IMPORTS = {"dataclasses", "datetime", "typing"}

#: Redis 客户端 API 的典型方法名 —— 出现在代码里就说明抽象层漏了实现细节。
REDIS_API_CALLS = {
    "pipeline", "hset", "hget", "hgetall", "hdel", "zadd", "zrangebyscore",
    "zremrangebyscore", "setex", "getset", "incr", "decr", "ttl", "persist",
    "scan_iter",
}

#: 不得出现的存储相关第三方模块。
FORBIDDEN_MODULES = {"redis", "sqlalchemy", "pymysql", "psycopg2", "pymongo"}

SQL_KEYWORDS = ["SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE",
                "CREATE INDEX", "ALTER TABLE", "DROP ", "PRAGMA ",
                "BEGIN IMMEDIATE"]


def read_source():
    with open(BASE_PY, "r", encoding="utf-8") as fh:
        return fh.read()


def docstring_spans(tree):
    """收集所有 docstring 的行号区间，用于把它们从"代码"里排除。"""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                spans.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
    return spans


def code_lines_without_docstrings():
    """返回 (行号, 去注释后的代码) —— 已剔除 docstring 与 `#` 注释。"""
    source = read_source()
    spans = docstring_spans(ast.parse(source))
    out = []
    for idx, raw in enumerate(source.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in spans):
            continue
        out.append((idx, raw.split("#", 1)[0]))
    return out


def make_snapshot(**over):
    """构造一个合法快照，仅覆盖需要改的字段。"""
    kwargs = dict(
        session_id="s1", user_id=1, role="backend", questions=["q1", "q2", "q3"],
        question_status=["pending"] * 3, user_answers=[None] * 3,
        current_index=0, last_seq=0, version=0, status=SessionStatus.ACTIVE,
        created_at="2026-09-29T00:00:00", updated_at="2026-09-29T00:00:00",
        expires_at="2026-09-29T02:00:00",
    )
    kwargs.update(over)
    return SessionSnapshot(**kwargs)


class ProtocolDependencyTests(unittest.TestCase):
    """约束 1：只依赖标准库、不含 Redis / SQL。"""

    def test_imports_are_stdlib_only(self):
        tree = ast.parse(read_source())
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                self.assertIsNone(
                    node.level or None,
                    "协议层不得用相对 import（应为绝对 import，避免 sys.path 耦合）",
                )
                seen.add((node.module or "").split(".")[0])
        self.assertEqual(
            seen, ALLOWED_IMPORTS,
            "协议层只允许 import %s，实际：%s"
            % (sorted(ALLOWED_IMPORTS), sorted(seen)),
        )

    def test_no_forbidden_module_reference_in_code(self):
        tree = ast.parse(read_source())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_MODULES:
                offenders.append("Name(%s) @L%s" % (node.id, node.lineno))
            if isinstance(node, ast.Attribute) and node.attr in REDIS_API_CALLS:
                offenders.append("Attribute(.%s) @L%s" % (node.attr, node.lineno))
        self.assertEqual(offenders, [], "协议层出现存储实现细节：%s" % offenders)

    def test_no_sql_leaks_into_protocol_layer(self):
        offenders = []
        for lineno, code in code_lines_without_docstrings():
            for kw in SQL_KEYWORDS:
                if kw in code:
                    offenders.append("L%s: %s" % (lineno, code.strip()))
        self.assertEqual(
            offenders, [],
            "协议层不得出现 SQL（这会把调用方与具体存储绑死）：%s" % offenders,
        )

    def test_module_imports_with_forbidden_modules_poisoned(self):
        """把 redis / sqlalchemy 等设为 `sys.modules[...] = None` 后仍须能导入。

        `sys.modules[name] = None` 会让 `import name` 直接抛
        `ImportError: import of X halted; None in sys.modules` —— 这是 Python
        保证的语义。因此本用例证明的是**整个导入链**都不依赖它们，
        比 grep "有没有写 import redis" 更强。
        """
        import importlib.util

        saved = {}
        for name in FORBIDDEN_MODULES:
            saved[name] = sys.modules.get(name, "MISSING")
            sys.modules[name] = None
        try:
            spec = importlib.util.spec_from_file_location(
                "stores_base_poisoned_probe", BASE_PY
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            for name, original in saved.items():
                if original == "MISSING":
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        for attr in ("SessionStore", "CaptchaStore", "RateLimitStore",
                     "SessionSnapshot"):
            self.assertTrue(hasattr(module, attr), "毒化导入后缺少 %s" % attr)


class ProtocolCompletenessTests(unittest.TestCase):
    """约束 3：方法名与参数名齐备。漏一个方法 = T-19 的一个坑。"""

    EXPECTED = {
        SessionStore: {
            "get": ["session_id"],
            "get_active": ["user_id", "now"],
            "find_replay": ["session_id", "seq"],
            "abandon_expired_for_user": ["user_id", "now"],
            "create": ["draft"],
            "commit_turn": ["commit"],
            "finish": ["session_id", "expected_version", "report_json",
                       "ended_reason", "now"],
            "abandon": ["session_id", "expected_version", "ended_reason", "now"],
            "abandon_all_expired": ["now"],
        },
        CaptchaStore: {
            "save": ["captcha_id", "code", "expires_at"],
            "verify_and_consume": ["captcha_id", "code", "now"],
            "purge_expired": ["now"],
        },
        RateLimitStore: {
            "count_failures": ["ip", "since"],
            "record_failure": ["ip", "at"],
            "clear": ["ip"],
            "purge_older_than": ["before"],
        },
    }

    def test_all_protocol_methods_present(self):
        for proto, methods in self.EXPECTED.items():
            self.assertTrue(
                getattr(proto, "_is_protocol", False),
                "%s 必须是 typing.Protocol" % proto.__name__,
            )
            for name in methods:
                self.assertTrue(
                    hasattr(proto, name),
                    "%s 缺少方法 %s" % (proto.__name__, name),
                )

    def test_parameter_names_are_stable(self):
        """参数名就是关键字调用契约 —— 改名会静默破坏所有调用方。"""
        for proto, methods in self.EXPECTED.items():
            for name, expected_params in methods.items():
                func = getattr(proto, name)
                params = list(inspect.signature(func).parameters)
                self.assertEqual(
                    params, ["self"] + expected_params,
                    "%s.%s 参数名不符：期望 %s，实际 %s"
                    % (proto.__name__, name, ["self"] + expected_params, params),
                )

    def test_protocols_are_runtime_checkable(self):
        for proto in (SessionStore, CaptchaStore, RateLimitStore):
            self.assertTrue(
                getattr(proto, "_is_runtime_protocol", False),
                "%s 必须 @runtime_checkable，否则装配处无法做启动自检"
                % proto.__name__,
            )


class DuckTypingTests(unittest.TestCase):
    """约束 4：调用方只依赖协议 —— 不继承也必须能通过 isinstance。"""

    def test_stub_satisfies_session_store(self):
        class StubSessionStore(object):
            def get(self, session_id): return None
            def get_active(self, user_id, now): return None
            def find_replay(self, session_id, seq): return None
            def abandon_expired_for_user(self, user_id, now): return 0
            def create(self, draft): return None
            def commit_turn(self, commit): return CommitResult(applied=False)
            def finish(self, session_id, expected_version, report_json,
                       ended_reason, now): return CommitResult(applied=False)
            def abandon(self, session_id, expected_version, ended_reason,
                        now): return CommitResult(applied=False)
            def abandon_all_expired(self, now): return 0

        self.assertIsInstance(StubSessionStore(), SessionStore)

    def test_stub_missing_a_method_is_rejected(self):
        """缺方法的桩必须被识破 —— 否则装配期的自检形同虚设。"""

        class Incomplete(object):
            def get(self, session_id): return None

        self.assertNotIsInstance(Incomplete(), SessionStore)
        self.assertNotIsInstance(Incomplete(), CaptchaStore)

    def test_stub_satisfies_captcha_and_rate_limit(self):
        class StubCaptcha(object):
            def save(self, captcha_id, code, expires_at): pass
            def verify_and_consume(self, captcha_id, code, now): return False
            def purge_expired(self, now): return 0

        class StubRate(object):
            def count_failures(self, ip, since): return 0
            def record_failure(self, ip, at): pass
            def clear(self, ip): pass
            def purge_older_than(self, before): return 0

        self.assertIsInstance(StubCaptcha(), CaptchaStore)
        self.assertIsInstance(StubRate(), RateLimitStore)


class DataTypeTests(unittest.TestCase):
    """约束 5：数据类型不可变 + 字段齐备。"""

    def test_dataclasses_are_frozen(self):
        for cls in (SessionSnapshot, SessionDraft, CommitResult):
            self.assertTrue(is_dataclass(cls), "%s 应是 dataclass" % cls.__name__)
            self.assertTrue(
                cls.__dataclass_params__.frozen,
                "%s 必须 frozen —— 否则「改了内存对象以为已持久化」会静默发生"
                % cls.__name__,
            )

    def test_snapshot_rejects_mutation(self):
        snap = make_snapshot()
        with self.assertRaises(Exception):
            snap.current_index = 99
        with self.assertRaises(Exception):
            snap.version = 99

    def test_snapshot_fields_match_interview_sessions_columns(self):
        """快照字段必须与 `interview_sessions` 的列一一对应。

        这是"协议"与"DDL"之间的**跨层一致性**断言：将来只改了一边，
        这里会立刻报错，而不是等到 T-19 运行时才发现。
        （T-17 的测试会从另一个方向核对同一组名字。）

        注：`report` 是 T-19 补的 —— ADR-004 明确要求
        `UPDATE interview_sessions SET report=:json ...`，但 §6.2 的建表语句
        漏了该列；迁移 004 补上，两处才重新对齐。
        """
        expected = {
            "session_id", "user_id", "role", "questions", "question_status",
            "user_answers", "current_index", "last_seq", "last_reply", "version",
            "status", "created_at", "updated_at", "expires_at", "ended_reason",
            "report",
        }
        actual = {f.name for f in fields(SessionSnapshot)}
        self.assertEqual(
            actual, expected,
            "SessionSnapshot 字段与 interview_sessions 列不一致：多=%s 少=%s"
            % (sorted(actual - expected), sorted(expected - actual)),
        )

    def test_states_and_reasons_cover_ddl_values(self):
        self.assertEqual(set(SessionStatus.ALL), {"active", "finished", "abandoned"})
        self.assertEqual(set(EndedReason.ALL), {"completed", "timeout", "manual"})
        self.assertNotIn(SessionStatus.ACTIVE, TERMINAL_STATUSES)
        self.assertEqual(set(TERMINAL_STATUSES), {"finished", "abandoned"})

    def test_snapshot_summary_has_409_payload_keys(self):
        """ADR-022 R-10：409 响应体必须自带会话摘要，省掉一次往返。"""
        summary = make_snapshot().summary()
        self.assertEqual(
            set(summary),
            {"session_id", "current_index", "last_seq", "total", "status"},
        )
        self.assertEqual(summary["total"], 3)

    def test_draft_defaults_are_new_session_defaults(self):
        draft = SessionDraft(
            session_id="s2", user_id=1, role="backend", questions=["q"],
            question_status=["pending"], user_answers=[None], current_index=0,
            created_at="2026-09-29T00:00:00", updated_at="2026-09-29T00:00:00",
            expires_at="2026-09-29T02:00:00",
        )
        self.assertEqual(draft.status, SessionStatus.ACTIVE)
        self.assertEqual(draft.version, 0)
        self.assertEqual(draft.last_seq, 0)
        self.assertIsNone(draft.last_reply)

    def test_exception_hierarchy_is_catchable(self):
        self.assertTrue(issubclass(SessionNotFound, StoreError))
        self.assertTrue(issubclass(ActiveSessionExists, StoreError))
        err = ActiveSessionExists(user_id=7)
        self.assertEqual(err.user_id, 7)
        self.assertIn("7", str(err))


class ReplayLookupTests(unittest.TestCase):
    """T-19 补的协议修正：`find_replay` 的返回必须能区分两种「没有回复」。

    T-16 首版把返回类型写成 `Optional[str]`，用 `None` 同时表示
    "不是重发，要调 AI" 与 "是重发，但上次回复为空" —— 后者被误判会导致
    **重复调用 AI、重复计费**，正是 ADR-023 幂等机制要消除的问题。
    这个用例把"两种情形必须可区分"钉住。
    """

    def test_no_replay_and_empty_reply_are_distinguishable(self):
        fresh = ReplayLookup(is_replay=False)
        empty_replay = ReplayLookup(is_replay=True, reply=None)

        self.assertFalse(fresh.is_replay)
        self.assertTrue(empty_replay.is_replay)
        self.assertIsNone(empty_replay.reply)
        # 关键：两者都能表达出来，且不相等 —— 首版的 Optional[str] 做不到
        self.assertNotEqual(fresh, empty_replay)

    def test_replay_carries_reply(self):
        look = ReplayLookup(is_replay=True, reply="上次的回复")
        self.assertTrue(look.is_replay)
        self.assertEqual(look.reply, "上次的回复")

    def test_is_frozen(self):
        look = ReplayLookup(is_replay=False)
        with self.assertRaises(Exception):
            look.is_replay = True

    def test_find_replay_returns_optional_lookup(self):
        """返回类型注解必须是 `Optional[ReplayLookup]`（None 表示会话不存在）。"""
        import typing

        hints = typing.get_type_hints(SessionStore.find_replay)
        self.assertEqual(hints["return"], typing.Optional[ReplayLookup])


class TimeContractTests(unittest.TestCase):
    """约束 6/7：时间串契约与过期边界。"""

    def test_iso_after_adds_seconds(self):
        base = "2026-09-29T00:00:00"
        self.assertEqual(iso_after(60, base), "2026-09-29T00:01:00")
        self.assertEqual(iso_after(-60, base), "2026-09-28T23:59:00")

    def test_utcnow_iso_is_parseable_and_ordered(self):
        a = utcnow_iso()
        datetime.fromisoformat(a)  # 不能抛
        self.assertLess(a, iso_after(1, a))

    def test_lexicographic_order_equals_chronological_order(self):
        """这是 SQL 里直接写 `expires_at > :now` 的**前提**。

        若格式漂移（例如混入 `+00:00` 后缀或本地时间），比较不会报错，
        只会悄悄给出错误结果 —— 所以必须显式测。
        """
        base = "2026-09-29T00:00:00"
        samples = [iso_after(s, base) for s in (0, 1, 9, 10, 59, 60, 3600, 86400)]
        self.assertEqual(samples, sorted(samples))
        for i in range(len(samples) - 1):
            for j in range(i + 1, len(samples)):
                self.assertLess(
                    samples[i], samples[j],
                    "字典序与时间序矛盾：%s vs %s" % (samples[i], samples[j]),
                )

    def test_cross_day_boundary_still_ordered(self):
        base = "2026-09-29T23:59:30"
        self.assertLess(iso_after(30, base), iso_after(31, base))

    def test_is_expired_treats_equal_as_expired(self):
        """用 `<=` 而非 `<`：`expires_at == now` 必须算过期。

        差一个字符就会让"刚好到期"的那一瞬间仍能写入 —— TTL 语义要求
        边界归入"已过期"，与 SQL 里的 `expires_at > :now` 保持同一口径。
        """
        snap = make_snapshot()
        self.assertTrue(snap.is_expired(snap.expires_at))
        self.assertTrue(snap.is_expired(iso_after(1, snap.expires_at)))
        self.assertFalse(snap.is_expired(iso_after(-1, snap.expires_at)))

    def test_is_active_reflects_status(self):
        self.assertTrue(make_snapshot().is_active)
        self.assertFalse(make_snapshot(status=SessionStatus.ABANDONED).is_active)
        self.assertFalse(make_snapshot(status=SessionStatus.FINISHED).is_active)


class PackageExportTests(unittest.TestCase):
    """`services.stores` 必须把协议暴露出来，且与 base 是同一对象。"""

    REEXPORTS = (
        "SessionStore", "CaptchaStore", "RateLimitStore", "SessionSnapshot",
        "SessionDraft", "CommitResult", "StoreError", "ActiveSessionExists",
        "SessionNotFound", "SessionStatus", "EndedReason", "ReplayLookup",
        "TERMINAL_STATUSES", "utcnow_iso", "iso_after",
    )

    def test_reexports_are_identical_objects(self):
        for name in self.REEXPORTS:
            self.assertIs(
                getattr(stores, name), getattr(stores_base, name),
                "services.stores.%s 与 base 不是同一对象" % name,
            )

    def test_all_matches_actual_exports(self):
        for name in stores.__all__:
            self.assertTrue(hasattr(stores, name), "__all__ 里的 %s 不存在" % name)
        for name in ("SessionStore", "CaptchaStore", "RateLimitStore"):
            self.assertIn(name, stores.__all__)

    def test_exports_do_not_include_any_implementation(self):
        """抽象层不得导出具体实现 —— 一旦导出，调用方就会开始直接 import 它。"""
        self.assertFalse(
            any("sqlite" in n.lower() or "redis" in n.lower()
                for n in stores.__all__),
            "services.stores 不应导出实现类：%s" % stores.__all__,
        )


if __name__ == "__main__":
    unittest.main()

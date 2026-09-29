"""T-22 测试：清理任务（单实例锁 + 三类清理 + CLI）。

任务描述的两个关键词：
  * **只运行一个实例** —— 文件锁 + （Linux 侧）systemd `Type=oneshot`；
  * **过期会话置 `abandoned`；过期验证码/限流记录被清除**。

另外钉住两个**首版真的写错了**的地方（都是"看起来对、跑起来错"的类型）：

  1. `--db` 曾经无效：首版用 `os.environ["DATABASE_URL"]` 指定目标库，
     但 `database.py` 在 **import 期**就把 `DATABASE_URL` 读进模块变量了，
     之后再改环境变量不起作用 → `--db` 静默跑在默认库上。
     现在改为显式构造 engine，并有专门用例断言"另一个库没被动过"。

  2. 会话的过期判据曾经多减了一次 TTL（`now - 2h`）：
     会话的 2 小时已经写进 `expires_at`，再减一次等于
     **过期会话要再等 2 小时才释放唯一锁**，正好废掉 ADR-022R 的承诺。
     现在只传 `now`，并有专门用例（"1 分钟前刚过期也必须被处理"）。
"""

import contextlib
import datetime
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from scripts import cleanup  # noqa: E402
from scripts.cleanup import (  # noqa: E402
    acquire_lock,
    build_session_factory,
    lock_path_for,
    release_lock,
    run_cleanup,
)

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")


def iso(delta_seconds):
    return (datetime.datetime.utcnow()
            + datetime.timedelta(seconds=delta_seconds)).isoformat()


def make_db(path):
    """建一个跑完全部迁移的临时库。"""
    from migrations.runner import run as migrate
    migrate(path, backup=False, log=lambda *a, **k: None)
    return path


def seed(path):
    """造出"该被清理"与"不该被清理"两组数据。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executemany(
            "INSERT INTO users (username,email,role,is_active) VALUES (?,?,?,1)",
            [("u1", "u1@x.y", "user"), ("u2", "u2@x.y", "user")])
        # 部分唯一索引限制"同用户同时只能有一个 active" —— 造夹具时最容易踩
        sessions = [
            ("expired-active", 1, "active", iso(-3600)),
            ("finished-old", 1, "finished", iso(-3600)),
            ("fresh-active", 2, "active", iso(3600)),
        ]
        for sid, uid, status, exp in sessions:
            conn.execute(
                "INSERT INTO interview_sessions (session_id,user_id,role,questions,"
                "question_status,user_answers,current_index,status,created_at,"
                "updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, uid, "r", "[]", "[]", "[]", 0, status, exp, exp, exp))
        conn.execute("INSERT INTO captcha_store (captcha_id,code,expires_at,used) "
                     "VALUES ('cap-expired','1111',?,0)", (iso(-10),))
        conn.execute("INSERT INTO captcha_store (captcha_id,code,expires_at,used) "
                     "VALUES ('cap-live','2222',?,0)", (iso(600),))
        conn.execute("INSERT INTO auth_attempts (ip,attempted_at) VALUES ('1.1.1.1',?)",
                     (iso(-1800),))
        conn.execute("INSERT INTO auth_attempts (ip,attempted_at) VALUES ('1.1.1.1',?)",
                     (iso(-10),))
        # T-51：令牌吊销记录。一条已失效（该清）、一条仍生效（**绝不能**清 ——
        # 删掉仍生效的行就是让被吊销的令牌复活）。
        conn.execute("INSERT INTO token_blacklist (jti,expires_at) VALUES ('jti-expired',?)",
                     (iso(-10),))
        conn.execute("INSERT INTO token_blacklist (jti,expires_at) VALUES ('jti-live',?)",
                     (iso(3600),))
        conn.commit()
    finally:
        conn.close()


def q1(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def code_without_prose(relative_path):
    """读出源码，但**剔除 docstring 与注释**。

    为什么需要：本文件里有两条静态约束（"不得用 os.kill 判存活"、
    "不得自己写 SQL"）。用整份源码做文本扫描会把**解释这些约束的散文**
    也算进去 —— `cleanup.py` 的 docstring 恰好写了"为什么不用
    `os.kill(pid, 0)`"，首版就是这么误报的（与 T-17 的
    `ON DELETE CASCADE` 被当成破坏性语句是同一类假阳性）。
    """
    import ast

    path = os.path.join(BACKEND_DIR, relative_path)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                spans.append((body[0].lineno, body[0].end_lineno or body[0].lineno))

    out = []
    for idx, raw in enumerate(source.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in spans):
            continue
        out.append(raw.split("#", 1)[0])
    return "\n".join(out)


def kill_attribute_used(relative_path):
    """AST 精确判断"是否真的用了 `.kill(...)`"，不受散文影响。"""
    import ast

    path = os.path.join(BACKEND_DIR, relative_path)
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "kill":
            return True
    return False


class CleanupHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t22-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = make_db(os.path.join(self.tmp, "c.db"))
        seed(self.db)

    def cleanup(self, **kw):
        return run_cleanup(self.db, **kw)

    def status_of(self, sid):
        return q1(self.db, "SELECT status FROM interview_sessions WHERE session_id=?",
                  (sid,))


class LockTests(CleanupHarness):
    """任务关键词之一：**只运行一个实例**。"""

    def setUp(self):
        super(LockTests, self).setUp()
        self.lock = lock_path_for(self.db)
        self.addCleanup(release_lock, self.lock)

    def test_first_acquire_succeeds(self):
        self.assertTrue(acquire_lock(self.lock, log=lambda *a: None))

    def test_second_acquire_is_refused(self):
        self.assertTrue(acquire_lock(self.lock, log=lambda *a: None))
        self.assertFalse(acquire_lock(self.lock, log=lambda *a: None),
                         "第二个实例竟然拿到了锁 —— 清理会被并发执行")

    def test_release_allows_reacquire(self):
        acquire_lock(self.lock, log=lambda *a: None)
        release_lock(self.lock)
        self.assertTrue(acquire_lock(self.lock, log=lambda *a: None))

    def test_lock_records_pid_for_diagnostics(self):
        acquire_lock(self.lock, log=lambda *a: None)
        with open(self.lock, "r", encoding="utf-8") as fh:
            self.assertEqual(int(fh.readline().strip()), os.getpid())

    def test_stale_lock_is_taken_over(self):
        """上次崩溃留下的旧锁必须能抢占，否则清理会永久停摆。"""
        acquire_lock(self.lock, log=lambda *a: None)
        old = datetime.datetime.now() - datetime.timedelta(seconds=7200)
        os.utime(self.lock, (old.timestamp(), old.timestamp()))
        self.assertTrue(acquire_lock(self.lock, stale_after=3600,
                                     log=lambda *a: None))

    def test_fresh_lock_is_respected(self):
        acquire_lock(self.lock, log=lambda *a: None)
        self.assertFalse(acquire_lock(self.lock, stale_after=3600,
                                      log=lambda *a: None))

    def test_pid_is_not_used_for_liveness(self):
        """**不能**用 `os.kill(pid, 0)` 判存活：Windows 上那是真的去终止进程。

        用 AST 判断而不是文本扫描 —— 代码里没有 `.kill` 调用，
        文档里解释"为什么不用它"是应该的，不该被当成违规。
        """
        self.assertFalse(
            kill_attribute_used(os.path.join("scripts", "cleanup.py")),
            "清理脚本用了 .kill(...) 判进程存活 —— 在 Windows 上会误杀进程",
        )


class RunCleanupTests(CleanupHarness):

    def test_dry_run_reports_counts_without_changing_anything(self):
        stats = self.cleanup(dry_run=True)
        self.assertEqual(stats, {"sessions_abandoned": 1, "captchas_purged": 1,
                                 "attempts_purged": 1, "blacklist_purged": 1})
        # 什么都没改
        self.assertEqual(self.status_of("expired-active"), "active")
        self.assertEqual(q1(self.db, "SELECT count(*) FROM captcha_store"), 2)
        self.assertEqual(q1(self.db, "SELECT count(*) FROM auth_attempts"), 2)
        self.assertEqual(q1(self.db, "SELECT count(*) FROM token_blacklist"), 2)

    def test_blacklist_purge_never_removes_live_revocations(self):
        """T-51 / R-4 的关键一条：只清 `expires_at <= now` 的吊销记录。

        删掉**仍生效**的行 = 让被吊销的令牌复活 —— 那正是本次要堵的洞，
        而且这种 bug 在"刚登出还能用"与"登出后过一会儿又能用"之间极难区分。
        """
        self.cleanup()
        left = q1(self.db, "SELECT count(*) FROM token_blacklist")
        self.assertEqual(left, 1, "清理把仍生效的吊销记录也删了 —— 被吊销令牌会复活")
        self.assertEqual(
            q1(self.db, "SELECT jti FROM token_blacklist"), "jti-live")

    def test_real_run_matches_dry_run_preview(self):
        """预览与实际必须一致 —— 否则 dry-run 就没有参考价值。"""
        preview = self.cleanup(dry_run=True)
        actual = self.cleanup()
        self.assertEqual(preview, actual)

    def test_expired_active_session_becomes_abandoned(self):
        self.cleanup()
        self.assertEqual(self.status_of("expired-active"), "abandoned")
        self.assertEqual(
            q1(self.db, "SELECT ended_reason FROM interview_sessions "
                        "WHERE session_id='expired-active'"),
            "timeout", "过期会话应带上 ended_reason=timeout")

    def test_recently_expired_session_is_handled(self):
        """**回归用例**：1 分钟前刚过期的会话也必须被处理。

        首版把过期判据写成 `now - 2h`（多减了一次会话 TTL），
        这条会失败 —— 那样过期会话要再等 2 小时才释放唯一锁，
        正好废掉 ADR-022R"用户永远能开新面试"的承诺。
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE interview_sessions SET expires_at=? "
                         "WHERE session_id='fresh-active'", (iso(-60),))
            conn.commit()
        finally:
            conn.close()
        stats = self.cleanup()
        self.assertEqual(self.status_of("fresh-active"), "abandoned",
                         "刚刚过期的会话没被处理 —— 判据里多减了 TTL")
        self.assertEqual(stats["sessions_abandoned"], 2)

    def test_terminal_session_is_untouched(self):
        self.cleanup()
        self.assertEqual(self.status_of("finished-old"), "finished")

    def test_unexpired_session_is_untouched(self):
        self.cleanup()
        self.assertEqual(self.status_of("fresh-active"), "active")

    def test_sessions_are_not_deleted(self):
        """ADR-007R：超时后仍要出报告，会话行（含 questions/user_answers）必须留存。"""
        self.cleanup()
        self.assertEqual(q1(self.db, "SELECT count(*) FROM interview_sessions"), 3)

    def test_abandoning_releases_the_unique_lock(self):
        """清理的核心目的：让被过期会话挡住的用户能开新面试。"""
        self.cleanup()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO interview_sessions (session_id,user_id,role,questions,"
                "question_status,user_answers,current_index,status,created_at,"
                "updated_at,expires_at) VALUES ('brand-new',1,'r','[]','[]','[]',0,"
                "'active',?,?,?)", (iso(0), iso(0), iso(7200)))
            conn.commit()
        finally:
            conn.close()

    def test_expired_captcha_purged_live_kept(self):
        self.cleanup()
        remaining = [r[0] for r in sqlite3.connect(self.db).execute(
            "SELECT captcha_id FROM captcha_store").fetchall()]
        self.assertEqual(remaining, ["cap-live"])

    def test_out_of_window_attempt_purged_in_window_kept(self):
        self.cleanup()
        self.assertEqual(q1(self.db, "SELECT count(*) FROM auth_attempts"), 1)
        self.assertEqual(
            q1(self.db, "SELECT count(*) FROM auth_attempts "
                        "WHERE attempted_at >= ?", (iso(-600),)), 1)

    def test_is_idempotent(self):
        self.cleanup()
        second = self.cleanup()
        self.assertEqual(second, {"sessions_abandoned": 0, "captchas_purged": 0,
                                  "attempts_purged": 0, "blacklist_purged": 0})

    def test_uses_injected_session_factory_and_engine(self):
        """可注入装配（测试用），且注入时不去碰 db_path。"""
        engine, session_factory = build_session_factory(self.db)
        try:
            stats = run_cleanup("/nonexistent/path.db", session_factory=session_factory,
                                engine=engine)
        finally:
            engine.dispose()
        self.assertEqual(stats["sessions_abandoned"], 1)

    def test_delegates_to_stores_not_raw_sql(self):
        """静态约束：清理动作必须走协议，不得自己写 UPDATE/DELETE 语句。

        否则将来换存储载体时，这个脚本会被漏掉。
        （剔除 docstring 与注释后再扫 —— 说明文字里提到 SQL 不算违规。）
        """
        src = code_without_prose(os.path.join("scripts", "cleanup.py"))
        for forbidden in ("UPDATE interview_sessions", "DELETE FROM captcha_store",
                          "DELETE FROM auth_attempts"):
            self.assertNotIn(forbidden, src,
                             "清理脚本直接写了 SQL（%s）—— 应改为调用仓储协议" % forbidden)


class CliTests(CleanupHarness):

    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cleanup.main(argv)
        return code, buf.getvalue()

    def test_success_exit_code(self):
        code, out = self.run_main(["--db", self.db])
        self.assertEqual(code, 0)
        self.assertIn("CLEANUP_OK", out)

    def test_dry_run_exit_code_and_no_lock_file(self):
        code, out = self.run_main(["--db", self.db, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", out)
        self.assertFalse(os.path.exists(lock_path_for(self.db)),
                         "dry-run 不该留下锁文件")

    def test_missing_db_returns_2(self):
        code, _ = self.run_main(["--db", os.path.join(self.tmp, "nope.db")])
        self.assertEqual(code, 2)

    def test_busy_returns_3(self):
        """已有实例在跑 -> 退出码 3（**不算失败**，systemd 不该告警）。"""
        lock = lock_path_for(self.db)
        self.addCleanup(release_lock, lock)
        acquire_lock(lock, log=lambda *a: None)
        code, out = self.run_main(["--db", self.db])
        self.assertEqual(code, 3)
        self.assertIn("跳过", out)
        # 且确实什么都没做
        self.assertEqual(self.status_of("expired-active"), "active")

    def test_lock_is_released_after_normal_run(self):
        self.run_main(["--db", self.db])
        self.assertFalse(os.path.exists(lock_path_for(self.db)))

    def test_lock_is_released_even_if_cleanup_fails(self):
        """清理中途抛错也不能把锁留下 —— 否则后续 1 小时都跑不了。"""
        original = cleanup.run_cleanup

        def boom(*a, **kw):
            raise RuntimeError("模拟清理失败")

        cleanup.run_cleanup = boom
        try:
            code, _ = self.run_main(["--db", self.db])
        finally:
            cleanup.run_cleanup = original
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(lock_path_for(self.db)),
                         "失败后锁文件没被释放")

    def test_db_argument_actually_targets_that_db(self):
        """**回归用例**：`--db` 必须真的作用于指定库。

        首版用 `os.environ["DATABASE_URL"]` 指定目标库，但 `database.py`
        在 import 期就读走了该变量 → `--db` 静默跑在默认库上。
        这里同时准备两个库，断言"指定的那个变了、另一个没变"。
        """
        other = make_db(os.path.join(self.tmp, "other.db"))
        seed(other)

        code, _ = self.run_main(["--db", self.db])
        self.assertEqual(code, 0)

        self.assertEqual(self.status_of("expired-active"), "abandoned",
                         "--db 指定的库没有被清理")
        self.assertEqual(
            q1(other, "SELECT status FROM interview_sessions "
                      "WHERE session_id='expired-active'"),
            "active", "--db 之外的库被误改了（说明目标库没生效）")

    def test_no_lock_flag_skips_locking(self):
        lock = lock_path_for(self.db)
        self.addCleanup(release_lock, lock)
        acquire_lock(lock, log=lambda *a: None)
        code, _ = self.run_main(["--db", self.db, "--no-lock"])
        self.assertEqual(code, 0, "--no-lock 应当忽略已存在的锁")


class DeadlineConstantsTests(unittest.TestCase):
    """常量口径必须与架构 §6.3 一致（不一致是最难查的那类偏差）。"""

    def test_attempt_window_matches_config(self):
        from config import CAPTCHA_LOCK_MINUTES

        self.assertEqual(cleanup.ATTEMPT_WINDOW_SECONDS,
                         CAPTCHA_LOCK_MINUTES * 60)

    def test_cleanup_does_not_reapply_session_or_captcha_ttl(self):
        """会话/验证码的过期依据是各自的 `expires_at`，清理**不再减 TTL**。

        源码里不该再出现会话 TTL 或验证码 TTL 的常量 ——
        一旦出现，就说明又有人想把 `now - TTL` 当成"过期判据"了。
        （docstring 里提到这些名字是解释历史，不算违规。）
        """
        src = code_without_prose(os.path.join("scripts", "cleanup.py"))
        self.assertNotIn("SESSION_TTL_SECONDS", src)
        self.assertNotIn("CAPTCHA_TTL_SECONDS", src)


if __name__ == "__main__":
    unittest.main()

"""T-52 测试：每日备份 + 保留 7 份（ADR-019 / NFR-8）。

覆盖四类东西，**每一类都既有正向也有负向**（T-02 定下的规矩：
只测"好备份通过"不足以证明校验器有效 —— 一个恒返回 True 的校验器
也能通过全部正向用例）：

1. **一次备份的产物完整性**：目录结构、单文件库、zip 可读、manifest 可交叉核对；
2. **保留策略**：第 N+1 份落盘时删最老的、`--keep` 生效、
   **刚生成的那一份永不被删**（时钟被往前调时的自我保护）、
   半成品目录不占保留额度但会被清理；
3. **校验器 `verify_backup_dir`**：篡改 sha256 / 截断库 / 删 manifest /
   换掉 zip / manifest 声称备份了 .env 而文件不见了 —— 全都要被判 FAIL；
4. **单实例与退出码**：锁互斥、遗留锁抢占、失败也放锁、忙碌时退出码 3、
   `.env` 读不到只告警不失败（`--require-env` 时才失败）。

### 为什么用假时钟（patch `rotate.datetime`）

备份目录名是 `%Y%m%d_%H%M%S`，**这个名字就是排序键**。要测"保留 7 份"
就必须造出 8 个名字不同的备份；用真实时间连续跑 8 次会得到 8 个几乎同名的
目录（同一秒内甚至同名）—— 那样测的是"同一秒撞名"，测不到保留策略。
所以每次调用都把 `now()` 往前推一天，得到确定性的第 1..N 天。

顺带钉住一条真实约束：**同一秒重复运行不能互相覆盖**。真实部署里
timer 一天只触发一次，但手动补跑可能连着来；`main()` 遇到同名目录时
会先删掉它再重建（`os.replace` 要求目标不存在），因此是"覆盖为最新一次"
而不是"崩在第二次"。
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from scripts import rotate_backup as rb  # noqa: E402

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, is_active BOOLEAN);
CREATE TABLE interview_records (id INTEGER PRIMARY KEY, user_id INTEGER, report TEXT);
CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT);
"""

BASE_DAY = datetime.datetime(2026, 9, 30, 3, 30, 0)


def day(n):
    return BASE_DAY + datetime.timedelta(days=n)


def make_source_db(path, users=3, records=4):
    """造一个 WAL 模式、含三张业务表的源库（T-17 之后真库的常态）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        for i in range(users):
            conn.execute("INSERT INTO users (username, is_active) VALUES (?, 1)",
                         ("u%d" % i,))
        for i in range(records):
            conn.execute(
                "INSERT INTO interview_records (user_id, report) VALUES (1, ?)",
                ('{"x":1}',))
        conn.commit()
    finally:
        conn.close()
    return path


class BackupTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t52-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "backup")
        self.db = make_source_db(os.path.join(self.tmp, "interview.db"))
        self.uploads = os.path.join(self.tmp, "uploads")
        os.makedirs(os.path.join(self.uploads, "avatars"))
        self.avatar = os.path.join(self.uploads, "avatars", "user_1_ab.jpg")
        with open(self.avatar, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        self.env = os.path.join(self.tmp, ".env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("SECRET_KEY=abc123\nDEEPSEEK_API_KEY=sk-test\n")

    def run_once(self, moment, **overrides):
        """在指定时刻跑一次 `run_backup`，返回 (stats, code, 控制台输出)。"""
        kwargs = dict(db_path=self.db, uploads_dir=self.uploads,
                      env_path=self.env, out_dir=self.out)
        kwargs.update(overrides)
        buf = io.StringIO()
        stats, code = rb.run_backup(now=moment, log=buf.write, **kwargs)
        return stats, code, buf.getvalue()

    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = rb.main(argv)
        return code, buf.getvalue()

    def latest(self):
        names = rb.list_backup_dirs(self.out)
        return os.path.join(self.out, names[-1]) if names else None


class BackupArtifactTests(BackupTestBase):
    """一次备份到底产出了什么。"""

    def test_creates_timestamped_directory_with_four_artifacts(self):
        stats, code, _ = self.run_once(BASE_DAY)
        self.assertEqual(code, 0)
        self.assertTrue(stats, "备份失败：stats 为 None")
        self.assertEqual(stats["stamp"], "20260930_033000")
        target = os.path.join(self.out, "20260930_033000")
        self.assertEqual(stats["backup_dir"], target)
        for name in (rb.DB_NAME, rb.UPLOADS_ARCHIVE, rb.ENV_NAME, rb.MANIFEST_NAME):
            self.assertTrue(os.path.isfile(os.path.join(target, name)),
                            "缺少产物 %s" % name)

    def test_no_incomplete_directory_left_behind(self):
        """半成品目录名以 `.incomplete-` 开头，落盘后不该残留。"""
        self.run_once(BASE_DAY)
        self.assertEqual(rb.list_incomplete_dirs(self.out), [])

    def test_database_backup_is_single_file_and_not_wal(self):
        """T-17 的坑：WAL 源库的备份若仍是 WAL，就不是单文件自洽的。"""
        self.run_once(BASE_DAY)
        db = os.path.join(self.latest(), rb.DB_NAME)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(os.path.exists(db + suffix),
                             "备份库留下了附属文件 %s" % suffix)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            conn.close()

    def test_database_backup_has_all_rows(self):
        self.run_once(BASE_DAY)
        db = os.path.join(self.latest(), rb.DB_NAME)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM interview_records").fetchone()[0], 4)
        finally:
            conn.close()

    def test_manifest_records_row_counts_and_hashes(self):
        self.run_once(BASE_DAY)
        with open(os.path.join(self.latest(), rb.MANIFEST_NAME), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["database"]["integrity_check"], "ok")
        self.assertEqual(m["database"]["row_counts"]["users"], 3)
        self.assertEqual(m["policy"]["keep"], 7)
        # sha256 必须与真实文件一致，否则 manifest 只是装饰
        self.assertEqual(
            m["database"]["sha256"], rb.sha256_of(os.path.join(self.latest(), rb.DB_NAME)))
        self.assertEqual(m["env_file"]["sha256"],
                         rb.sha256_of(os.path.join(self.latest(), rb.ENV_NAME)))

    def test_manifest_contains_pasteable_restore_hint(self):
        """事故现场没人翻文档 —— 恢复命令必须固化在产物里。"""
        self.run_once(BASE_DAY)
        with open(os.path.join(self.latest(), rb.MANIFEST_NAME), encoding="utf-8") as f:
            m = json.load(f)
        hint = m["restore_hint"]
        self.assertIn(rb.DB_NAME, hint)
        self.assertIn(rb.UPLOADS_ARCHIVE, hint)
        self.assertIn("systemctl stop", hint)

    def test_uploads_archive_contains_the_files(self):
        self.run_once(BASE_DAY)
        with zipfile.ZipFile(os.path.join(self.latest(), rb.UPLOADS_ARCHIVE)) as zf:
            self.assertEqual(zf.namelist(), ["avatars/user_1_ab.jpg"])
            self.assertEqual(zf.read("avatars/user_1_ab.jpg"),
                             open(self.avatar, "rb").read())

    def test_uploads_archive_keeps_nested_paths(self):
        deep = os.path.join(self.uploads, "avatars", "2026", "09")
        os.makedirs(deep)
        with open(os.path.join(deep, "deep.jpg"), "wb") as f:
            f.write(b"deep")
        self.run_once(BASE_DAY)
        with zipfile.ZipFile(os.path.join(self.latest(), rb.UPLOADS_ARCHIVE)) as zf:
            self.assertIn("avatars/2026/09/deep.jpg", zf.namelist())

    def test_missing_uploads_dir_is_not_a_failure(self):
        """uploads 为空不是错误：全新部署还没有人上传过头像。"""
        shutil.rmtree(self.uploads)
        stats, code, out = self.run_once(BASE_DAY)
        self.assertEqual(code, 0)
        self.assertEqual(stats["uploads_files"], 0)
        self.assertIn("uploads 目录不存在", out)
        with zipfile.ZipFile(os.path.join(self.latest(), rb.UPLOADS_ARCHIVE)) as zf:
            self.assertEqual(zf.namelist(), [])

    def test_shape_verified_immediately_after_write(self):
        """写完立刻回读校验：证明"现在就能恢复"，而不是三个月后才发现坏。"""
        self.run_once(BASE_DAY)
        ok, problems, info = rb.verify_backup_dir(self.latest())
        self.assertTrue(ok, problems)
        self.assertEqual(info["row_counts"]["users"], 3)


class EnvFileTests(BackupTestBase):
    """.env 的处理：数据备份不因配置缺失而失败（设计决策，见脚本 docstring）。"""

    def test_missing_env_warns_but_still_exits_zero(self):
        os.remove(self.env)
        stats, code, out = self.run_once(BASE_DAY)
        self.assertEqual(code, 0, "配置缺失不该让数据备份判失败")
        self.assertFalse(stats["env_backed_up"])
        self.assertIn("警告", out)
        self.assertFalse(os.path.exists(os.path.join(self.latest(), rb.ENV_NAME)))
        with open(os.path.join(self.latest(), rb.MANIFEST_NAME), encoding="utf-8") as f:
            m = json.load(f)
        self.assertIsNone(m["env_file"])
        self.assertTrue(m["env_warning"], "manifest 必须如实记下为什么没有 .env")

    def test_require_env_makes_it_a_hard_failure(self):
        os.remove(self.env)
        stats, code, out = self.run_once(BASE_DAY, require_env=True)
        self.assertEqual(code, 2)
        self.assertIsNone(stats)
        self.assertIn("require-env", out)
        self.assertEqual(rb.list_backup_dirs(self.out), [],
                         "硬失败时不该留下半个备份目录")

    def test_backup_still_verifies_when_env_succeeded(self):
        self.run_once(BASE_DAY)
        ok, problems, _ = rb.verify_backup_dir(self.latest())
        self.assertTrue(ok, problems)


class RetentionTests(BackupTestBase):
    """保留策略（ADR-019 的验收口径："保留 7 份"）。

    口径：`--keep N` = **运行结束后磁盘上的备份目录总数上限**。
    因此稳态是 N 个目录、每次新备份落盘后删掉最老的那一份。
    判据对真跑与 `--dry-run` 完全一致（"本次要落盘的那一份"同样占名额）。
    """

    def test_keeps_seven_by_default(self):
        for n in range(10):
            _, code, _ = self.run_once(day(n))
            self.assertEqual(code, 0, "第 %d 天的备份失败" % n)
        names = rb.list_backup_dirs(self.out)
        self.assertEqual(len(names), 7, "应保留 7 份，实得 %d 份" % len(names))
        # 留下的是**最新的 7 份**（第 3..9 天）
        self.assertEqual(names[0], rb.stamp_of(day(3)))
        self.assertEqual(names[-1], rb.stamp_of(day(9)))

    def test_early_runs_do_not_prune_anything(self):
        """前 7 天什么都不该删 —— 一开始就删说明判据写反了。"""
        for n in range(7):
            stats, code, _ = self.run_once(day(n))
            self.assertEqual(code, 0)
            self.assertEqual(stats["pruned"], [],
                             "第 %d 天就删了备份：%s" % (n, stats["pruned"]))
        self.assertEqual(len(rb.list_backup_dirs(self.out)), 7)

    def test_keep_is_configurable(self):
        for n in range(5):
            self.run_once(day(n), keep=2)
        names = rb.list_backup_dirs(self.out)
        self.assertEqual(names, [rb.stamp_of(day(3)), rb.stamp_of(day(4))])

    def test_keep_one_keeps_only_the_newest(self):
        """keep=1 的语义："只留最新这一份"。

        **回归用例**：首版把预算写成 `keep - 受保护数`，于是 keep=1 时
        第一轮轮转就把唯一的历史备份删了 —— "留 1 份"被实现成"旧的一律不要"。
        """
        for n in range(3):
            self.run_once(day(n), keep=1)
        self.assertEqual(rb.list_backup_dirs(self.out), [rb.stamp_of(day(2))])

    def test_keep_zero_is_rejected(self):
        _, code, out = self.run_once(BASE_DAY, keep=0)
        self.assertEqual(code, 2)
        self.assertIn("--keep", out)

    def test_just_created_backup_is_never_pruned(self):
        """时钟被往前调时，"最新"可能不是"刚生成的" —— 那份必须受保护。

        不保护就会退化成"备份完立刻把自己删了"，比不备份更危险：
        目录看起来还在轮转，实际一份都不剩。
        """
        for n in range(7):
            self.run_once(day(n))
        # 手工造一个"未来"的目录，然后让时钟回到过去再备份一次
        future = os.path.join(self.out, "20301231_235959")
        os.makedirs(future)
        stale_stats, code, _ = self.run_once(day(0))
        self.assertEqual(code, 0)
        self.assertIn(rb.stamp_of(day(0)), rb.list_backup_dirs(self.out),
                      "刚生成的备份被当成最老的一份删掉了")
        # 而且它落到磁盘上了（不是"算完就丢"）
        ok, problems, _ = rb.verify_backup_dir(stale_stats["backup_dir"])
        self.assertTrue(ok, problems)

    def test_prune_dry_run_deletes_nothing(self):
        # 用第 10 天起的时间戳，避免与后面手工造的最老目录撞名
        for n in range(10, 14):
            self.run_once(day(n), keep=3)
        before = rb.list_backup_dirs(self.out)
        self.assertEqual(len(before), 3, "keep=3 稳态应是 3 份")
        # 手工多放一份最老的，制造"确实需要删一个"的局面
        os.makedirs(os.path.join(self.out, rb.stamp_of(day(0))))
        before = rb.list_backup_dirs(self.out)
        self.assertEqual(len(before), 4)
        removed = rb.prune(self.out, keep=3, protect=[rb.stamp_of(day(13))],
                           dry_run=True, log=lambda *a: None)
        self.assertEqual(removed, [rb.stamp_of(day(0))])
        self.assertEqual(rb.list_backup_dirs(self.out), before,
                         "dry-run 竟然真的删了目录")

    def test_prune_dry_run_matches_a_real_prune(self):
        """同参数、同初始状态：dry-run 报的名单必须与真跑删掉的一致。"""
        for n in range(5):
            self.run_once(day(n), keep=3)
        snapshot = rb.list_backup_dirs(self.out)
        hypothetical = rb.prune(self.out, keep=3, protect=[rb.stamp_of(day(4))],
                                dry_run=True, log=lambda *a: None)
        real = rb.prune(self.out, keep=3, protect=[rb.stamp_of(day(4))],
                        dry_run=False, log=lambda *a: None)
        self.assertEqual(hypothetical, real)
        # 真跑之后剩下的，正是快照里除被删者之外那些（顺序也是时间序）
        expected_left = [n for n in snapshot if n not in hypothetical]
        self.assertEqual(rb.list_backup_dirs(self.out), expected_left)
        self.assertLessEqual(len(expected_left), 3)

    def test_prune_protects_the_fresh_backup_even_if_not_newest(self):
        """直接钉住 `prune()` 的保护语义（这是最容易写错的一处）。"""
        for n in range(4):
            os.makedirs(os.path.join(self.out, rb.stamp_of(day(n))))
        fresh = rb.stamp_of(day(0))          # 最老的一个，但它是"刚生成的"
        removed = rb.prune(self.out, keep=1, protect=[fresh],
                           dry_run=False, log=lambda *a: None)
        self.assertNotIn(fresh, removed)
        self.assertIn(fresh, rb.list_backup_dirs(self.out))
        # keep=1：受保护的 fresh 占掉唯一名额，其余全删
        self.assertEqual(rb.list_backup_dirs(self.out), [fresh])

    def test_incomplete_dirs_are_cleaned_and_do_not_consume_quota(self):
        leftover = os.path.join(self.out, rb.TMP_PREFIX + "20260929_033000")
        os.makedirs(leftover)
        with open(os.path.join(leftover, "junk"), "w") as f:
            f.write("half-written")
        self.run_once(BASE_DAY)
        self.assertFalse(os.path.exists(leftover), "半成品目录没有被清理")
        self.assertEqual(rb.list_backup_dirs(self.out), [rb.stamp_of(BASE_DAY)])

    def test_incomplete_dirs_are_not_listed_as_backups(self):
        os.makedirs(os.path.join(self.out, rb.TMP_PREFIX + "20260929_033000"))
        self.assertEqual(rb.list_backup_dirs(self.out), [])

    def test_non_timestamp_directories_are_left_alone(self):
        """备份根目录下别人放的东西（如手工恢复出来的目录）不该被轮转删掉。"""
        keep_me = os.path.join(self.out, "manual-restore-2026")
        os.makedirs(keep_me)
        for n in range(9):
            self.run_once(day(n))
        self.assertTrue(os.path.isdir(keep_me), "非备份目录被轮转删掉了")


class VerifyTests(BackupTestBase):
    """校验器：正向必须通过，**每一类损坏都必须被抓住**。"""

    def setUp(self):
        super().setUp()
        self.run_once(BASE_DAY)
        self.target = self.latest()

    def test_healthy_backup_passes(self):
        ok, problems, info = rb.verify_backup_dir(self.target)
        self.assertTrue(ok, problems)
        self.assertEqual(info["journal_mode"], "delete")

    def test_tampered_database_fails_sha_check(self):
        db = os.path.join(self.target, rb.DB_NAME)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO users (username, is_active) VALUES ('intruder', 1)")
        conn.commit()
        conn.close()
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok, "被改动的库必须判 FAIL")
        self.assertTrue(any("sha256" in p for p in problems), problems)

    def test_truncated_database_fails(self):
        db = os.path.join(self.target, rb.DB_NAME)
        with open(db, "r+b") as f:
            f.truncate(200)
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok, "截断的库必须判 FAIL")
        self.assertTrue(problems)

    def test_missing_database_fails(self):
        os.remove(os.path.join(self.target, rb.DB_NAME))
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("interview.db" in p for p in problems))

    def test_missing_manifest_fails(self):
        os.remove(os.path.join(self.target, rb.MANIFEST_NAME))
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("manifest" in p for p in problems))

    def test_corrupt_manifest_fails(self):
        with open(os.path.join(self.target, rb.MANIFEST_NAME), "w") as f:
            f.write("{ this is not json")
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("manifest" in p for p in problems))

    def test_row_count_mismatch_fails_even_with_matching_sha(self):
        """防"manifest 被复制错"：行数是与 sha256 独立的第二道判据。"""
        path = os.path.join(self.target, rb.MANIFEST_NAME)
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        m["database"]["row_counts"]["users"] = 999
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("行数不符" in p for p in problems), problems)

    def test_replaced_uploads_archive_fails(self):
        with zipfile.ZipFile(os.path.join(self.target, rb.UPLOADS_ARCHIVE), "w") as zf:
            zf.writestr("something-else.txt", "x")
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok, "条目数对不上必须判 FAIL")
        self.assertTrue(any("uploads.zip" in p for p in problems), problems)

    def test_garbage_uploads_archive_fails(self):
        with open(os.path.join(self.target, rb.UPLOADS_ARCHIVE), "wb") as f:
            f.write(b"not a zip at all")
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("zip" in p for p in problems), problems)

    def test_missing_uploads_archive_fails(self):
        os.remove(os.path.join(self.target, rb.UPLOADS_ARCHIVE))
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any("uploads.zip" in p for p in problems))

    def test_env_claimed_but_absent_fails(self):
        os.remove(os.path.join(self.target, rb.ENV_NAME))
        ok, problems, _ = rb.verify_backup_dir(self.target)
        self.assertFalse(ok)
        self.assertTrue(any(".env" in p for p in problems))

    def test_run_verify_reports_all_backups(self):
        self.run_once(day(1))
        buf = io.StringIO()
        good, bad = rb.run_verify(self.out, log=buf.write)
        self.assertEqual((good, bad), (2, []))
        self.assertIn("2 份通过 / 0 份不健康", buf.getvalue())

    def test_run_verify_detects_a_bad_one(self):
        self.run_once(day(1))
        os.remove(os.path.join(self.out, rb.stamp_of(day(1)), rb.DB_NAME))
        buf = io.StringIO()
        good, bad = rb.run_verify(self.out, log=buf.write)
        self.assertEqual(good, 1)
        self.assertEqual(bad, [rb.stamp_of(day(1))])


class DryRunTests(BackupTestBase):

    def test_dry_run_writes_nothing(self):
        stats, code, out = self.run_once(BASE_DAY, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", out)
        self.assertFalse(os.path.exists(self.out), "dry-run 竟然创建了目录")

    def test_dry_run_and_real_run_agree_on_counts(self):
        """预览与实际必须同口径（否则"预览说删 1 份、真跑删 3 份"）。

        `keep=1`：第一天留下 day0；第二天新备份落盘后删掉 day0；第三天删 day1。
        于是"明天会删谁"的正确答案是**当前唯一那一份**（day2）。
        """
        for n in range(3):
            self.run_once(day(n), keep=1)
        self.assertEqual(rb.list_backup_dirs(self.out), [rb.stamp_of(day(2))])

        dry_stats, _, _ = self.run_once(day(3), keep=1, dry_run=True)
        real_stats, _, _ = self.run_once(day(3), keep=1, dry_run=False)
        self.assertEqual(dry_stats["pruned"], real_stats["pruned"])
        self.assertEqual(dry_stats["pruned"], [rb.stamp_of(day(2))])
        self.assertEqual(rb.list_backup_dirs(self.out), [rb.stamp_of(day(3))])

    def test_dry_run_does_not_prune(self):
        for n in range(3):
            self.run_once(day(n), keep=1)
        before = rb.list_backup_dirs(self.out)
        self.run_once(day(3), keep=1, dry_run=True)
        self.assertEqual(rb.list_backup_dirs(self.out), before)


class CliTests(BackupTestBase):

    def test_default_run_exits_zero_and_prints_backup_ok(self):
        code, out = self.run_main(["--db", self.db, "--uploads", self.uploads,
                                   "--env-file", self.env, "--out", self.out])
        self.assertEqual(code, 0, out)
        self.assertIn("BACKUP_OK", out)
        self.assertIn("自校验: 通过", out)

    def test_missing_db_returns_2(self):
        code, _ = self.run_main(["--db", os.path.join(self.tmp, "nope.db"),
                                 "--out", self.out])
        self.assertEqual(code, 2)

    def test_dry_run_cli_leaves_no_lock_file(self):
        code, out = self.run_main(["--db", self.db, "--uploads", self.uploads,
                                   "--env-file", self.env, "--out", self.out,
                                   "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", out)
        self.assertFalse(os.path.exists(rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX)))

    def test_lock_is_released_after_normal_run(self):
        self.run_main(["--db", self.db, "--uploads", self.uploads,
                       "--env-file", self.env, "--out", self.out])
        self.assertFalse(os.path.exists(rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX)))

    def test_busy_returns_3_and_does_nothing(self):
        """已有实例在跑 -> 退出码 3（**不算失败**，systemd 不该告警）。"""
        lock = rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX)
        self.addCleanup(rb.release_lock, lock)
        self.assertTrue(rb.acquire_lock(lock, log=lambda *a: None))
        code, out = self.run_main(["--db", self.db, "--uploads", self.uploads,
                                   "--env-file", self.env, "--out", self.out])
        self.assertEqual(code, 3)
        self.assertIn("跳过", out)
        self.assertEqual(rb.list_backup_dirs(self.out), [], "跳过时不该产生备份")

    def test_verify_mode_passes_on_healthy_backups(self):
        self.run_main(["--db", self.db, "--uploads", self.uploads,
                       "--env-file", self.env, "--out", self.out])
        code, out = self.run_main(["--out", self.out, "--verify"])
        self.assertEqual(code, 0, out)
        self.assertIn("1 份通过", out)

    def test_verify_mode_exits_1_on_damaged_backup(self):
        self.run_main(["--db", self.db, "--uploads", self.uploads,
                       "--env-file", self.env, "--out", self.out])
        os.remove(os.path.join(self.latest(), rb.DB_NAME))
        code, out = self.run_main(["--out", self.out, "--verify"])
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL", out)

    def test_verify_with_no_backups_exits_0_with_message(self):
        code, out = self.run_main(["--out", self.out, "--verify"])
        self.assertEqual(code, 0)
        self.assertIn("没有找到任何备份", out)

    def test_second_run_same_second_overwrites_instead_of_crashing(self):
        """同一秒重跑：timer 一天只触发一次，但人工补跑可能连着来。

        `os.replace(tmp, final)` 要求目标不存在，因此第二次必须**先清掉同名目录**；
        否则补跑会以 `OSError: Directory not empty` 崩掉，而日志只会说"备份失败"。
        """
        first_stats, first, _ = self.run_once(BASE_DAY)
        second_stats, second, _ = self.run_once(BASE_DAY)
        self.assertEqual((first, second), (0, 0))
        self.assertEqual(rb.list_backup_dirs(self.out), [rb.stamp_of(BASE_DAY)])
        self.assertEqual(second_stats["backup_dir"], first_stats["backup_dir"])
        ok, problems, _ = rb.verify_backup_dir(second_stats["backup_dir"])
        self.assertTrue(ok, problems)

    def test_failure_leaves_no_lock_behind(self):
        """备份中途抛错也不能把锁留下 —— 否则后续 1 小时全都跑不了。"""
        original = rb.backup_database

        def boom(*a, **kw):
            raise RuntimeError("模拟备份失败")

        rb.backup_database = boom
        try:
            code, out = self.run_main(["--db", self.db, "--uploads", self.uploads,
                                       "--env-file", self.env, "--out", self.out])
        finally:
            rb.backup_database = original
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX)),
                         "失败后锁文件没被释放")


class LockTests(BackupTestBase):
    """单实例锁（T-52 起与 cleanup.py 共用 `single_instance` 模块）。"""

    def setUp(self):
        super().setUp()
        self.lock = rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX)

    def test_lock_is_mutually_exclusive(self):
        self.assertTrue(rb.acquire_lock(self.lock, log=lambda *a: None))
        self.addCleanup(rb.release_lock, self.lock)
        self.assertFalse(rb.acquire_lock(self.lock, log=lambda *a: None),
                         "第二个实例竟然也拿到了锁")

    def test_stale_lock_is_taken_over(self):
        """上次崩溃留下的锁（超过 stale-after）必须能被抢占。"""
        with open(self.lock, "w") as f:
            f.write("999999\n")
        old = (datetime.datetime.now() - datetime.timedelta(hours=3)).timestamp()
        os.utime(self.lock, (old, old))
        self.assertTrue(rb.acquire_lock(self.lock, stale_after=3600,
                                        log=lambda *a: None))
        rb.release_lock(self.lock)

    def test_release_is_idempotent(self):
        rb.release_lock(self.lock)   # 文件不存在也不该抛
        rb.acquire_lock(self.lock, log=lambda *a: None)
        rb.release_lock(self.lock)
        rb.release_lock(self.lock)

    def test_backup_lock_and_cleanup_lock_are_separate_files(self):
        """共用一个锁文件会让"备份时清理"互相阻塞 —— 没必要。"""
        from scripts import cleanup

        self.assertNotEqual(rb.lock_path_for(self.db, rb.BACKUP_LOCK_SUFFIX),
                            cleanup.lock_path_for(self.db))

    def test_lock_sits_next_to_the_database_not_in_tmp(self):
        """放 /tmp 的后果：systemd（PrivateTmp）与手动执行会看到**不同的锁**，
        防重入形同虚设。"""
        self.assertTrue(self.lock.startswith(os.path.abspath(os.path.dirname(self.db))))

    def test_no_pid_kill_liveness_probe(self):
        """`os.kill(pid, 0)` 在 Windows 上会**真的去终止进程** —— 明令禁止。"""
        import ast

        path = os.path.join(BACKEND_DIR, "scripts", "single_instance.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "kill":
                self.fail("single_instance.py 里出现了 .kill(...) 调用")


if __name__ == "__main__":
    unittest.main(verbosity=2)

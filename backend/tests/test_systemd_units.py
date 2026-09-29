"""T-22 / T-52 附带：systemd 单元文件的静态检查。

**开发机是 Windows，没有 systemd**，所以这些单元文件在本机无法真跑 ——
真正的验证要等阿里云 Linux 部署时做（见 `deploy/systemd/README.md`）。

但这不代表不能测：单元文件最容易出的错是**写漏关键指令**
（忘了 `Type=oneshot`、忘了 `SuccessExitStatus=3`、`Unit=` 名字打错），
这些都是纯文本层面的，可以在本机 StaticAnalysis 式地钉住。
写错 `Unit=` 的后果尤其隐蔽：timer 会静默地什么都不触发。

T-52 追加了 `backup.{service,timer}`（每日备份）。备份单元比清理单元
多两处**踩过就出事**的坑，这里专门钉住：

* `SuccessExitStatus` 必须含 **0 和 3**（3 = 已有实例在跑，不是失败）；
* 必须**声明 `ReadWritePaths`，且不能同时写 `ProtectHome=true`** ——
  旧站的项目根在 `/root` 下，`ProtectHome=true` 会让 `/root` 不可见，
  备份会以"数据库不存在"（其实是 ENOENT）失败，极难排查。
"""

import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
SYSTEMD_DIR = os.path.join(REPO_ROOT, "deploy", "systemd")
SERVICE = os.path.join(SYSTEMD_DIR, "cleanup.service")
TIMER = os.path.join(SYSTEMD_DIR, "cleanup.timer")
BACKUP_SERVICE = os.path.join(SYSTEMD_DIR, "backup.service")
BACKUP_TIMER = os.path.join(SYSTEMD_DIR, "backup.timer")
README = os.path.join(SYSTEMD_DIR, "README.md")


def directives(path):
    """解析 unit 文件为 {section: {key: [values]}}（同名键可重复）。"""
    data = {}
    section = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                data.setdefault(section, {})
                continue
            if "=" not in line or section is None:
                continue
            key, _, value = line.partition("=")
            data[section].setdefault(key.strip(), []).append(value.strip())
    return data


class ServiceUnitTests(unittest.TestCase):

    def setUp(self):
        self.data = directives(SERVICE)

    def test_type_is_oneshot(self):
        """**这是"只运行一个实例"的第二层保障**：systemd 不会在上一次
        还没结束时再启动一次。若写成 simple/默认，防重入就只剩文件锁一层。
        """
        self.assertEqual(self.data["Service"]["Type"], ["oneshot"])

    def test_execstart_points_at_cleanup_script(self):
        exec_start = self.data["Service"]["ExecStart"][0]
        self.assertIn("scripts/cleanup.py", exec_start,
                      "ExecStart 没有指向 scripts/cleanup.py")

    def test_success_exit_status_includes_skip_code(self):
        """退出码 3 = "已有另一个实例在跑，本次跳过"，**不是失败**。

        不声明它，systemd 会把跳过记成 failed，监控持续误告警。
        """
        values = self.data["Service"]["SuccessExitStatus"][0].split()
        self.assertIn("0", values)
        self.assertIn("3", values, "缺少 SuccessExitStatus=3，跳过会被记成失败")

    def test_has_finite_timeout(self):
        self.assertIn("TimeoutStartSec", self.data["Service"],
                      "缺超时会让卡死的清理进程一直占着 unit")

    def test_read_write_paths_declared(self):
        """写了 ProtectSystem=strict 就必须显式列出可写路径，否则连库都写不了。"""
        if self.data["Service"].get("ProtectSystem") == ["strict"]:
            self.assertIn("ReadWritePaths", self.data["Service"],
                          "ProtectSystem=strict 但没写 ReadWritePaths —— 清理会因只读失败")

    def test_runs_as_non_root(self):
        self.assertIn("User", self.data["Service"])
        self.assertNotEqual(self.data["Service"]["User"], ["root"])

    def test_install_target(self):
        self.assertEqual(self.data["Install"]["WantedBy"], ["multi-user.target"])


class TimerUnitTests(unittest.TestCase):

    def setUp(self):
        self.data = directives(TIMER)

    def test_on_calendar_present(self):
        self.assertIn("OnCalendar", self.data["Timer"])

    def test_calendar_is_every_15_minutes(self):
        self.assertEqual(self.data["Timer"]["OnCalendar"], ["*:0/15"])

    def test_unit_points_at_the_service(self):
        """写错 Unit= 的后果最隐蔽：timer 会静默地什么都不触发。"""
        self.assertEqual(self.data["Timer"]["Unit"], ["cleanup.service"])

    def test_unit_name_matches_the_service_file(self):
        declared = self.data["Timer"]["Unit"][0]
        self.assertTrue(os.path.exists(os.path.join(SYSTEMD_DIR, declared)),
                        "Unit=%s 指向的文件不存在" % declared)

    def test_persistent_catches_up_missed_runs(self):
        self.assertEqual(self.data["Timer"]["Persistent"], ["true"])

    def test_has_randomized_delay(self):
        self.assertIn("RandomizedDelaySec", self.data["Timer"])

    def test_install_target(self):
        self.assertEqual(self.data["Install"]["WantedBy"], ["timers.target"])

    def test_no_duplicate_directives(self):
        """同名键写了两遍时 systemd 只用最后一条 —— 静默且容易踩。"""
        for section, keys in self.data.items():
            for key, values in keys.items():
                if key in ("ReadWritePaths", "Environment", "After", "Wants"):
                    continue  # 这几个本来就允许出现多次
                self.assertEqual(len(values), 1,
                                 "[%s] %s 出现了 %d 次" % (section, key, len(values)))


class DeploymentDocTests(unittest.TestCase):

    def test_readme_exists_and_mentions_windows_manual_run(self):
        """开发机是 Windows —— 文档必须写明"本机怎么单次手动跑"。"""
        self.assertTrue(os.path.exists(README))
        with open(README, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("cleanup.py", text)
        self.assertIn("--dry-run", text)
        self.assertIn("SuccessExitStatus", text,
                      "文档应当解释退出码 3 为什么不算失败")

    def test_readme_documents_the_uncovered_table(self):
        """`token_blacklist` 没被清理是**刻意**的，必须在文档里说清楚，
        否则将来有人会以为是漏了。"""
        with open(README, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("token_blacklist", text)

    def test_readme_documents_backup_units(self):
        """T-52 的两份单元文件也必须在同一份部署文档里说清楚。"""
        with open(README, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("rotate_backup.py", text)
        self.assertIn("backup.timer", text)
        self.assertIn("恢复", text, "文档必须写明怎么恢复备份")


class BackupServiceUnitTests(unittest.TestCase):
    """T-52：每日备份单元。"""

    def setUp(self):
        self.data = directives(BACKUP_SERVICE)

    def test_type_is_oneshot(self):
        self.assertEqual(self.data["Service"]["Type"], ["oneshot"])

    def test_execstart_points_at_rotate_backup(self):
        exec_start = self.data["Service"]["ExecStart"][0]
        self.assertIn("scripts/rotate_backup.py", exec_start)
        self.assertIn("--keep 7",
                      exec_start,
                      "ADR-019 要求保留 7 份，ExecStart 里必须显式传给脚本")

    def test_success_exit_status_allows_skip(self):
        """备份脚本"已有实例在跑"时返回 3；不声明它就会被记成 failed。"""
        values = self.data["Service"]["SuccessExitStatus"][0].split()
        self.assertIn("0", values)
        self.assertIn("3", values, "缺少 SuccessExitStatus=3，跳过会被记成失败")

    def test_has_finite_timeout(self):
        self.assertIn("TimeoutStartSec", self.data["Service"],
                      "缺超时会让卡死的备份进程一直占着 unit")

    def test_runs_as_non_root(self):
        self.assertIn("User", self.data["Service"])
        self.assertNotEqual(self.data["Service"]["User"], ["root"])

    def test_backup_dir_is_writable(self):
        path = self.data["Service"]["ExecStart"][0]
        out_flag = "--out" in path
        self.assertTrue(out_flag, "ExecStart 必须显式指定 --out（备份根目录）")
        self.assertIn("ReadWritePaths", self.data["Service"],
                      "备份要写盘，必须显式声明可写路径")

    def test_protect_home_is_not_true(self):
        """**回归用例**：项目根在 `/root/ai-interview-simulator` 上。

        `ProtectHome=true` 会让 `/root` 完全不可见，于是备份脚本连
        `interview.db` 都打不开，日志只说"数据库不存在" —— 排查成本极高。
        """
        value = self.data["Service"].get("ProtectHome", [None])[0]
        self.assertNotEqual(value, "true",
                            "ProtectHome=true 会让 /root 下的项目读不到数据库")

    def test_install_target(self):
        self.assertEqual(self.data["Install"]["WantedBy"], ["multi-user.target"])


class BackupTimerUnitTests(unittest.TestCase):

    def setUp(self):
        self.data = directives(BACKUP_TIMER)

    def test_runs_once_a_day(self):
        self.assertEqual(self.data["Timer"]["OnCalendar"], ["*-*-* 03:30:00"],
                         "ADR-019 要求每日一次")

    def test_unit_points_at_the_backup_service(self):
        self.assertEqual(self.data["Timer"]["Unit"], ["backup.service"])

    def test_unit_name_matches_the_service_file(self):
        declared = self.data["Timer"]["Unit"][0]
        self.assertTrue(os.path.exists(os.path.join(SYSTEMD_DIR, declared)),
                        "Unit=%s 指向的文件不存在" % declared)

    def test_persistent_catches_up_missed_runs(self):
        """长时间停机不补跑 = 连续多天没有备份。"""
        self.assertEqual(self.data["Timer"]["Persistent"], ["true"])

    def test_has_randomized_delay(self):
        self.assertIn("RandomizedDelaySec", self.data["Timer"])

    def test_install_target(self):
        self.assertEqual(self.data["Install"]["WantedBy"], ["timers.target"])

    def test_no_duplicate_directives(self):
        for section, keys in self.data.items():
            for key, values in keys.items():
                if key in ("ReadWritePaths", "Environment", "After", "Wants"):
                    continue
                self.assertEqual(len(values), 1,
                                 "[%s] %s 出现了 %d 次" % (section, key, len(values)))

    def test_backup_and_cleanup_do_not_collide_on_the_same_minute(self):
        """两个 timer 撞在同一分钟会同时抢 IO，没必要时。"""
        cleanup = directives(TIMER)["Timer"]["OnCalendar"][0]
        backup = self.data["Timer"]["OnCalendar"][0]
        self.assertNotEqual(cleanup, backup)


if __name__ == "__main__":
    unittest.main()

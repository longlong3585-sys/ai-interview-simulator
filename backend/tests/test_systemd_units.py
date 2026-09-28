"""T-22 附带：systemd 单元文件的静态检查。

**开发机是 Windows，没有 systemd**，所以这两份单元文件在本机无法真跑 ——
真正的验证要等阿里云 Linux 部署时做（见 `deploy/systemd/README.md`）。

但这不代表不能测：单元文件最容易出的错是**写漏关键指令**
（忘了 `Type=oneshot`、忘了 `SuccessExitStatus=3`、`Unit=` 名字打错），
这些都是纯文本层面的，可以在本机 StaticAnalysis 式地钉住。
写错 `Unit=` 的后果尤其隐蔽：timer 会静默地什么都不触发。
"""

import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
SYSTEMD_DIR = os.path.join(REPO_ROOT, "deploy", "systemd")
SERVICE = os.path.join(SYSTEMD_DIR, "cleanup.service")
TIMER = os.path.join(SYSTEMD_DIR, "cleanup.timer")
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


if __name__ == "__main__":
    unittest.main()

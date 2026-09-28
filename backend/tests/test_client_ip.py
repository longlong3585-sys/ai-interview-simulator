"""T-21 测试：客户端真实 IP 解析（`X-Forwarded-For` 信任链）。

这是限流器的**唯一入口**：取错了要么限流失效（fail-open），
要么把全网锁死。因此本文件把两端都钉住。

最重要的两条：
  * **取最右，不取最左** —— 最左是客户端自己写的，可任意伪造；
  * **链长不足时退回直连对端** —— 不猜（fail-closed）。
"""

import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from utils.client_ip import (  # noqa: E402
    _reset_warning_for_tests,
    resolve_client_ip,
    trusted_proxy_count,
)


class FakeClient(object):
    def __init__(self, host):
        self.host = host


class FakeRequest(object):
    """最小可用的 Request 替身（只需要 headers 与 client.host）。"""

    def __init__(self, peer, xff=None):
        self.client = FakeClient(peer) if peer is not None else None
        self.headers = {} if xff is None else {"X-Forwarded-For": xff}


class TrustedProxyCountTests(unittest.TestCase):

    def setUp(self):
        self._old = os.environ.get("TRUSTED_PROXY_COUNT")
        _reset_warning_for_tests()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TRUSTED_PROXY_COUNT", None)
        else:
            os.environ["TRUSTED_PROXY_COUNT"] = self._old
        _reset_warning_for_tests()

    def test_default_is_one_proxy(self):
        """默认 1 = 架构文档写明的线上部署（Nginx 单层）。"""
        os.environ.pop("TRUSTED_PROXY_COUNT", None)
        self.assertEqual(trusted_proxy_count(), 1)

    def test_env_override(self):
        os.environ["TRUSTED_PROXY_COUNT"] = "2"
        self.assertEqual(trusted_proxy_count(), 2)
        os.environ["TRUSTED_PROXY_COUNT"] = "0"
        self.assertEqual(trusted_proxy_count(), 0)

    def test_garbage_falls_back_to_one(self):
        os.environ["TRUSTED_PROXY_COUNT"] = "abc"
        self.assertEqual(trusted_proxy_count(), 1)

    def test_negative_is_clamped_to_zero(self):
        os.environ["TRUSTED_PROXY_COUNT"] = "-3"
        self.assertEqual(trusted_proxy_count(), 0)


class ResolveClientIpTests(unittest.TestCase):

    def setUp(self):
        _reset_warning_for_tests()

    # ---------------- 直连（无代理）----------------

    def test_no_proxy_uses_peer_address(self):
        """TRUSTED_PROXY_COUNT=0：完全忽略 XFF，用 TCP 对端地址。"""
        req = FakeRequest("203.0.113.7", xff="9.9.9.9")  # XFF 是伪造的
        self.assertEqual(resolve_client_ip(req, trusted_count=0), "203.0.113.7")

    def test_no_proxy_and_no_peer_returns_unknown(self):
        req = FakeRequest(None)
        self.assertEqual(resolve_client_ip(req, trusted_count=0), "unknown")

    # ---------------- 单层代理（本项目线上）----------------

    def test_single_proxy_takes_rightmost(self):
        """**最核心的一条**：Nginx 追加在最后的才是真实客户端。"""
        req = FakeRequest("127.0.0.1", xff="203.0.113.7")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "203.0.113.7")

    def test_spoofed_leftmost_is_ignored(self):
        """攻击者自填最左段 —— 必须被忽略，否则换个值就换个限流桶。"""
        req = FakeRequest("127.0.0.1", xff="9.9.9.9, 203.0.113.7")
        self.assertEqual(
            resolve_client_ip(req, trusted_count=1), "203.0.113.7",
            "取了最左段（攻击者可控）—— 限流可被绕过",
        )

    def test_multiple_spoofed_prefixes_still_ignored(self):
        req = FakeRequest("127.0.0.1", xff="1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.7")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "203.0.113.7")

    def test_missing_xff_falls_back_to_peer(self):
        req = FakeRequest("127.0.0.1", xff=None)
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "127.0.0.1")

    def test_empty_xff_falls_back_to_peer(self):
        req = FakeRequest("127.0.0.1", xff="   ")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "127.0.0.1")

    # ---------------- 两层代理 ----------------

    def test_two_proxies_takes_second_from_right(self):
        """客户端 -> LB -> Nginx -> 应用：真实客户端在倒数第二段。"""
        req = FakeRequest("10.0.0.5", xff="203.0.113.7, 10.0.0.9")
        self.assertEqual(resolve_client_ip(req, trusted_count=2), "203.0.113.7")

    def test_two_proxies_ignores_spoofed_prefix(self):
        req = FakeRequest("10.0.0.5", xff="9.9.9.9, 203.0.113.7, 10.0.0.9")
        self.assertEqual(resolve_client_ip(req, trusted_count=2), "203.0.113.7")

    # ---------------- 链长不足 / 数据异常 ----------------

    def test_chain_too_short_falls_back_to_peer(self):
        """配置了 2 层但链上只有 1 段 —— 不猜，退回直连对端（fail-closed）。"""
        req = FakeRequest("10.0.0.5", xff="203.0.113.7")
        self.assertEqual(resolve_client_ip(req, trusted_count=2), "10.0.0.5")

    def test_invalid_ip_in_trusted_position_falls_back_to_peer(self):
        """受信任位置不是合法 IP（代理配错）—— 不能原样使用那个怪值。

        原样使用会给攻击者"每次换一个桶"的机会（fail-open）；
        退回直连对端虽然可能过严，但方向是对的：**限流宁可误伤，不可漏放**。
        """
        req = FakeRequest("127.0.0.1", xff="not-an-ip")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "127.0.0.1")

    def test_never_returns_empty_string(self):
        """永远不返回空串 —— 空键会让每个异常请求各拿一个新桶。"""
        for req in (FakeRequest(None, xff=""),
                    FakeRequest(None, xff="garbage"),
                    FakeRequest("", xff="")):
            for count in (0, 1, 2):
                self.assertTrue(resolve_client_ip(req, trusted_count=count),
                                "count=%d 时返回了空串" % count)

    # ---------------- 格式容忍 ----------------

    def test_whitespace_is_tolerated(self):
        req = FakeRequest("127.0.0.1", xff="  9.9.9.9 ,  203.0.113.7  ")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "203.0.113.7")

    def test_empty_segments_are_skipped(self):
        req = FakeRequest("127.0.0.1", xff="9.9.9.9, , 203.0.113.7,")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "203.0.113.7")

    def test_ipv6_is_supported(self):
        req = FakeRequest("127.0.0.1", xff="2001:db8::1")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "2001:db8::1")

    def test_ipv4_mapped_ipv6_is_supported(self):
        req = FakeRequest("127.0.0.1", xff="::ffff:203.0.113.7")
        self.assertEqual(resolve_client_ip(req, trusted_count=1), "::ffff:203.0.113.7")

    def test_missing_client_attribute_is_tolerated(self):
        """某些测试替身没有 .client —— 不能让登录 500。"""

        class Bare(object):
            headers = {"X-Forwarded-For": "203.0.113.7"}

        self.assertEqual(resolve_client_ip(Bare(), trusted_count=1), "203.0.113.7")
        self.assertEqual(resolve_client_ip(Bare(), trusted_count=0), "unknown")

    # ---------------- 分桶语义（这才是限流真正依赖的）----------------

    def test_different_clients_get_different_keys(self):
        """**本任务存在的理由**：不同用户必须落到不同桶。

        若用了 `request.client.host`（Nginx 后面就是代理 IP），
        下面三个请求会全落到同一个键上 —— 一人连错 5 次锁死全网。
        """
        keys = set()
        for client in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
            req = FakeRequest("127.0.0.1", xff=client)
            keys.add(resolve_client_ip(req, trusted_count=1))
        self.assertEqual(len(keys), 3, "不同客户端被解析成了同一个限流键：%s" % keys)
        self.assertNotIn("127.0.0.1", keys, "限流键退化成了代理 IP")

    def test_same_client_behind_proxy_is_stable(self):
        """同一客户端的多次请求必须解析出**同一个键**（否则计数永远攒不起来）。"""
        keys = {resolve_client_ip(FakeRequest("127.0.0.1", xff="203.0.113.7"),
                                  trusted_count=1)
                for _ in range(5)}
        self.assertEqual(len(keys), 1)


if __name__ == "__main__":
    unittest.main()

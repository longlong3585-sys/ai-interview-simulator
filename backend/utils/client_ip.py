"""T-21：解析客户端真实 IP（`X-Forwarded-For` 感知，**信任链长度可配**）。

## 为什么必须有这个模块

原实现（`routers/auth_router.py`）用的是：

    client_ip = request.client.host if request.client else "unknown"

ADR-006 的部署形态是 **Nginx → uvicorn**。此时 `request.client.host` 拿到的是
**Nginx 的地址**（通常是 127.0.0.1），于是**所有用户共用同一个限流键** ——
一个人连错 5 次，**全网都被锁 10 分钟**。这不是理论风险，是必现的。

## 🔴 最容易写错的地方：取 XFF 的**哪一端**

`X-Forwarded-For` 是**可被客户端伪造**的请求头。常见的 `$proxy_add_x_forwarded_for`
是**追加**语义，所以：

    客户端发:  X-Forwarded-For: 9.9.9.9      ← 攻击者自己写的
    Nginx 追加: X-Forwarded-For: 9.9.9.9, <真实客户端IP>
    应用收到:  "9.9.9.9, <真实客户端IP>"

* 取**最左**（`split(",")[0]`）→ 拿到 `9.9.9.9`，**攻击者每次换个值就换一个
  限流桶 → 限流形同虚设**（fail-open）。
* 取**最右**（本模块的做法）→ 拿到 Nginx 亲自观测到的真实客户端 IP。

所以本模块取的是 `parts[-trusted_proxy_count]`：**从右往左数第 N 个**，
N = 你实际部署的信任代理层数。最右边的那些是"我自己的代理追加的"，
左边的全是别人写的。

| 部署 | `TRUSTED_PROXY_COUNT` | 取哪个 |
|---|---|---|
| 客户端直连 uvicorn（本地开发） | `0` | 完全忽略 XFF，用 `request.client.host` |
| 客户端 → Nginx → 应用（**本项目线上**） | `1` | 最右 1 个 |
| 客户端 → LB → Nginx → 应用 | `2` | 最右 2 个 |

## 默认值与它的取舍（如实说明）

默认 `1`，因为它对应**架构文档写明的线上部署**（ADR-006：Nginx 在前）。
但这个默认值有一个已知代价：**若真的没有代理而你又没设成 0**，
XFF 就完全由客户端控制 → 取"最右 1 个"等于取攻击者写的值 → **限流可被绕过**。

因此：

* 首次解析时会打一条**一次性 WARNING**，把当前生效的信任层数讲清楚，
  让"信任了 XFF 却没人知道"这件事不可能发生；
* 本项目 `.env` 里应当显式写上 `TRUSTED_PROXY_COUNT`，与部署保持一致。

反过来，若选了过小的值（例如线上有 Nginx 却设成 0），后果是**回退到代理 IP**，
即"全网共用一个桶"—— 这属于 fail-closed（过严），比 fail-open（被绕过）安全。
所以两个方向的错误里，我们宁可错在"过严"这一侧。

## 校验失败时的处置

若被信任的那一段**不是合法 IP**（说明代理配错了），本模块**不猜**：
退回 `request.client.host` 并打 WARNING。

为什么不是"原样使用那个怪值"：那样会给攻击者一个**每次都能换桶**的机会
（fail-open）。退回直连对端虽然可能过严，但方向是对的 ——
**限流器宁可误伤，不可漏放**。
"""

import ipaddress
import logging
import os

logger = logging.getLogger("app.client_ip")

#: 允许的请求头名（大小写不敏感由框架处理）
_XFF_HEADER = "X-Forwarded-For"

#: 是否已经就"当前信任层数"提示过（避免每个请求刷日志）
_warned = {"done": False}


def trusted_proxy_count() -> int:
    """当前生效的信任代理层数（每次读取，便于测试改环境变量）。"""
    raw = os.getenv("TRUSTED_PROXY_COUNT", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "TRUSTED_PROXY_COUNT=%r 不是整数，按 1 处理（Nginx 单层）", raw
        )
        return 1
    if value < 0:
        logger.warning("TRUSTED_PROXY_COUNT=%d 为负数，按 0 处理", value)
        return 0
    return value


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _warn_once(count: int):
    if _warned["done"]:
        return
    _warned["done"] = True
    if count <= 0:
        logger.info(
            "客户端 IP 解析：TRUSTED_PROXY_COUNT=0，忽略 X-Forwarded-For，"
            "直接使用 TCP 对端地址（适用于客户端直连、前面没有反向代理）"
        )
    else:
        logger.info(
            "客户端 IP 解析：信任 X-Forwarded-For 最右 %d 段。"
            "请确认线上确实是 %d 层代理（本项目默认 Nginx 1 层）；"
            "若前面没有代理，必须把 TRUSTED_PROXY_COUNT 设为 0，"
            "否则 X-Forwarded-For 可被客户端伪造、限流会被绕过。",
            count, count,
        )


def _reset_warning_for_tests():
    """仅供测试：允许重复观察那条一次性日志。"""
    _warned["done"] = False


def resolve_client_ip(request, trusted_count=None) -> str:
    """解析客户端真实 IP。**这是限流键的唯一来源。**

    参数
    ----
    request:
        带 `.headers` 与 `.client` 的对象（FastAPI/Starlette 的 `Request`）。
    trusted_count:
        覆盖 `TRUSTED_PROXY_COUNT`（测试用）。`None` 时读环境变量。

    返回
    ----
    用于限流分桶的字符串。**永远不返回空串** —— 拿不到就退 `"unknown"`，
    这样至少所有异常请求共用同一个桶（fail-closed），而不是各拿一个新桶。
    """
    count = trusted_proxy_count() if trusted_count is None else trusted_count
    _warn_once(count)

    direct = ""
    client = getattr(request, "client", None)
    if client is not None:
        direct = getattr(client, "host", "") or ""

    if count <= 0:
        return direct or "unknown"

    headers = getattr(request, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = headers.get(_XFF_HEADER) or ""
        except Exception:  # noqa: BLE001  （headers 不是映射也不该让登录 500）
            raw = ""

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < count:
        # 链长不够：要么代理没转发，要么配置的层数不对。
        # 不猜 —— 用直连对端（fail-closed）。
        return direct or "unknown"

    candidate = parts[-count]
    if not _is_valid_ip(candidate):
        logger.warning(
            "X-Forwarded-For 中受信任的那一段不是合法 IP（%r）；"
            "退回直连对端 %r。请检查反向代理配置。", candidate, direct or "unknown",
        )
        return direct or "unknown"

    return candidate

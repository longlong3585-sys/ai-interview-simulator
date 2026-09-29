"""T-50 / ADR-016：JWT **滑动续期 + 8 小时绝对上限**的纯逻辑。

这个模块刻意**零依赖**（不 import config / database / fastapi）：它的输入是
"已经解码好的 JWT payload + 当前时间"，输出是"要不要续期 / 能不能续期 / 为什么"。
所有判断因此可以在单元测试里用**固定时钟**精确复现 —— 而"8 小时"这种规则
靠手点页面是验不出来的。

## ADR-016 的五条规则，逐条落在下面

1. **回写头名固定 `X-Refreshed-Token`**（常量 `REFRESHED_TOKEN_HEADER`）；
2. 续期触发条件：**剩余有效期 < 50%** 才回写（`RENEWAL_THRESHOLD_RATIO`）——
   否则每次请求都续期，白白放大写放大，也让"令牌寿命"变得无法解释；
3. **绝对上限**：`auth_time` 起算 **8 小时**（`ABSOLUTE_MAX_LIFETIME_SECONDS`），
   超过则拒绝续期（超期响应带 `X-Token-Expired: absolute`）；
4. `jti` 语义：**续期沿用同一 `jti`**，使 ADR-003 的黑名单能一次性吊销整条续期链；
5. 续期只换 `exp`/`iat`，**身份声明（sub/user_id/role）逐字复制** ——
   续期不是重新登录，不能在续期途中悄悄改变身份。

## 一个刻意的"保守"决定

**只续期仍然有效的令牌**（`exp > now`）。已过期的令牌**不会**因为"还在 8 小时内"
就被续期 —— 那等于让过期令牌无条件复活，`exp` 形同虚设。
代价是"挂机超过 30 分钟要重新登录"，这与 ADR-016 的动机（一次 15 分钟面试
+ 上传 + 报告不会被踢出）并不冲突。
"""

from __future__ import annotations

import time
import uuid
from typing import Optional, Tuple

# —— 回写头（ADR-016 第 1 点）——
REFRESHED_TOKEN_HEADER = "X-Refreshed-Token"
# —— 绝对上限被触发时的标记头（ADR-016 R-9 的 UX 落点）——
TOKEN_EXPIRED_HEADER = "X-Token-Expired"
TOKEN_EXPIRED_ABSOLUTE = "absolute"

# —— 绝对上限（秒）：自首次签发起可续期的总时长 ——
ABSOLUTE_MAX_LIFETIME_SECONDS = 8 * 60 * 60
# —— 触发阈值：剩余有效期 < 总寿命的 50% 才续期 ——
RENEWAL_THRESHOLD_RATIO = 0.5

# 不携带这些声明的令牌**不续期**（见模块 docstring 的保守决定 + T-51 的吊销前提）。
# 注意 `user_id` **不在**这里：它是身份声明之一（续期时若存在就逐字复制），
# 但早期/其它签发路径可能只带 `sub`（由 `get_current_user` 查库拿用户）——
# 那种令牌同样应当能续期，不能因为少一个可选声明就把它永久锁死。
REQUIRED_RENEWAL_CLAIMS = ("jti", "auth_time", "exp", "iat")

# —— 结果原因码（调用方与测试都按它分支，不靠解析中文）——
REASON_RENEWED = "renewed"
REASON_NOT_NEEDED = "not_needed"
REASON_NO_CLAIMS = "no_claims"
REASON_EXPIRED = "expired"
REASON_ABSOLUTE_LIMIT = "absolute_limit"

# 续期时**逐字复制**的声明（身份不许在续期途中被改写）
IDENTITY_CLAIMS = ("sub", "user_id", "role", "jti", "auth_time")


def origin_auth_claims(now: Optional[int] = None) -> dict:
    """首次签发（登录）时要并入 payload 的声明（ADR-016 第 3、4 点）。

    * `jti` —— 本条续期链的标识，续期时沿用（T-51 的黑名单按它吊销整条链）；
    * `auth_time` —— 绝对上限的起算点（Unix 秒，整数）。
    """
    moment = int(time.time() if now is None else now)
    return {"jti": uuid.uuid4().hex, "auth_time": moment}


def _as_int(value) -> Optional[int]:
    """把 JWT 里读出来的时间戳收成整数；bool 不算（`True` 会被 int() 悄悄变成 1）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def has_renewal_claims(payload) -> bool:
    """令牌是否携带续期所需的全部声明（缺任何一个 → 不续期）。

    `jti` 是**字符串**（uuid hex），`auth_time` / `exp` / `iat` 是**整数**时间戳 ——
    两类分开判，不要图省事一律 `int()`（那样每个令牌都会被判成"缺声明"）。
    """
    if not isinstance(payload, dict):
        return False
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti.strip():
        return False
    for claim in ("auth_time", "exp", "iat"):
        if _as_int(payload.get(claim)) is None:
            return False
    return True


def should_renew(payload, now: Optional[int] = None) -> bool:
    """是否需要续期：**仍然有效**且**剩余有效期 < 50%**。

    已过期的令牌不续期（见模块 docstring）；缺声明的令牌不续期。
    """
    if not has_renewal_claims(payload):
        return False
    moment = int(time.time() if now is None else now)
    exp = _as_int(payload.get("exp"))
    iat = _as_int(payload.get("iat"))
    if exp is None or iat is None or iat > exp:
        return False
    if exp <= moment:  # 已过期：不续期（否则 exp 形同虚设）
        return False
    remaining = exp - moment
    lifetime = exp - iat
    if lifetime <= 0:
        return False
    return remaining < lifetime * RENEWAL_THRESHOLD_RATIO


def exceeds_absolute_lifetime(payload, now: Optional[int] = None) -> bool:
    """是否超过 8 小时绝对上限。

    **缺 `auth_time` 或值非法都算超限**（fail-closed）：宁可让用户重新登录一次，
    也不要给出"没有绝对上限"的令牌链 —— 那正是 ADR-016 要堵的洞。
    """
    moment = int(time.time() if now is None else now)
    auth_time = _as_int((payload or {}).get("auth_time")) if isinstance(payload, dict) else None
    if auth_time is None:
        return True
    return moment - auth_time >= ABSOLUTE_MAX_LIFETIME_SECONDS


def renewed_claims(original, auth_time: int, ttl_seconds: int, now: Optional[int] = None) -> Tuple[dict, int]:
    """构造续期后的 payload 与新的过期时刻（ADR-016 第 4、5 点）。

    * `jti` / `auth_time` 沿用原值 —— 绝对上限**不会**因为续期而重置；
    * `sub` / `user_id` / `role` 逐字复制 —— 续期不改变身份。
    """
    moment = int(time.time() if now is None else now)
    new_exp = moment + int(ttl_seconds)
    claims = {}
    for claim in IDENTITY_CLAIMS:
        if claim in original:
            claims[claim] = original[claim]
    claims["auth_time"] = int(auth_time)
    claims["iat"] = moment
    claims["exp"] = new_exp
    return claims, new_exp


def evaluate_renewal(payload, ttl_seconds: int, now: Optional[int] = None):
    """中间件用的**总判定**：返回 `(should_issue, claims_or_None, reason)`。

    调用方（`utils/token_renewal_middleware.py`）只看这三个值：
      * `should_issue=True` → 用 `claims` 签新令牌并回写 `X-Refreshed-Token`；
      * `reason=REASON_ABSOLUTE_LIMIT` → 拒绝请求，带 `X-Token-Expired: absolute`；
      * 其余 → 原样放行。
    """
    moment = int(time.time() if now is None else now)
    exp = _as_int((payload or {}).get("exp")) if isinstance(payload, dict) else None

    # 「已过期」优先于「已超绝对上限」：过期令牌本来就该被拒绝，
    # 没必要额外给出"你的会话超时了"这种更具体的信号（那是给仍在有效期内、
    # 但已越过 8 小时硬上限的会话用的）。
    if exp is not None and exp <= moment:
        return False, None, REASON_EXPIRED

    if not has_renewal_claims(payload):
        return False, None, REASON_NO_CLAIMS

    if exceeds_absolute_lifetime(payload, moment):
        return False, None, REASON_ABSOLUTE_LIMIT

    if not should_renew(payload, moment):
        return False, None, REASON_NOT_NEEDED

    auth_time = _as_int(payload.get("auth_time"))
    claims, _ = renewed_claims(payload, auth_time, ttl_seconds, moment)
    return True, claims, REASON_RENEWED

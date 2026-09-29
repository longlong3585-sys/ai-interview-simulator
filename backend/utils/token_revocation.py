"""T-51 / ADR-003 选 B：令牌吊销的**纯逻辑**（黑名单该留多久、什么算已吊销）。

与 T-50 的 `utils/token_renewal.py` 同一手法：把"判断"剥成零依赖纯函数，
真正的存储读写交给 `services/stores/sqlite_token_blacklist_store.py`。
这样"TTL 必须 ≥ 8 小时"这条**安全约束**可以在单元测试里被钉死 ——
它靠手点页面是验不出来的（要真的等 8 小时）。

## 三条规则

1. **留多久：`now + 8h`**（`BLACKLIST_MIN_TTL_SECONDS`）。
   这是"不早于令牌自然死亡"与"不晚于绝对上限"两条约束的**唯一交集**：
   * 下界：任何合法令牌的 `exp` 都不超过 `auth_time + 8h`（T-50 的绝对上限），
     所以记录活到 `now + 8h` **一定**覆盖它的剩余寿命 —— 否则会出现
     "登出 → 等记录过期 → 复用旧令牌"，这正是 `docs/02-arch-review.md` R-4 的形态
     （初稿写 30 分钟，续期链却能活 8 小时 ⇒ 被吊销令牌 30 分钟后复活）；
   * 上界：T-50 之后没有任何令牌能活过 8 小时，留更久只会留垃圾行。
   ⇒ 验收标准里"黑名单 TTL **≥8h**"因此是**恰好 8h**，两个方向都有测试钉住。
2. **缺 `jti` 的令牌无法吊销**：它是 T-50 之前签发的老令牌。
   这种情况**不报错**（登出仍应成功、前端仍要清本地），但要明确返回
   "无法吊销"的判定，避免调用方误以为已经吊销成功。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

#: 黑名单记录的最短保留时长（秒）= T-50 的续期绝对上限。
#:
#: 刻意**与 `token_renewal.ABSOLUTE_MAX_LIFETIME_SECONDS` 取同一个数值**
#: （8 小时），并有一条测试断言两者相等 —— 只写注释不写断言的话，
#: 将来谁把上限调大/调小，这里会**静默**失效，而那正是 R-4 的复发路径。
BLACKLIST_MIN_TTL_SECONDS = 8 * 60 * 60


def _as_int(value) -> Optional[int]:
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


def blacklist_expiry(payload, now: Optional[int] = None, min_ttl_seconds: int = BLACKLIST_MIN_TTL_SECONDS) -> str:
    """算出这条吊销记录该留到什么时候（UTC ISO-8601 字符串）= **`now` + 8 小时**。

    为什么与令牌自己的 `exp` 无关（也不再取 `max`）：
      * 令牌最长只能活到 `auth_time + 8h`（T-50），因此 `now + 8h` **一定**覆盖
        它的剩余寿命 —— 这就是"≥8h"这条验收标准的来源；
      * 反过来，留得比 8h 更久没有任何意义（没有令牌能活那么长），
        只会让表里堆垃圾行。因此取**恰好** 8h。

    令牌的 `exp` 参数仍在签名里，因为调用方传的就是解码后的 payload；
    这里只把它用于**不变量自检**（见 `blacklist_covers_token`），不参与时长计算。
    """
    moment = int(time.time() if now is None else now)
    target = moment + int(min_ttl_seconds)
    return datetime.utcfromtimestamp(target).isoformat()


def revocation_target(payload) -> Optional[str]:
    """该令牌能否被吊销：返回 `jti`（非空字符串）或 `None`。

    `None` 表示"这枚令牌没有 `jti`，无法吊销"（T-50 之前签发的老令牌）。
    调用方据此决定是否需要提示"请重新登录以完成登出"，而不是假装吊销成功。
    """
    if not isinstance(payload, dict):
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str):
        return None
    jti = jti.strip()
    return jti or None


def is_revocable(payload) -> bool:
    """该令牌是否可吊销（有 `jti`）。"""
    return revocation_target(payload) is not None


def blacklist_covers_token(expires_at_iso: str, payload, now: Optional[int] = None) -> bool:
    """不变量自检：该吊销记录是否覆盖令牌的完整剩余寿命，且不超过 8 小时。

    对 T-50 之后签发的令牌，这条**必然**成立（令牌活不过 8 小时）；
    它真正的用处是**在测试里钉住这一点** —— 一旦有人改动 TTL 或绝对上限，
    这里会立刻红，而不是等到线上出现"被吊销令牌复活"。
    """
    if not isinstance(expires_at_iso, str) or not expires_at_iso:
        return False
    moment = int(time.time() if now is None else now)
    try:
        stored = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return False
    stored_ts = int(stored.timestamp()) if stored.tzinfo is not None \
        else int((stored - datetime(1970, 1, 1)).total_seconds())
    exp = _as_int((payload or {}).get("exp")) if isinstance(payload, dict) else None
    if exp is not None and stored_ts < exp:
        return False
    return stored_ts <= moment + BLACKLIST_MIN_TTL_SECONDS


def iso_now(now: Optional[int] = None) -> str:
    """当前 UTC 时间的 ISO 串（与存储层时间契约同格式、同实现口径）。"""
    return datetime.utcfromtimestamp(int(time.time() if now is None else now)).isoformat()


def iso_after_seconds(seconds: float, now=None) -> str:
    """`now` 之后 `seconds` 秒的 ISO 串（负值用于构造"已过期"的测试数据）。

    `now` 可以是 Unix 秒（int/float，默认取当前时间），也可以是**ISO 串** ——
    与 `services/stores/base.iso_after(seconds, base)` 的参数形态一致。
    两种都收是有意的：本模块与存储层的时间工具经常在同一个表达式里混用
    （比如"以刚才算出的那个时刻为基准再往后 8 小时"），
    如果这里只收 int，调用方就会撞上 `int('2023-…')` 这种看不清来源的报错。
    """
    if isinstance(now, str):
        base = datetime.fromisoformat(now)
    else:
        base = datetime.utcfromtimestamp(int(time.time() if now is None else now))
    return (base + timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# 黑名单读写（供 `auth.get_current_user`、登出端点与续期中间件共用）
#
# 装配点仍是 `services.stores.factory` —— 本模块不直接 import 任何契约实现，
# 只持有 factory 返回的**协议对象**（与 T-20/T-21 的调用方式一致）。
# ---------------------------------------------------------------------------

_store = None


def get_blacklist_store():
    """黑名单存储（进程内单例；由 `TOKEN_BLACKLIST_STORE_BACKEND` 选择实现）。"""
    global _store
    if _store is None:
        from services.stores.factory import get_token_blacklist_store
        _store = get_token_blacklist_store()
    return _store


def reset_blacklist_store(store=None):
    """仅供测试：替换/清掉缓存（`store=None` 表示下次重新装配）。"""
    global _store
    _store = store


def revoke_token(payload, now: Optional[int] = None) -> Optional[str]:
    """把该令牌的 `jti` 写进黑名单，返回写入的过期时刻（UTC ISO 串）。

    令牌没有 `jti`（T-50 之前签发的老令牌）时返回 `None` —— **不抛错**：
    登出必须成功（前端要清本地状态），只是这枚令牌无法在服务端被吊销。
    调用方据返回值决定要不要提示"请重新登录以完成登出"。
    """
    jti = revocation_target(payload)
    if jti is None:
        return None
    expires_at = blacklist_expiry(payload, now)
    get_blacklist_store().revoke(jti, expires_at)
    return expires_at


def is_token_revoked(payload, now: Optional[int] = None) -> bool:
    """该令牌是否已被吊销（仍在黑名单有效期内）。

    没有 `jti` 的令牌**不可能**在黑名单里（无法被吊销），直接返回 `False`，
    省掉一次数据库查询 —— 这也是老令牌不被误伤的原因。
    """
    jti = revocation_target(payload)
    if jti is None:
        return False
    return bool(get_blacklist_store().is_revoked(jti, iso_now(now)))

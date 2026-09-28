"""T-10 / FR-6.4：JSON 文本列的**容错解析**单一来源。

背景：
  `interview_records.report` / `messages` 是 TEXT 列（存 JSON 字符串）。
  修复前 `user.py` 的历史列表与详情直接写 `json.loads(r.report)`：
    - `report` 为 NULL 时 → `TypeError` → 整个列表接口 **500**
    - 存量脏数据（非法 JSON）同样会 500，且**一条坏记录会拖垮整个列表**
  同一文件里 `get_user_stats()` 是包了 try/except 的，口径并不统一 ——
  这正是本任务要收敛的问题。

现在的约定：任何从数据库读 JSON 文本列的地方，一律走 `safe_json_loads()`。
"""

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def safe_json_loads(text: Optional[str], default: Any = None) -> Any:
    """容错地把 JSON 文本解析为对象。

    - `None` / 空串 → 返回 `default`（不抛异常）
    - 已是 dict/list（SQLAlchemy JSON 列或上游已解析）→ 原样返回
    - 非法 JSON → 记录告警并返回 `default`，**不让单条脏数据 500 整个接口**
    """
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, (str, bytes, bytearray)):
        logger.warning("safe_json_loads 收到非文本输入：%r", type(text))
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        preview = text[:120] if isinstance(text, str) else repr(text[:120])
        logger.warning("JSON 解析失败，已回退为默认值：%s | 内容片段=%r", exc, preview)
        return default

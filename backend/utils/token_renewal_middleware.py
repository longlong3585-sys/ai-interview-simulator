"""T-50 / ADR-016：滑动续期中间件。

它挂在 **ASGI 层**而不是逐个端点：续期必须对所有受保护请求生效，
漏掉任何一个端点都会表现为"某些接口永远不续期"——那是极难发现、且
只会在长时间面试中偶发的问题。

分工：
  * 本文件只负责"从请求里取 Bearer 令牌 → 交给纯逻辑判定 → 落响应头"；
  * 所有规则（50% 阈值、8 小时上限、缺声明不续期）都在
    `utils/token_renewal.py` 里，可被单元测试用固定时钟覆盖。

公开端点（登录/注册/验证码/题库）直接跳过：它们本来就不带令牌。
"""

from __future__ import annotations

import logging

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config import ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, SECRET_KEY
from utils.token_renewal import (
    REFRESHED_TOKEN_HEADER,
    REASON_ABSOLUTE_LIMIT,
    TOKEN_EXPIRED_ABSOLUTE,
    TOKEN_EXPIRED_HEADER,
    evaluate_renewal,
)
from utils.token_revocation import is_token_revoked

logger = logging.getLogger("app")

# 与前端 `services/authResponse.ts` 的 PUBLIC_API_PATH_PREFIXES 一一对应。
# 两边都列一遍是刻意的：后端不能信任前端"不带令牌"的约定，
# 而前端也不能把"后端不续期"当成"可以不登录"。
#
# T-51：`/api/logout` 也在这里，但它**不是公开端点** —— 它需要令牌才知道吊销谁。
# 放进这个元组的理由是"**跳过黑名单与续期**"，而不是"跳过鉴权"：
#   * 登出必须**幂等**：拿着已被吊销的令牌再点一次"退出"也得 200，
#     否则前端会把它当失败（而用户只是想确保自己登出了）；
#   * 登出**不允许续期**：给一个正在登出的会话发新令牌毫无意义。
PUBLIC_PATH_PREFIXES = (
    "/api/captcha",
    "/api/login",
    "/api/register",
    "/api/logout",
    "/api/question_bank",
)

_BEARER_PREFIX = "bearer "

#: T-51：令牌被吊销（真登出）时的标记头。与 `X-Token-Expired` 分开是刻意的 ——
#: 两者的前端语义不同：吊销 = 这枚令牌作废（前端已登出，本地状态本就没有了）；
#: 绝对超时 = 会话仍在服务端（要保留进度、只提示重新登录）。
TOKEN_REVOKED_HEADER = "X-Token-Revoked"


def is_public_path(path: str) -> bool:
    if not isinstance(path, str):
        return False
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PATH_PREFIXES)


def extract_bearer_token(authorization: str | None) -> str | None:
    """从 `Authorization` 头里取令牌；不是 Bearer 方案则返回 None。"""
    if not isinstance(authorization, str):
        return None
    value = authorization.strip()
    if len(value) <= len(_BEARER_PREFIX) or value[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = value[len(_BEARER_PREFIX):].strip()
    return token or None


class TokenRenewalMiddleware(BaseHTTPMiddleware):
    """滑动续期 + 绝对上限。

    三个出口（顺序即语义）：

      1. **已超 8 小时绝对上限** → 401 + `X-Token-Expired: absolute`，
         且**不带** `WWW-Authenticate`（沿用应用其它 401 的形态）。
         前端据此提示"重新登录"但**保留本地会话**（进度在服务端，可恢复）。
      2. 剩余有效期 < 50% → 正常放行，响应加 `X-Refreshed-Token: <新令牌>`。
      3. 其余（不需要续期 / 缺声明 / 令牌无效）→ 原样放行，由各端点自己鉴权。
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if is_public_path(path) or request.method == "OPTIONS":
            return await call_next(request)

        token = extract_bearer_token(request.headers.get("authorization"))
        if not token:
            return await call_next(request)

        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        except JWTError:
            # 无效令牌不是本中间件的职责：交给端点 → 统一 401。
            return await call_next(request)

        # T-51：**已吊销的令牌不得续期**（否则"登出"只挡住下一个请求，
        # 却顺手发了一枚新的、仍然有效的令牌回去 —— 吊销被自己绕过）。
        # 这里直接 401；`get_current_user` 里还有一层同样判定，
        # 两处都查不是冗余：中间件可以被绕过（直接调依赖），依赖不会。
        if is_token_revoked(payload):
            logger.info("拒绝请求：令牌已被吊销（sub=%s jti=%s）",
                        payload.get("sub"), payload.get("jti"))
            return JSONResponse(
                status_code=401,
                content={"detail": "登录已失效，请重新登录"},
                headers={TOKEN_REVOKED_HEADER: "revoked"},
            )

        should_issue, claims, reason = evaluate_renewal(
            payload, ttl_seconds=ACCESS_TOKEN_EXPIRE_MINUTES * 60
        )

        if reason == REASON_ABSOLUTE_LIMIT:
            logger.info(
                "拒绝续期：已超过绝对上限（sub=%s jti=%s auth_time=%s）",
                payload.get("sub"),
                payload.get("jti"),
                payload.get("auth_time"),
            )
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "登录已超过最长会话时长，请重新登录（面试进度已保存在服务端，登录后可恢复）",
                    "token_expired": TOKEN_EXPIRED_ABSOLUTE,
                },
                headers={TOKEN_EXPIRED_HEADER: TOKEN_EXPIRED_ABSOLUTE},
            )

        response = await call_next(request)

        if should_issue and claims and response.status_code < 400:
            response.headers[REFRESHED_TOKEN_HEADER] = jwt.encode(
                claims, SECRET_KEY, algorithm=ALGORITHM
            )

        return response

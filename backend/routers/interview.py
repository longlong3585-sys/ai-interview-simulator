import io
import json
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import JSONResponse
from PyPDF2 import PdfReader
from docx import Document
from sqlalchemy.orm import Session

from auth import get_db, get_current_user, get_current_admin_user, require_user
from models.schemas import ChatRequest, SaveInterviewRequest
from services.stores.base import (
    ActiveSessionExists,
    EndedReason,
    SessionDraft,
    SessionSnapshot,
    StoreError,
    TurnCommit,
    iso_after,
    utcnow_iso,
)
from services.stores.factory import get_session_store
from utils.ai_helpers import client, extract_json_from_response, generate_questions, clean_resume_text
from utils.safe_json import safe_json_loads
from database import User, InterviewRecord
from config import MAX_FILE_SIZE, INTERVIEW_DURATION_SECONDS

router = APIRouter(prefix="/api", tags=["interview"])

logger = logging.getLogger("app.interview")

# T-23 / ADR-004：**会话键统一**。
#
# 修复前这里是 `interview_sessions: Dict[int, Dict] = {}` —— 一个进程内字典，
# 以 `user_id` 为键。三个问题：
#   1. **静默覆盖**：同一用户再次 start 会直接覆盖上一场面试，没有任何提示；
#   2. **多 worker 各存一份**：ADR-006 的部署是 `--workers 2`，
#      请求落到另一个 worker 就"没有会话"，刷新即丢失进度（Bug 2 的根因之一）；
#   3. **重启即清空**。
# 现在统一为持久化的 `SessionStore`：会话以 **UUID `session_id`** 为键，
# `user_id` 只用来"找该用户当前活跃的会话"；冲突由
# `UNIQUE(user_id) WHERE status='active'` 部分唯一索引变成**显式 409**。
#
# 注意：**前端契约本次不变** —— 路由仍按"当前登录用户的活跃会话"定位会话，
# 因此前端不需要传 session_id。`GET /api/interview/session` 与客户端显式持有
# session_id 属于 T-24/T-25。

#: 会话 TTL（架构 §6.3：2 小时）
#
# ⚠️ 与 `INTERVIEW_DURATION_SECONDS`（config.py）**不是一回事**（T-28）：
# 这里是"这行数据还值不值得当成活跃会话"的锁卫生上限；
# 那里是"这场面试还允许答题吗"的业务规则（15 分钟）。
SESSION_TTL_SECONDS = 2 * 60 * 60

UPLOAD_DIR = "uploads/avatars"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _active_session(user_id: int) -> Optional[SessionSnapshot]:
    """取该用户**当前活跃**（active 且未过期）的会话；没有则 `None`。

    惰性判定：`expires_at <= now` 的行即便 `status` 仍是 `active`，
    `get_active` 也不会返回它 —— 口径统一在存储层，路由不重复判断。
    """
    return get_session_store().get_active(user_id, utcnow_iso())


# ===========================================================================
# T-28 / FR-4.12：**超时兜底** —— 服务端自己掌握超时时刻
# ===========================================================================
#
# 要修的 Bug（`docs/05-issues-backlog.md` Bug 3B）："15 分钟归零"原先只活在
# 前端的一个倒计时里。前端锁定当然要做（T-42），但**锁定拦不住手工请求**：
# 超时之后只要再发一次 `POST /api/chat`，服务端照样把这一轮记进会话
# （会话仍 active），用户于是"超时了也能一直答下去"，业务规则形同虚设。
#
# 修法：把死线搬到服务端 —— 死线 = 会话行的 `created_at`（服务端写入，
# 客户端无法伪造）+ `INTERVIEW_DURATION_SECONDS`。任何一次碰到该会话的请求
# 都先做一次惰性判定（术语与 T-22 清理、T-25 自愈一致）：
#
#     到点且仍 active  ->  store.abandon(reason='timeout')  ->  释放唯一锁
#
# 为什么用惰性判定而不是只靠定时清理：
#   * 定时器最小粒度是分钟级，用户点下去的那一瞬间必须**立即**被拦住；
#   * 定时清理（`abandon_all_expired`）管的是 2 小时 TTL，与 15 分钟业务
#     规则无关 —— 靠它兜底意味着超时后还能再答 1 小时 45 分钟。
#
# ⚠️ 为什么必须覆盖**每一个**会话入口（chat / skip / abandon / session /
# report / start）：只拦住 chat，用户就能用 `generate_report` 把一场本该
# 超时的面试当成"正常完成"（`ended_reason=completed`，状态 finished）。
# 报告口径（T-27）与锁释放（T-25）都建立在"结束原因由服务端裁定"之上。
#
# 关于"不得把用户锁死在门外"（ADR-022R）：兜底**只置终态、不删行**，
# 唯一锁随状态一起释放，所以下一行请求就能开新面试。


def _enforce_interview_timeout(user_id: int,
                               now: Optional[str] = None) -> Optional[SessionSnapshot]:
    """超时兜底：到点就把该用户的活跃会话置 `abandoned`（`ended_reason=timeout`）。

    返回
    ----
    `SessionSnapshot`
        这一行**刚刚**（或由并发请求同时）因超时被结束 —— 调用方按超时处理
        （写路径返回 409，读路径照常返回状态）。
    `None`
        没有超时会话，或活跃行是被**别的理由**结束的（用户点了放弃、
        报告已正常生成）。此时交回调用方走各自的正常分支，不抢别人的语义。

    并发：两个请求同时到点，只有一个 `abandon` 能成功（乐观锁），另一个
    `applied=False` —— 那**不是**错误，锁已经释放、目的已达成，故只记日志。
    """
    now = now or utcnow_iso()
    store = get_session_store()
    snapshot = store.get_active(user_id, now)
    if snapshot is None:
        return None
    if not snapshot.is_timed_out(now, INTERVIEW_DURATION_SECONDS):
        return None

    result = store.abandon(snapshot.session_id, snapshot.version,
                           EndedReason.TIMEOUT, now)
    if result.applied:
        logger.info("超时兜底：会话 %s 已置 abandoned（ended_reason=timeout，"
                    "user_id=%s）", snapshot.session_id, user_id)
        return result.snapshot or snapshot

    # 没写成有两种可能：① 并发请求刚改了 version（仍是 active）；
    # ② 同一瞬间用户点了放弃 / 报告落库把它置成了终态。
    # ① 用最新 version 重试一次（载荷只有"置终态"，重试是安全的，与
    # `/interview/abandon` 同一策略）；② 则不越权改写别人的结束原因。
    latest = result.snapshot or store.get(snapshot.session_id) or snapshot
    if latest.is_active:
        retry = store.abandon(latest.session_id, latest.version,
                              EndedReason.TIMEOUT, now)
        if retry.applied:
            logger.info("超时兜底（重试成功）：会话 %s 已置 abandoned",
                        latest.session_id)
            return retry.snapshot or latest
        latest = retry.snapshot or store.get(latest.session_id) or latest

    if latest.ended_reason == EndedReason.TIMEOUT:
        # 并发请求也在做同一件事 —— 语义一致，照常按超时处理。
        return latest
    logger.info("超时兜底跳过：会话 %s 已被以 %s 结束",
                latest.session_id, latest.ended_reason)
    return None


def _ended_session_payload(snapshot: SessionSnapshot) -> dict:
    """"刚刚结束的那一场"的摘要（T-28 附加下发）。

    用途：超时兜底之后会话不再活跃（`GET /api/interview/session` 的
    `session` 为 null），前端却必须能给出 FR-4.12 要求的**明确反馈**
    （"因超时已自动结束"）并引导用户去出报告 —— 没有这个字段，前端只能
    显示"你没有任何面试"，用户会以为进度丢了。

    刻意不含 `report` 正文（可能很大）：只给 `has_report`，报告本身走
    `POST /api/generate_report` 取。
    """
    return {
        "session_id": snapshot.session_id,
        "status": snapshot.status,
        "ended_reason": snapshot.ended_reason,
        "current_index": snapshot.current_index,
        "total": len(snapshot.questions),
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "has_report": bool(snapshot.report),
    }


def _no_session_response() -> JSONResponse:
    """T-23：**没有进行中的会话 -> 409 + 指引**（不再静默降级）。

    修复前 `/api/chat` 在没有会话时会**直接走通用 AI 对话分支** ——
    任何登录用户只要不 start_interview，就能把该接口当免费的 DeepSeek 代理用
    （Bug 1 里"白嫖额度"的另一条路径）。现在必须 409。

    ⚠️ `detail` 保持**字符串**：前端是 `data.detail || '默认文案'` 直接渲染的，
    换成对象会变成 React 的 "Objects are not valid as a React child"。
    机器可读的信息另放 `code` / `hint` / `actions` 字段。
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": "当前没有进行中的面试会话，请先开始面试",
            "code": "no_active_session",
            "hint": "请先调用 POST /api/start_interview 开始一场面试。"
                    "如果你刚才在面试中（例如刷新了页面），当前版本需要重新开始。",
            "actions": ["start_interview"],
        },
    )


def _session_conflict_response(existing=None) -> JSONResponse:
    """已有活跃会话时的 409，**直接携带会话摘要**（T-25 / ADR-022 R-10）。

    为什么摘要必须放进 409 本体：前端收到 409 时要立刻能给出
    "**继续上次面试 / 放弃并重新开始**"两个选项。若摘要要另外发一次
    `GET /api/interview/session` 才能拿到，用户就会先看到一段"你已有面试"
    的空窗，再等一次往返 —— ADR-022 R-10 明确要求"使前端无需额外一次往返
    即可恢复"，说的就是这件事。

    `actions` 列出可用的后续动作（T-24 之后这两个接口都已存在）。
    """
    content = {
        "detail": "你已有进行中的面试",
        "code": "active_session_exists",
        "hint": "同一时间只能有一场进行中的面试。"
                "你可以继续这一场，或放弃它再重新开始。",
        "actions": ["get_session", "abandon"],
    }
    if existing is not None:
        # 复用读接口的摘要形状，前端一套代码就能渲染两个入口
        content["session"] = _session_payload(existing)
    return JSONResponse(status_code=409, content=content)


def _timeout_response(snapshot: SessionSnapshot) -> JSONResponse:
    """超时兜底后的 **409**（T-28 / FR-4.12）。

    语义要点：
      * `code` 与"没有活跃会话"（`no_active_session`）**必须区分开**：
        前者是"时间到了"，后者是"你根本没在面试"。前端要给出的提示
        完全不同（FR-4.12 第 ③ 条："明确反馈'因超时已自动结束'"）。
      * `ended_reason=timeout` 一并下发，让前端不必猜自己是被哪种方式结束的；
      * `actions` 给出**两条出路**：去出报告（T-27 的口径）、开新面试
        （ADR-022R：超时不得把用户锁死在门外）；
      * `detail` 保持**字符串** —— 前端是 `detail || 默认文案` 直接渲染的。
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": "本场面试已超时自动结束，请开始一场新的面试",
            "code": "interview_timeout",
            "ended_reason": EndedReason.TIMEOUT,
            "hint": "面试时长上限为 %d 分钟；到点后由**服务端**自动结束会话，"
                    "因此超时后无法再继续答题。已答部分仍可生成报告"
                    "（按超时口径评分，未及作答的题不扣分），"
                    "也可以立刻重新开始一场新的面试。"
                    % max(1, INTERVIEW_DURATION_SECONDS // 60),
            "actions": ["generate_report", "start_interview"],
            "session": _session_payload(snapshot),
        },
    )


def _version_conflict_response(latest: Optional[SessionSnapshot]) -> JSONResponse:
    """ADR-004 第 4 步：乐观锁冲突 -> 409，并带上**最新进度**供前端恢复。"""
    return JSONResponse(
        status_code=409,
        content={
            "detail": "面试状态已被其它请求更新，请刷新后重试",
            "code": "version_conflict",
            "hint": "同一场面试的并发写入只允许一个成功。"
                    "请按返回的 current_index 重新同步进度后重发本条消息。",
            "actions": ["refresh"],
            "current_index": latest.current_index if latest else None,
            "last_seq": latest.last_seq if latest else None,
            "total": len(latest.questions) if latest else None,
        },
    )


def _stamp_server_facts(result: dict, ended_reason: str, counts: dict) -> None:
    """把**只有服务端才知道的事实**写进报告，覆盖模型输出（T-27）。

    为什么必须覆盖，而不是"在提示词里请模型照抄"：
      * `ended_reason` 模型根本无从知道 —— 它看不到会话是怎么结束的；
      * `answered_count` / `total_questions` 是会话上的客观事实。让模型自己数，
        就会出现"报告说答了 3 题、会话里只有 2 题"这种自相矛盾的展示，
        而这正是 FR-4.5 要消除的"误导"。

    另外，ADR-007R 的"超时且零作答时不得当作全错"**不能只靠提示词**：
    模型偶尔不遵守，就会原样复现"误导性 0 分"。既然这是一条 MUST，
    就该由服务端兜底保证 —— 这里做的是**补标注**，不是改分数
    （分数只有模型能给，服务端不越权编造）。

    条件比 ADR 原文**更窄**一处：ADR 写"`ended_reason='timeout'` 且
    `answered_count=0`"，这里额外要求 `skipped=0`。因为"全部主动跳过"
    也是 answered_count=0，但那是候选人**明确拒绝回答**，给它贴上
    "未及作答，无法评分"就成了反向误导（把放弃说成没机会）。
    """
    result["ended_reason"] = ended_reason
    result["answered_count"] = counts["answered"]
    result["total_questions"] = counts["total"]

    if ended_reason == EndedReason.TIMEOUT and not counts["answered"] \
            and not counts["skipped"]:
        marker = "未及作答，无法评分"
        already = "%s %s" % (result.get("details") or "",
                             result.get("suggestion") or "")
        if marker not in already:
            result["details"] = (
                "%s：本次面试因超时自动结束，在收到任何回答之前就已结束，"
                "以下各分项分数**不代表**对候选人的负面评价。\n%s"
                % (marker, result.get("details") or "")
            ).rstrip()


def _persist_report(snapshot: Optional[SessionSnapshot], report) -> None:
    """把评分结果落库。两条路径，取决于会话现在是死是活（T-27）。

    * 会话仍是 `active` → `finish()`：置 `finished` + 写报告（**同事务**，
      ADR-004 报告路径）。这一步同时释放唯一锁，不做的话用户会被自己的
      会话锁到 TTL 结束（2 小时）。
    * 会话已是 `abandoned`（超时 / 用户放弃）→ `attach_report()`：
      **只写报告，不动状态**。ADR-022R 裁决"超时 → `abandoned`，不是
      `finished`（面试并未正常完成）"，报告不能反过来污染状态语义。

    两条路径都在乐观锁冲突时**重试一次**（用最新 version），确保不会留下
    "评了分却没落库"。与 `/api/chat` 的"绝不重试写"不矛盾：chat 的载荷是
    基于旧快照算出的 `user_answers` 整列，重放会**串题**；而报告只取决于
    刚生成的评分结果，不随 version 变化。

    **失败不抛出**：报告已经生成好了，不能因为落库失败就让用户拿不到结果。
    """
    if snapshot is None:
        return
    store = get_session_store()
    payload = json.dumps(report, ensure_ascii=False)
    try:
        if snapshot.is_active:
            result = store.finish(snapshot.session_id, snapshot.version, payload,
                                  EndedReason.COMPLETED, utcnow_iso())
            if not result.applied:
                latest = store.get(snapshot.session_id)
                if latest is not None and latest.is_active:
                    store.finish(latest.session_id, latest.version, payload,
                                 EndedReason.COMPLETED, utcnow_iso())
        else:
            result = store.attach_report(snapshot.session_id, snapshot.version,
                                         payload, utcnow_iso())
            if not result.applied:
                latest = store.get(snapshot.session_id)
                if latest is not None and not latest.is_active \
                        and not latest.report:
                    store.attach_report(latest.session_id, latest.version,
                                        payload, utcnow_iso())
    except StoreError as exc:
        logger.warning("报告落库/置终态失败（会话 %s）：%s",
                       snapshot.session_id, exc)


# T-13 / FR-4.10：跳过词的**单一来源**。
#
# 修复前前端 App.tsx 与服务端各硬编码一份完全相同的列表 —— 一旦有人只改一侧，
# 用户"打跳过词"的行为在前端本地判定与后端判定之间就会分歧，且不会有任何报错。
# 现在以本常量为唯一来源，经 GET /api/interview/config 下发给前端
# （跨进程无法共享常量，故选下发而非导入）。
SKIP_WORDS = [
    "不会", "忘记了", "不知道", "不了解", "没学过", "没接触过", "没经验",
    "太难", "换一个", "换个", "换道", "简单的", "跳过去", "跳过吧", "略过",
    "答不上", "答不出来", "搞不定", "想不起来", "没做过", "换个简单", "下一题", "跳过",
]


def _is_skip_message(user_message: str) -> bool:
    lower_msg = user_message.strip().lower()
    return any(word in lower_msg for word in SKIP_WORDS)


@router.get("/interview/config")
def get_interview_config(current_user: User = Depends(get_current_user)):
    """T-13 / FR-4.10：向前端下发面试相关常量，避免前后端各存一份。

    需要登录（不在 §3.1 的公开白名单内）—— 这些常量只服务于面试流程，
    而面试流程本身就必须登录。

    T-28：新增 `duration_seconds`。前端的 15 分钟倒计时原本硬编码在
    `App.tsx`（`setTimeLeft(15 * 60)`），服务端改了时长它也不知道 ——
    两边一旦不一致，就会出现"前端已经锁 UI，服务端说还能答"这类
    互相矛盾的表现。**服务端是唯一来源**，前端只负责展示。
    """
    return {
        "skip_words": list(SKIP_WORDS),
        "duration_seconds": INTERVIEW_DURATION_SECONDS,
    }


@router.post("/chat")
async def chat(req: ChatRequest, current_user: User = Depends(require_user)):
    user_message = req.message.strip()
    is_skip = _is_skip_message(user_message)

    # T-04 / FR-2.4（Bug 1）：身份**一律取自令牌**，绝不再读请求体。
    # 修复前该接口无任何鉴权依赖，且用客户端自报的 user_id 取会话，
    # 导致未登录即可调用（白嫖 AI 额度）并读写他人的面试会话（串号）。
    # T-23：会话改为从持久化存储按 user_id 解析成 **session_id**。
    # T-28 / FR-4.12：先做超时兜底（服务端自己掌握超时时刻）。
    # 顺序很重要 —— 必须先兜底再取活跃会话：兜底把会话置终态后
    # `_active_session` 就返回 None，若顺序反过来会误报"你没有在面试"，
    # 用户看到的提示就从"因超时已自动结束"退化成"请先开始面试"。
    timed_out = _enforce_interview_timeout(current_user.id)
    if timed_out is not None:
        return _timeout_response(timed_out)

    snapshot = _active_session(current_user.id)
    if snapshot is None:
        # 修复前这里会掉进下面的"通用 AI 对话"分支 ——
        # 即任何登录用户不 start_interview 也能白嫖 AI 额度。现在必须 409。
        return _no_session_response()

    return await _chat_with_session(snapshot, user_message, is_skip)


async def _chat_with_session(snapshot, user_message: str, is_skip: bool):
    """ADR-004 的时序：**AI 调用在事务外，短事务只负责落库**。

        1. 取快照（上面已完成）
        2. 【事务外】调用 DeepSeek            <- 25~30s，不持任何写锁
        3. 短事务 + 乐观锁写回；冲突返回 409，**绝不重试写**

    修复前是"读内存字典 -> 改字典 -> 调 AI"，没有事务概念；
    唯一的并发风险来自多 worker 各持一份字典，表现为**串题**。
    """
    idx = snapshot.current_index
    questions = snapshot.questions
    statuses = list(snapshot.question_status)
    answers = list(snapshot.user_answers)
    total = len(questions)

    feedback = ""

    if idx < total and not is_skip:
        statuses[idx] = "answered"
        answers[idx] = user_message
        try:
            eval_prompt = f'''你是一名严格的技术面试官。用户刚回答了问题：「{questions[idx]}」
用户回答：{user_message[:800]}
请用简短的一句话给出正面反馈（如"回答得不错"或指出明显缺陷），然后直接问下一个问题。
不要额外解释，不要带编号。'''
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": "你是严格的面试官，给出简短反馈后直接问下一个问题。"}, {"role": "user", "content": eval_prompt}],
                temperature=0.7, timeout=25,
            )
            feedback = resp.choices[0].message.content
        except Exception:
            feedback = ""
        idx += 1
    elif is_skip and idx < total:
        statuses[idx] = "skipped"
        answers[idx] = "[跳过] " + user_message
        feedback = "好的，这个方向我们先跳过。"
        idx += 1
    else:
        idx += 1

    # ---- 短事务：基于快照的**绝对期望值**写回（不是 current_index + 1）----
    result = get_session_store().commit_turn(TurnCommit(
        session_id=snapshot.session_id,
        expected_version=snapshot.version,
        current_index=idx,
        question_status=statuses,
        user_answers=answers,
        # 客户端尚未传 seq（该契约在 T-26 前后引入），沿用原值 ——
        # 不伪造假序号，否则幂等判定会错误地去重。
        last_seq=snapshot.last_seq,
        last_reply=None,
        updated_at=utcnow_iso(),
        now=utcnow_iso(),
    ))

    if not result.applied:
        # ADR-004 第 4 步：**不重试写**，把最新状态交给前端去 409。
        # 用陈旧载荷覆盖会静默串题（答案落到错误题目上）。
        latest = result.snapshot or _active_session(snapshot.user_id)
        return _version_conflict_response(latest)

    if idx >= total:
        # 注意：**不**把 status 置为 finished —— 按 ADR-004，那是
        # "报告生成成功"时的事。此处只是"题目问完了"，会话仍需保持 active
        # 以便 generate_report 带 version 守卫写入报告。
        return {"reply": (feedback + " " if feedback else "") +
                         "我们的面试到此结束，感谢你的参与！", "finished": True}

    next_q = questions[idx]
    reply = (feedback + "\n\n" + next_q) if feedback else next_q
    return {"reply": reply, "current_index": idx, "finished": False}



@router.post("/start_interview")
async def start_interview(
    current_user: User = Depends(require_user),
    role: str = Form("后端开发"),
    resume_text: str = Form(""),
    questions_json: str = Form("[]")
):
    # T-10 / FR-6.4：此处原先直接 json.loads(questions_json)，
    # 客户端传非 JSON 会让整个请求 500。改为容错解析并返回可读的 400。
    questions_list = safe_json_loads(questions_json, default=None)
    if questions_list is None:
        raise HTTPException(status_code=400, detail="questions_json 不是合法的 JSON")
    if not isinstance(questions_list, list):
        raise HTTPException(status_code=400, detail="questions_json 必须是 JSON 数组")
    if not questions_list:
        questions_list = generate_questions(resume_text)

    now = utcnow_iso()

    # ------------------------------------------------------------------
    # T-25 / ADR-022 R-10：**插入前先自愈过期行**。
    #
    # 惰性判定有个时序缺口：一行可能 `expires_at` 已过、`status` 却仍是
    # `active` —— 它**仍占着** `UNIQUE(user_id) WHERE status='active'` 索引。
    # 于是会出现自相矛盾的一幕：
    #     POST /api/start_interview -> 409（"你已有进行中的面试"）
    #     GET  /api/interview/session -> null（"你没有在面试"）
    # 用户看到的是"说我有一场面试，又说我没有任何面试"，无从下手。
    #
    # 修法：在插入**之前**把该用户所有已过期的 active 行置为 abandoned
    # （`ended_reason=timeout`），锁自然释放，插入就能成功。
    # ------------------------------------------------------------------
    store = get_session_store()
    healed = store.abandon_expired_for_user(current_user.id, now)
    if healed:
        logger.info("start_interview 自愈了 %d 条过期会话（user_id=%s）",
                    healed, current_user.id)

    # T-28 / FR-4.12：业务死线（15 分钟）同样要在插入前释放，
    # 否则"超时后想重开"的用户会撞上 409 —— 那正是 ADR-022R 明令禁止的
    # "把用户锁死在门外"。（`abandon_expired_for_user` 只管 2 小时 TTL，
    # 不覆盖 15 分钟的业务规则。）
    timed_out = _enforce_interview_timeout(current_user.id, now)
    if timed_out is not None:
        logger.info("start_interview 前释放了超时会话 %s（user_id=%s）",
                    timed_out.session_id, current_user.id)

    draft = SessionDraft(
        session_id=uuid.uuid4().hex,
        user_id=current_user.id,
        role=role,
        questions=questions_list,
        question_status=["pending"] * len(questions_list),
        user_answers=[None] * len(questions_list),
        current_index=0,
        created_at=now,
        updated_at=now,
        expires_at=iso_after(SESSION_TTL_SECONDS, now),
    )
    try:
        snapshot = store.create(draft)
    except ActiveSessionExists:
        # 到这里说明**确实**有一场未过期的活跃面试（过期行上面已经自愈了）。
        # T-25：409 直接带上会话摘要，前端无需额外往返即可展示
        # "继续上次面试 / 放弃并重新开始"（ADR-022 R-10）。
        return _session_conflict_response(store.get_active(current_user.id, utcnow_iso()))
    except StoreError as exc:
        raise HTTPException(status_code=500, detail="创建面试会话失败：%s" % exc)

    greeting = f"你好！我已分析了你的简历，准备了 {len(questions_list)} 个面试问题。让我们从第一个问题开始吧。\n\n{questions_list[0]}"

    return {
        "success": True,
        "greeting": greeting,
        "questions": questions_list,
        "current_index": 0,
        "total": len(questions_list),
        # 新增字段（向后兼容）：T-24/T-25 之后客户端会显式持有它并回传。
        "session_id": snapshot.session_id,
    }


@router.post("/skip_question")
async def skip_question(current_user: User = Depends(require_user)):
    user_id = current_user.id

    # T-28：跳过也是"写入会话"的一条路径，同样必须被超时兜底拦住。
    timed_out = _enforce_interview_timeout(user_id)
    if timed_out is not None:
        return _timeout_response(timed_out)

    snapshot = _active_session(current_user.id)
    if snapshot is None:
        # 修复前这里返回 400 —— 但"没有进行中的面试"是**状态冲突**而非参数错误，
        # 且 400 不带指引，前端只能弹一句"没有活跃的面试会话"。
        return _no_session_response()

    idx = snapshot.current_index
    questions = snapshot.questions
    total = len(questions)
    if idx >= total:
        return {"reply": "所有问题已结束，请点击结束面试生成报告。", "finished": True}

    statuses = list(snapshot.question_status)
    answers = list(snapshot.user_answers)
    statuses[idx] = "skipped"
    answers[idx] = "[跳过]"
    idx += 1

    result = get_session_store().commit_turn(TurnCommit(
        session_id=snapshot.session_id,
        expected_version=snapshot.version,
        current_index=idx,
        question_status=statuses,
        user_answers=answers,
        last_seq=snapshot.last_seq,
        last_reply=None,
        updated_at=utcnow_iso(),
        now=utcnow_iso(),
    ))
    if not result.applied:
        return _version_conflict_response(
            result.snapshot or _active_session(snapshot.user_id))

    if idx >= total:
        return {"reply": "已跳过。我们的面试到此结束，感谢你的参与！",
                "finished": True, "current_index": idx}
    return {"reply": "好的，这个方向我们跳过。\n\n" + questions[idx],
            "finished": False, "current_index": idx}


def _session_payload(snapshot: SessionSnapshot) -> dict:
    """把快照转成前端可用的会话摘要（T-24）。

    刻意**不返回** `report`（可能很大，且是评分结果，不该在"读进度"时下发）；
    也不返回 `user_id` —— 会话归属由令牌决定，下发它只会造成"可指定他人"的误导。
    """
    import datetime
    remaining = None
    try:
        exp = datetime.datetime.fromisoformat(snapshot.expires_at)
        remaining = max(0, int((exp - datetime.datetime.utcnow()).total_seconds()))
    except (ValueError, TypeError):
        pass

    # T-28：把**业务死线**也下发（与上面的 2 小时 TTL 区分开）。
    # 前端 T-42 的倒计时应当以这个时刻为准，而不是在前端硬编码 15 分钟 ——
    # 否则服务端改了时长，前端还按老时长锁 UI，两边会各说各话。
    deadline_at = snapshot.interview_deadline(INTERVIEW_DURATION_SECONDS)
    interview_remaining = None
    if deadline_at is not None:
        try:
            dl = datetime.datetime.fromisoformat(deadline_at)
            interview_remaining = max(
                0, int((dl - datetime.datetime.utcnow()).total_seconds()))
        except (ValueError, TypeError):
            pass

    return {
        "session_id": snapshot.session_id,
        "role": snapshot.role,
        "questions": snapshot.questions,
        "question_status": snapshot.question_status,
        "user_answers": snapshot.user_answers,
        "current_index": snapshot.current_index,
        "last_seq": snapshot.last_seq,
        "total": len(snapshot.questions),
        "status": snapshot.status,
        "version": snapshot.version,
        "created_at": snapshot.created_at,
        "expires_at": snapshot.expires_at,
        "remaining_seconds": remaining,
        "duration_seconds": INTERVIEW_DURATION_SECONDS,
        "deadline_at": deadline_at,
        "interview_remaining_seconds": interview_remaining,
    }


@router.get("/interview/session")
def get_interview_session(current_user: User = Depends(require_user)):
    """T-24 / Bug 2：返回**当前活跃会话**；没有则 `session: null`。

    为什么必须有这个接口：单靠"把会话落库"并不能修好"刷新即报废" ——
    前端刷新后手里没有任何会话标识，必须有一个"问服务端我现在有没有在面试"
    的入口。架构 v2.1 终审的结论：**仅持久化不足以修复，
    必须配合会话读取接口**（ADR-022 把它列为 Bug 2 的必需组成部分）。

    **无会话时返回 200 + null，而不是 404/409** —— 这是正常的业务状态
    （用户就是没在面试），不是错误。前端据此决定显示"开始面试"还是
    "继续上次面试"。

    T-28 补两件事：
      1. 读之前先做**超时兜底** —— 刷新页面本身就是"意识到超时"的最早时机，
         让 GET 自愈可以把"前端被挂起/断网，倒计时没跑到点"这一类的超时
         也在用户回来时立刻结算，而不是拖到下一次写请求；
      2. 附加下发 `last_ended`（最近结束的一场）—— 超时兜底后 `session`
         必然是 null，前端若无从知道"刚刚那场是因超时结束的"，就只能显示
         "你没有任何面试"，用户会以为进度丢了（FR-4.12 第 ③ 条要的是
         **明确反馈**，不是沉默）。
    """
    _enforce_interview_timeout(current_user.id)
    snapshot = _active_session(current_user.id)
    ended = get_session_store().get_last_ended(current_user.id)
    return {
        "session": _session_payload(snapshot) if snapshot else None,
        "last_ended": _ended_session_payload(ended) if ended else None,
    }


@router.post("/interview/abandon")
def abandon_interview_session(current_user: User = Depends(require_user)):
    """T-24 / ADR-022R：把当前活跃会话置为 `abandoned`（**释放唯一锁**）。

    这是"用户永远能开新面试"的兜底出口：浏览器崩溃 / 换设备 / 断网后用户
    回不来，若没有这个接口就得等 2 小时 TTL 才能重开 —— ADR-022 明确要求
    提供它，否则等于把用户锁在门外。

    语义要点：
      * 置 `abandoned` 而**不是删除** —— 会话内容要留给后续的历史/报告；
      * `ended_reason=manual`（与超时的 `timeout` 区分开）；
      * 没有活跃会话时返回 **409 + 指引**（与 T-23 的口径一致）；
      * 乐观锁冲突时**重试一次**：用户点"放弃"的意图很明确，不该因为并发
        被无声拒绝（重试是安全的 —— 载荷只有"置终态"，不含任何可能陈旧的内容）。
    """
    # T-28 / FR-4.12：先做超时兜底。用户在半分钟前就已经到点了（只是前端
    # 还在等他点），这次点击的**真实原因**是超时，不是"主动放弃" ——
    # `ended_reason` 必须如实写成 `timeout`（评分口径 T-27 就依赖它）。
    # 兜底成功后用户的意图（"结束这场、让我重开"）已经达成，故回 200 而不是
    # 409：他不是做了错事，只是慢了一步。
    timed_out = _enforce_interview_timeout(current_user.id)
    if timed_out is not None:
        return {
            "success": True,
            "abandoned_session_id": timed_out.session_id,
            "status": timed_out.status,
            "ended_reason": timed_out.ended_reason or EndedReason.TIMEOUT,
            "hint": "本场面试已因超时自动结束（与主动放弃等效：锁已释放），"
                    "你现在可以立刻开始一场新的面试。",
        }

    snapshot = _active_session(current_user.id)
    if snapshot is None:
        return _no_session_response()

    store = get_session_store()
    result = store.abandon(snapshot.session_id, snapshot.version,
                           EndedReason.MANUAL, utcnow_iso())
    if not result.applied:
        latest = store.get(snapshot.session_id)
        if latest is not None and latest.is_active:
            result = store.abandon(latest.session_id, latest.version,
                                   EndedReason.MANUAL, utcnow_iso())
        if not result.applied:
            return _version_conflict_response(latest or snapshot)

    return {
        "success": True,
        "abandoned_session_id": snapshot.session_id,
        "status": "abandoned",
        "ended_reason": "manual",
        "hint": "已放弃这场面试，你现在可以重新开始一场新的面试。",
    }


def _build_transcript(snapshot: SessionSnapshot):
    """从**服务端会话**重建面试对话记录（T-26）。

    返回 `(transcript, question_count)`。

    修复前这里直接用 `req.messages` —— 也就是**前端传来的聊天记录**。问题：
      1. 前端可以任意伪造/删减对话，直接影响评分结果（可自证清白）；
      2. Bug 3A：超时自动结束时前端状态可能已经乱了，传上来的 messages
         与真实作答对不上，报告与进度脱节；
      3. `generate_report` 因此无法在"服务端自己知道发生了什么"的前提下工作，
         而 ADR-007R 的"区分未及作答 / 答不上"必须建立在这个前提上。

    现在以会话为唯一事实来源：`questions` 是出题清单，`user_answers` 是
    候选人实际提交的回答，`question_status` 标明每题是 answered/skipped/pending。
    重建出来的形态与原先前端传来的那份基本一致（面试官问 -> 候选人答），
    但**不再受客户端影响**。

    注意：这里**不**把 AI 每轮给出的短评拼进去 —— 那些评语不参与打分，
    会话里也没有单独存（`last_reply` 只保留最后一条）。评分真正需要的是
    "题目 + 候选人回答 + 回答状态"，这三样会话全都有。
    """
    lines = []
    for i, question in enumerate(snapshot.questions):
        status = (snapshot.question_status[i]
                  if i < len(snapshot.question_status) else "pending")
        answer = (snapshot.user_answers[i]
                  if i < len(snapshot.user_answers) else None)
        lines.append("assistant: %s" % question)
        if status == "skipped":
            lines.append("user: [跳过此题]")
        elif status == "pending" or not answer:
            # 未作答：不编造内容，如实标注 —— ADR-007R 要求区分
            # "未及作答"与"答不上"，此处只如实呈现事实（具体评分口径属 T-27）。
            lines.append("user: [尚未作答]")
        else:
            lines.append("user: %s" % answer)
    return "\n".join(lines), len(snapshot.questions)


def _answer_counts(snapshot: SessionSnapshot):
    """按**服务端会话**统计各状态的题数（T-27）。

    这三个数是要显示给用户、并写进报告的**事实**，不能交给模型去数
    （T-26 的教训：凡是服务端已经知道的事实，就不要让模型猜）。
    缺失的状态一律按 `pending` 处理 —— 与 `_build_transcript` 同口径。
    """
    counts = {"answered": 0, "skipped": 0, "pending": 0}
    total = len(snapshot.questions)
    for i in range(total):
        st = (snapshot.question_status[i]
              if i < len(snapshot.question_status) else "pending")
        counts[st if st in counts else "pending"] += 1
    counts["total"] = total
    return counts


def _ended_reason_note(ended_reason: str) -> str:
    """把"这场面试是怎么结束的"翻译成评分口径（ADR-007R）。"""
    if ended_reason == EndedReason.TIMEOUT:
        return (
            "面试**因超时自动结束**，报告只能基于**已答部分**评分。\n"
            "存在未及作答的题目时绝不因此扣分 —— 那是时间到了，不是候选人不会。"
        )
    if ended_reason == EndedReason.MANUAL:
        return (
            "面试被候选人**主动放弃**（点击了「放弃并重开」）。\n"
            "报告基于放弃之前已经答过的部分评分，未及作答的题目同样不扣分。"
        )
    return "面试**正常结束**（题目已问完或候选人主动结束），按常规口径评分。"


def _report_target(user_id: int):
    """决定"给哪一场面试出报告、以及它的结束原因"（T-26 + T-27）。

    返回 `(snapshot, ended_reason)`；没有可评的对象时返回 `None`。

    1. **有进行中的会话** → 评这一场，`ended_reason = completed`；
    2. **否则取最近结束的一场**（且还没出过报告）→ 用库里那一行真实的
       `ended_reason`。

    第 2 条是 T-27 补的，也是"超时不出报告"在路由层的根因：
    ADR-007R 要求"超时仍要出报告"，而超时后会话已经是 `abandoned`，
    `get_active()` 永远取不到它 —— 修复前这里只能返回 409，
    用户超时后就再也拿不到任何评估结果。

    `ended_reason` **一律以库里那一行为准**，绝不接受客户端自报：
    否则前端只要声称"我超时了"，就能拿到一份按"未及作答"口径打的宽松分数。
    """
    snapshot = _active_session(user_id)
    if snapshot is not None:
        return snapshot, EndedReason.COMPLETED

    last = get_session_store().get_last_ended(user_id)
    if last is None or last.report:
        # 没有结束过的会话，或那一场已经出过报告 —— 都算"没有可评的对象"。
        # 后者顺带挡住重复计费：重发不会让模型再算一遍。
        return None
    return last, (last.ended_reason or EndedReason.MANUAL)


@router.post("/generate_report")
async def generate_report(current_user: User = Depends(require_user)):
    """T-26 / Bug 3A + T-27 / FR-4.5：评分**以服务端会话为准**，且超时也出报告。

    契约变更（T-26）：请求体从 `{"messages": [...]}` 变成**空体**
    （甚至可以不传 body）。前端不再有机会影响评分输入。

    T-27 在此基础上补齐两件事：
      1. **超时会话也能出报告** —— 超时后会话是 `abandoned`，
         `_report_target` 会回退到"最近结束且还没出报告"的一场
         （ADR-007R：超时仍要出报告）。修复前这种情况直接 409，
         用户超时后就再也拿不到任何评估结果。
      2. **评分口径区分"未及作答"与"答不上"** —— `pending` 的题是
         面试结束/超时导致的未及作答，**不得计入扣分**；`skipped`
         才是主动放弃，照常计分。零作答超时不得当作"全错给全 0"。

    `ended_reason` 与两个计数由**服务端**写入，不采信模型输出（也不接受客户端自报）。

    T-28 补一层：**兜底必须在判定 `ended_reason` 之前跑**。否则到点后还没被
    兜底的那一瞬间来出报告，`_report_target` 会看到一行仍 `active` 的会话，
    把这场超时的面试判成 `completed`（状态还写成 finished）—— 正是 T-27
    要消除的"超时说成正常完成"。兜底先把它置成 `abandoned + timeout`，
    报告路径才会走 `attach_report`（状态保持 abandoned，口径按超时）。
    """
    _enforce_interview_timeout(current_user.id)
    target = _report_target(current_user.id)
    if target is None:
        # 与 /api/chat 同口径：没有可评的面试就没有报告可评。
        # 修复前这里能凭空拿前端传来的 messages 生成一份"报告"，
        # 是又一条白嫖 AI 的路径。
        return _no_session_response()

    snapshot, ended_reason = target
    counts = _answer_counts(snapshot)
    transcript, question_count = _build_transcript(snapshot)
    slist = []
    for i, q in enumerate(snapshot.questions):
        st = (snapshot.question_status[i]
              if i < len(snapshot.question_status) else "pending")
        slist.append(f"  Q{i+1}: {q} -> {st}")
    question_status_note = ("\n## 五、各题状态（由系统给出，与下方对话记录一一对应）：\n"
                            + "\n".join(slist))

    prompt = f"""你是一位极其严格的技术面试评估专家。请根据以下面试对话，对候选人进行冷酷、真实的评估。

## 一、本场面试的结束方式（先读这一节，它决定评分口径）
ended_reason = {ended_reason}
{_ended_reason_note(ended_reason)}

## 二、三种题目状态的处理方式**完全不同**（本次评分的核心规则）
对话记录里每道题的作答都带状态，请严格区分：
- [尚未作答]（status=pending）：候选人**从未有机会回答**这道题。
  它属于「**未及作答**」，**不是**「答不上」，**不得计入扣分**，
  更**不得**被当作答错、不会、或敷衍。
  请把它当作"这场面试里没有发生过的题"：它不进入分母，也不拉低任何分项。
- [跳过此题]（status=skipped）：候选人**主动**放弃该题（点了跳过，或说了
  "不会/不知道/没学过"）。这属于「**答不上**」，**照常计分** ——
  这正是它与 [尚未作答] 的唯一、但关键的区别。
- 有正常作答的题（status=answered）：照常计分。

## 三、评分标准（重要，请严格执行）：

### 给全 0 分的**唯一**条件（口径比以往更窄，必须按新口径执行）：
- 只有当"**真正问过**的题"（= answered + skipped）**全部**属于无效回答
  （我不会/不知道/没学过/没接触过/跳过/敷衍）时，才可以给全 0 分。
- ⚠️ 只要存在 [尚未作答] 的题，就**不得**因为"没答满"而清零，也**不得**
  把 [尚未作答] 当作无效回答去凑满"全部无效"这个条件。
- 如果候选人回复高度简短且无实质内容（如只用"是的""嗯""对"敷衍），也视为无效回答。

### 一道题都没答上就结束了（answered=0 且 skipped=0）：
- 此时**没有任何可评估的作答内容**，这是「**未及作答，无法评分**」，
  不是「候选人全错」。**不得**输出"全 0 分"式的结论。
- 必须在 details 中明确写出「未及作答，无法评分」，并说明面试在收到
  任何回答之前就已结束。
- 各分数字段仍需给出数值，但它**不代表**对候选人的负面评价。

### 有效回答率与分数对照（分母 = 真正问过的题 = answered + skipped，**不含** [尚未作答]）：
- 回答了 1 个问题且质量勉强及格 -> 总分 3-4 分
- 回答了一半问题，有缺陷 -> 总分 4-5 分
- 回答了大部分问题但存在明显错误 -> 总分 5-6 分
- 所有问题都回答了但深度不够 -> 总分 6-7 分
- 回答质量高且有深度见解 -> 总分 8-9 分
- 回答精辟、有深度、展现了专家级理解 -> 总分 9-10 分（极少给出）

### 分项评分细则：
- expression_score：表达是否清晰有条理，回答是敷衍还是认真阐述（走神语无伦次=1-3，一般=4-6，清晰=7-9）
- technical_score：技术回答的准确性、深度（错误百出=1-3，有缺陷=4-5，基本正确=6-7，深度到位=8-9）
- logic_score：逻辑思维是否清晰，能否结构化地分析问题（混乱=1-3，一般=4-6，严谨=7-9）
{question_status_note}

## 六、本场统计（由系统给出，请直接采用，不要自己重新数）：
- 总题数 total_questions = {counts['total']}
- 正常作答 answered = {counts['answered']}
- 主动跳过 skipped = {counts['skipped']}（属于"答不上"，照常计分）
- 未及作答 pending = {counts['pending']}（**不得计入扣分**）

## 七、面试对话记录（共{question_count}轮提问）：
{transcript}

## 输出格式（严格 JSON，不要其他内容）：
`ended_reason` 由系统写入，**你不要输出该字段**；`answered_count` 必须等于
上面的 answered = {counts['answered']}，不要自行改动。
{{
    "expression_score": 整数(0-10),
    "technical_score": 整数(0-10),
    "logic_score": 整数(0-10),
    "overall_score": 保留一位小数(0.0-10.0),
    "answered_count": {counts['answered']},
    "total_questions": {counts['total']},
    "suggestion": "具体可操作的改进建议（50字以上，如指出哪些知识点需要补强）",
    "details": "简要总结优点和不足"
}}"""
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一位极其严格的技术面试评估专家。评分必须冷酷真实，不合格就是不合格。严格按照 JSON 格式输出。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )
        result_text = response.choices[0].message.content
        result = extract_json_from_response(result_text)
        _stamp_server_facts(result, ended_reason, counts)

        # T-23：原先是 `interview_sessions.pop(user_id)` —— 用"删掉会话"来
        # 释放"同一用户只能有一场进行中面试"的锁。持久化之后语义是
        # **置为 finished**（ADR-004 报告路径：报告落库与置终态同事务）。
        # 若此处不置终态，用户会在每场面试后被锁到 TTL 结束（2 小时）——
        # 那是把 ADR-022R 明确要消除的问题重新引入。
        #
        # T-27：超时会话走 `attach_report` —— 报告落库但**状态保持 abandoned**
        # （ADR-022R 裁决"超时不是 finished"）。
        _persist_report(snapshot, result)
        return result
    except Exception as e:
        # 降级结果同样带上服务端事实：前端要靠 `ended_reason` 决定是否
        # 显示"因超时自动结束"的提示，缺了它会把超时面试显示成正常完成。
        degraded = {
            "expression_score": 0,
            "technical_score": 0,
            "logic_score": 0,
            "overall_score": 0.0,
            "answered_count": counts["answered"],
            "total_questions": counts["total"],
            "suggestion": f"评估服务异常：{str(e)}，请联系管理员。",
            "details": "评分服务暂时不可用，本次面试未生成有效评估。"
        }
        _stamp_server_facts(degraded, ended_reason, counts)
        return degraded


@router.post("/save_interview")
def save_interview(
    req: SaveInterviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user)
):
    record = InterviewRecord(
        user_id=current_user.id,
        role=req.role,
        messages=json.dumps(req.messages),
        report=json.dumps(req.report),
        status="pending"
    )
    db.add(record)
    db.commit()
    return {"msg": "保存成功"}


@router.post("/resume/upload")
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    # T-05 / FR-2.4（Bug 1）：此前该接口无任何鉴权依赖，未登录即可上传文件
    # 触发 PDF/DOCX 解析，构成资源消耗攻击面。
    #
    # 依赖选型（见 02-architecture.md §4.2）：这里挂 `get_current_user` 而非
    # `require_user` —— 后者会对 admin 返回 403（"管理员不能进行面试"），
    # 而"上传简历"本身不应以角色为由拒绝，故只用登录态这一层。
    allowed_mime = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    if file.content_type not in allowed_mime:
        raise HTTPException(status_code=400, detail="仅支持 PDF 或 DOCX 格式")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="文件大小不能超过 5MB")

    text = ""
    if file.filename.lower().endswith('.pdf'):
        try:
            reader = PdfReader(io.BytesIO(contents))
            if reader.is_encrypted:
                raise HTTPException(status_code=400, detail="PDF 文件已加密，无法解析")
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="PDF 解析失败，可能是加密或扫描件")
    elif file.filename.lower().endswith('.docx'):
        try:
            doc = Document(io.BytesIO(contents))
            for para in doc.paragraphs:
                text += para.text + "\n"
        except Exception:
            raise HTTPException(status_code=400, detail="DOCX 解析失败，文件可能损坏")
    else:
        raise HTTPException(status_code=400, detail="仅支持 PDF 或 DOCX 格式")

    if not text.strip():
        raise HTTPException(status_code=400, detail="无法提取文本内容，请确保文件不是图片或扫描件")

    text = clean_resume_text(text)

    preview = (text[:500] + "...") if len(text) > 500 else text
    questions = generate_questions(text)

    return {
        "success": True,
        "filename": file.filename,
        "preview": preview,
        "questions": questions,
        "full_text": text
    }

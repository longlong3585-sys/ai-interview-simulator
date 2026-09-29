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
from models.schemas import ChatRequest, ReportRequest, SaveInterviewRequest
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
from config import MAX_FILE_SIZE

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
SESSION_TTL_SECONDS = 2 * 60 * 60

UPLOAD_DIR = "uploads/avatars"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _active_session(user_id: int) -> Optional[SessionSnapshot]:
    """取该用户**当前活跃**（active 且未过期）的会话；没有则 `None`。

    惰性判定：`expires_at <= now` 的行即便 `status` 仍是 `active`，
    `get_active` 也不会返回它 —— 口径统一在存储层，路由不重复判断。
    """
    return get_session_store().get_active(user_id, utcnow_iso())


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


def _finish_session(snapshot: Optional[SessionSnapshot], report) -> None:
    """评分成功后把会话置为 `finished` 并落库报告（ADR-004 报告路径）。

    乐观锁冲突时**重试一次**（用最新 version）。这里的重试是安全的，
    与 `/api/chat` 的"绝不重试"不矛盾：chat 的载荷是基于旧快照算出的
    `user_answers` 整列，重放会**串题**；而报告只取决于刚生成的评分结果，
    不随 version 变化。重试的目的是确保**锁一定被释放** ——
    否则用户会被自己的会话锁到 TTL 结束。

    **失败不抛出**：报告已经生成好了，不能因为落库失败就让用户拿不到结果。
    """
    if snapshot is None:
        return
    store = get_session_store()
    payload = json.dumps(report, ensure_ascii=False)
    try:
        result = store.finish(snapshot.session_id, snapshot.version, payload,
                              EndedReason.COMPLETED, utcnow_iso())
        if not result.applied:
            latest = store.get(snapshot.session_id)
            if latest is not None and latest.is_active:
                store.finish(latest.session_id, latest.version, payload,
                             EndedReason.COMPLETED, utcnow_iso())
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
    """
    return {
        "skip_words": list(SKIP_WORDS),
    }


@router.post("/chat")
async def chat(req: ChatRequest, current_user: User = Depends(require_user)):
    user_message = req.message.strip()
    is_skip = _is_skip_message(user_message)

    # T-04 / FR-2.4（Bug 1）：身份**一律取自令牌**，绝不再读请求体。
    # 修复前该接口无任何鉴权依赖，且用客户端自报的 user_id 取会话，
    # 导致未登录即可调用（白嫖 AI 额度）并读写他人的面试会话（串号）。
    # T-23：会话改为从持久化存储按 user_id 解析成 **session_id**。
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
    """
    snapshot = _active_session(current_user.id)
    return {"session": _session_payload(snapshot) if snapshot else None}


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


@router.post("/generate_report")
async def generate_report(req: ReportRequest, current_user: User = Depends(require_user)):
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in req.messages])
    user_msgs = [m['content'] for m in req.messages if m['role'] == 'user']
    ai_msgs = [m['content'] for m in req.messages if m['role'] == 'assistant']
    question_count = len(ai_msgs)

    # T-23：会话状态改从持久化存储读（原先读进程内字典）。
    # 注意：**评分输入仍以前端传来的 messages 为准** ——
    # "改为以服务端会话为准"是 T-26 的契约变更，不在本任务范围。
    snapshot = _active_session(current_user.id)
    question_status_note = ""
    if snapshot:
        slist = []
        for i, q in enumerate(snapshot.questions):
            st = snapshot.question_status[i]
            slist.append(f"  Q{i+1}: {q} -> {st}")
        question_status_note = "\n## 各问题回答状态：\n" + "\n".join(slist) + "\n（answered=已回答, skipped=跳过, pending=未答）请参考这些状态调整评分。"

    prompt = f"""你是一位极其严格的技术面试评估专家。请根据以下面试对话，对候选人进行冷酷、真实的评估。

## 评分标准（重要，请严格执行）：

### 无法回答问题（得 0 分）：
- 如果候选人对你提出的问题一个都没有给出有效回答（全是我不会/不知道/没学过/没接触过/跳过等），则所有分数均为 0。
- 如果候选人回复高度简短且无实质内容（如只用"是的""嗯""对"敷衍），也视为无效回答。

### 有效回答率与分数对照：
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

## 面试对话记录（共{question_count}轮提问）：
{transcript}

## 输出格式（严格 JSON，不要其他内容）：
{{
    "expression_score": 整数(0-10),
    "technical_score": 整数(0-10),
    "logic_score": 整数(0-10),
    "overall_score": 保留一位小数(0.0-10.0),
    "answered_count": 有效回答的问题数量,
    "total_questions": {question_count},
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

        # T-23：原先是 `interview_sessions.pop(user_id)` —— 用"删掉会话"来
        # 释放"同一用户只能有一场进行中面试"的锁。持久化之后语义是
        # **置为 finished**（ADR-004 报告路径：报告落库与置终态同事务）。
        # 若此处不置终态，用户会在每场面试后被锁到 TTL 结束（2 小时）——
        # 那是把 ADR-022R 明确要消除的问题重新引入。
        _finish_session(snapshot, result)
        return result
    except Exception as e:
        return {
            "expression_score": 0,
            "technical_score": 0,
            "logic_score": 0,
            "overall_score": 0.0,
            "answered_count": 0,
            "total_questions": question_count,
            "suggestion": f"评估服务异常：{str(e)}，请联系管理员。",
            "details": "评分服务暂时不可用，本次面试未生成有效评估。"
        }


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

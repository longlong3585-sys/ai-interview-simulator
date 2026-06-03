import io
import json
import os
from typing import Dict

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from PyPDF2 import PdfReader
from docx import Document
from sqlalchemy.orm import Session

from auth import get_db, get_current_admin_user, require_user
from models.schemas import ChatRequest, ReportRequest, SaveInterviewRequest
from utils.ai_helpers import client, extract_json_from_response, generate_questions, clean_resume_text
from database import User, InterviewRecord
from config import MAX_FILE_SIZE

router = APIRouter(prefix="/api", tags=["interview"])

interview_sessions: Dict[int, Dict] = {}

UPLOAD_DIR = "uploads/avatars"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _is_skip_message(user_message: str) -> bool:
    lower_msg = user_message.strip().lower()
    skip_words = ["不会", "忘记了", "不知道", "不了解", "没学过", "没接触过", "没经验",
                  "太难", "换一个", "换个", "换道", "简单的", "跳过去", "跳过吧", "略过",
                  "答不上", "答不出来", "搞不定", "想不起来", "没做过", "换个简单", "下一题", "跳过"]
    return any(word in lower_msg for word in skip_words)


@router.post("/chat")
async def chat(req: ChatRequest):
    user_message = req.message.strip()
    lower_msg = user_message.lower()
    is_skip = _is_skip_message(user_message)

    user_id = req.user_id
    session = interview_sessions.get(user_id) if user_id else None

    if user_id and session:
        idx = session["current_index"]
        questions = session["questions"]
        statuses = session["question_status"]
        if idx < len(questions) and not is_skip:
            statuses[idx] = "answered"
            session["user_answers"][idx] = user_message
            try:
                eval_prompt = f"""你是一名严格的技术面试官。用户刚回答了问题：「{questions[idx]}」
用户回答：{user_message[:800]}
请用简短的一句话给出正面反馈（如"回答得不错"或指出明显缺陷），然后直接问下一个问题。
不要额外解释，不要带编号。"""
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "system", "content": "你是严格的面试官，给出简短反馈后直接问下一个问题。"}, {"role": "user", "content": eval_prompt}],
                    temperature=0.7, timeout=25,
                )
                feedback = resp.choices[0].message.content
            except Exception:
                feedback = ""
            idx += 1
        elif is_skip and idx < len(questions):
            statuses[idx] = "skipped"
            session["user_answers"][idx] = "[跳过] " + user_message
            feedback = "好的，这个方向我们先跳过。"
            idx += 1
        else:
            feedback = ""
            idx += 1

        session["current_index"] = idx
        if idx >= len(questions):
            session["finished"] = True
            return {"reply": (feedback + " " if feedback else "") + "我们的面试到此结束，感谢你的参与！", "finished": True}

        if not feedback:
            next_q = questions[idx]
            reply = next_q
        else:
            next_q = questions[idx]
            reply = feedback + "\n\n" + next_q
        return {"reply": reply, "current_index": idx, "finished": False}

    is_asking_interviewer = (
        (user_message.rstrip('？').endswith('?') or user_message.rstrip('吗').endswith('吗'))
        and any(word in lower_msg for word in ["你", "面试官", "请问", "能告诉我", "什么是", "怎么理解", "自我介绍", "不用", "需要", "可以先", "能不能", "可不可以"])
    )

    system_prompt = f"你是一名严格的{req.role}岗位面试官。"

    if is_asking_interviewer:
        system_prompt += " 候选人向你提出了一个问题或请求（比如要求先做自我介绍等）。你必须优先直接回应他的请求，自然地允许他表述，然后再继续提问。绝对不要忽略候选人的问题而直接抛出新问题。"

    system_prompt += " 如果你的上一轮提问涉及某个技术点，候选人给出了有效回答，应针对该回答进行追问，每次只问一个问题。"

    if is_skip:
        system_prompt += " 面试者刚才表示不会回答当前问题或请求换题。请你用一句话表示已记录（如'好的，这个方向我们先跳过'），然后紧接着立刻提出一个全新的、与刚才题目方向完全不同的技术问题。不要在确认语之后等待回复，直接给出下一个问题。每次只问一个问题。"

    system_prompt += " 注意：你最多只能问 10 个问题。当你感觉已经获取足够信息或问完第 10 个问题后，请用\"我们的面试到此结束，感谢你的参与！\"作为回复的结尾，系统会自动为你生成评估报告。"

    if req.resume_context:
        system_prompt += f" 以下是候选人的简历摘要：{req.resume_context}。"

    if req.resume_questions:
        system_prompt += f" 请优先按照以下问题列表的顺序依次提问：{' | '.join(req.resume_questions)}。完成列表后再根据回答追问。"

    if not req.resume_context and not req.resume_questions:
        system_prompt += " 请你从基础技术问题开始提问，逐步深入。"
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": req.message}
            ],
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        return {"reply": reply}
    except Exception as e:
        error_msg = str(e)
        if "402" in error_msg or "balance" in error_msg.lower():
            hint = "DeepSeek 账户余额不足，请充值或更换 API Key。"
        else:
            hint = f"AI 接口调用失败：{error_msg}"
        return {"reply": hint}


@router.post("/start_interview")
async def start_interview(
    current_user: User = Depends(require_user),
    role: str = Form("后端开发"),
    resume_text: str = Form(""),
    questions_json: str = Form("[]")
):
    questions_list = json.loads(questions_json)
    if not questions_list:
        questions_list = generate_questions(resume_text)

    session = {
        "user_id": current_user.id,
        "role": role,
        "questions": questions_list,
        "current_index": 0,
        "question_status": ["pending"] * len(questions_list),
        "user_answers": [""] * len(questions_list),
        "finished": False,
    }
    interview_sessions[current_user.id] = session

    greeting = f"你好！我已分析了你的简历，准备了 {len(questions_list)} 个面试问题。让我们从第一个问题开始吧。\n\n{questions_list[0]}"

    return {
        "success": True,
        "greeting": greeting,
        "questions": questions_list,
        "current_index": 0,
        "total": len(questions_list),
    }


@router.post("/skip_question")
async def skip_question(current_user: User = Depends(require_user)):
    user_id = current_user.id
    session = interview_sessions.get(user_id)
    if not session:
        raise HTTPException(status_code=400, detail="没有活跃的面试会话")
    idx = session["current_index"]
    questions = session["questions"]
    if idx >= len(questions):
        return {"reply": "所有问题已结束，请点击结束面试生成报告。", "finished": True}
    session["question_status"][idx] = "skipped"
    session["user_answers"][idx] = "[跳过]"
    idx += 1
    session["current_index"] = idx
    if idx >= len(questions):
        session["finished"] = True
        return {"reply": "已跳过。我们的面试到此结束，感谢你的参与！", "finished": True, "current_index": idx}
    return {"reply": "好的，这个方向我们跳过。\n\n" + questions[idx], "finished": False, "current_index": idx}


@router.post("/generate_report")
async def generate_report(req: ReportRequest, current_user: User = Depends(require_user)):
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in req.messages])
    user_msgs = [m['content'] for m in req.messages if m['role'] == 'user']
    ai_msgs = [m['content'] for m in req.messages if m['role'] == 'assistant']
    question_count = len(ai_msgs)

    session = interview_sessions.get(current_user.id)
    question_status_note = ""
    if session:
        slist = []
        for i, q in enumerate(session["questions"]):
            s = session["question_status"][i]
            slist.append(f"  Q{i+1}: {q} -> {s}")
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
        interview_sessions.pop(current_user.id, None)
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
async def upload_resume(file: UploadFile = File(...)):
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

from typing import Optional
from datetime import date
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    role: str = "后端开发"
    resume_context: str = ""
    resume_questions: list = []
    # T-04：user_id 已移除 —— 身份一律由 JWT 解析（FR-2.4）。
    # T-11：action 已移除 —— 后端从未读取过它（前端曾传 'start'，纯死参数）。
    # 客户端若仍发送这些多余字段，Pydantic v2 默认忽略（extra='ignore'），不会报错。


class SaveInterviewRequest(BaseModel):
    role: str
    messages: list
    report: dict


class InterviewUpdateRequest(BaseModel):
    status: Optional[str] = None
    admin_comment: Optional[str] = None


class ProfileUpdate(BaseModel):
    nickname: Optional[str] = None
    bio: Optional[str] = None
    gender: Optional[str] = None
    birthday: Optional[date] = None


class ReportRequest(BaseModel):
    messages: list
    # T-11：user_id 已移除 —— 后端从未读取（generate_report 只用 req.messages
    # 与 current_user.id 定位会话）。保留会造成"可指定他人"的误导。

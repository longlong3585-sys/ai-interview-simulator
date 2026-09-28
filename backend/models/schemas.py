from typing import Optional
from datetime import date
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    role: str = "后端开发"
    resume_context: str = ""
    resume_questions: list = []
    # T-04：user_id 已移除 —— 身份一律由 JWT 解析（FR-2.4）。
    # 保留该字段会让调用方误以为可以指定用户，是安全隐患。
    # 客户端若仍发送该字段，Pydantic v2 默认忽略（extra='ignore'），不会报错。
    action: str = "chat"  # 死参数，待 T-11 清理（FR-4.9）


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
    user_id: Optional[int] = None

from typing import Optional
from datetime import date
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    role: str = "后端开发"
    resume_context: str = ""
    resume_questions: list = []
    user_id: Optional[int] = None
    action: str = "chat"


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

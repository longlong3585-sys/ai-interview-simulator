from typing import Optional
from datetime import date
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    # T-04：user_id 已移除 —— 身份一律由 JWT 解析（FR-2.4）。
    # T-11：action 已移除 —— 后端从未读取过它（前端曾传 'start'，纯死参数）。
    # T-23：role / resume_context / resume_questions 已移除 ——
    #   这三个字段原先只服务于"/api/chat 无会话时走通用 AI 对话"那条分支；
    #   T-23 把"无会话"改成 **409 + 指引**（否则任何登录用户都能把该接口当
    #   免费的 DeepSeek 代理用），分支消失，字段随之成为死参数。
    #   面试官人格所需的 role 与简历上下文由 `start_interview` 的表单字段
    #   （role / resume_text / questions_json）提供，并写进会话本身。
    # 客户端若仍发送多余字段，Pydantic v2 默认忽略（extra='ignore'），不会报错。


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



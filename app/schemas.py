"""请求 / 响应数据结构（Pydantic）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ==================================================================== 用户侧
class SessionCreateRequest(BaseModel):
    visitor_id: str = Field(default="", max_length=64, description="访客标识（前端 localStorage 生成）")


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, description="会话 ID；不传则自动新建会话")
    message: str = Field(min_length=1, max_length=1000, description="用户问题")
    visitor_id: str = Field(default="", max_length=64)


class CsatRequest(BaseModel):
    score: Literal[1, -1] = Field(description="1=好评，-1=差评")
    reason: str = Field(default="", max_length=100)
    comment: str = Field(default="", max_length=500)


class CloseSessionRequest(BaseModel):
    reason: str = Field(default="user", max_length=20)


# ==================================================================== 知识库
class KnowledgeIn(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=6000)
    category: str = Field(default="未分类", max_length=50)
    similar: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    slot: str | None = Field(default=None, max_length=50, description="需要先澄清的槽位 ID")
    enabled: bool = True


class KnowledgeUpdate(BaseModel):
    question: str | None = Field(default=None, min_length=1, max_length=300)
    answer: str | None = Field(default=None, min_length=1, max_length=6000)
    category: str | None = Field(default=None, max_length=50)
    similar: list[str] | None = None
    keywords: list[str] | None = None
    slot: str | None = None
    enabled: bool | None = None


class SearchTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


# ==================================================================== 文档导入
class DocumentImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120, description="文档标题")
    content: str = Field(min_length=1, description="文档正文（粘贴或前端读取的文本）")
    category: str = Field(default="", max_length=50)
    source_name: str = Field(default="", max_length=200)
    source_type: str = Field(default="text", max_length=20)
    publish: bool = Field(default=False, description="是否直接发布（默认先入草稿供预览）")


class DocumentPublishRequest(BaseModel):
    enabled: bool = True


# ==================================================================== 客服工作台
class AgentStatusRequest(BaseModel):
    status: Literal["online", "busy", "offline"]


class AgentReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class CannedIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)
    scope: Literal["team", "personal"] = "team"


class CannedUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    content: str | None = Field(default=None, max_length=2000)
    scope: Literal["team", "personal"] | None = None


# ==================================================================== 运营后台
class BotConfigUpdate(BaseModel):
    bot_name: str | None = Field(default=None, max_length=30)
    bot_avatar: str | None = Field(default=None, max_length=4)
    welcome: str | None = Field(default=None, max_length=600)
    ending: str | None = Field(default=None, max_length=600)
    fallback: str | None = Field(default=None, max_length=600)
    handoff: str | None = Field(default=None, max_length=600)
    handoff_offline: str | None = Field(default=None, max_length=600)
    handoff_trigger_prefix: str | None = Field(default=None, max_length=600)
    handoff_suggest: str | None = Field(default=None, max_length=600)
    quick_questions: list[str] | None = None
    service_hours: dict[str, list] | None = None
    service_hours_text: str | None = Field(default=None, max_length=120)


class TriggerRuleUpdate(BaseModel):
    threshold: int | None = Field(default=None, ge=0, le=999)
    enabled: int | None = Field(default=None, ge=0, le=1)


class AccountIn(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=6, max_length=64)
    name: str = Field(default="", max_length=30)
    role: Literal["admin", "supervisor", "agent", "editor"] = "agent"
    enabled: bool = True


class AccountUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=64)
    name: str | None = Field(default=None, max_length=30)
    role: Literal["admin", "supervisor", "agent", "editor"] | None = None
    enabled: bool | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=30)
    password: str = Field(min_length=1, max_length=64)


class FallbackToKnowledgeRequest(BaseModel):
    category: str = Field(default="未分类", max_length=50)
    answer: str = Field(min_length=1, max_length=6000)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, Any
from datetime import datetime
from models.models import MessageStatus, MessageType, ConversationType, MemberRole
import re


# ─── Auth ─────────────────────────────────────────────────────────────────────

class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserLogin(BaseModel):
    username: str  # username or email
    password: str
    device_id: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


# ─── User ─────────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    avatar_url: Optional[str]
    bio: Optional[str]
    is_verified: bool
    last_seen: Optional[datetime]
    show_last_seen: bool

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    bio: Optional[str] = Field(None, max_length=300)
    show_last_seen: Optional[bool] = None
    show_read_receipts: Optional[bool] = None


# ─── Conversation ────────────────────────────────────────────────────────────

class CreateDirectConversation(BaseModel):
    user_id: str  # The other person


class CreateGroupConversation(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    member_ids: list[str] = Field(..., min_length=1, max_length=255)


class ConversationOut(BaseModel):
    id: str
    type: ConversationType
    name: Optional[str]
    description: Optional[str]
    avatar_url: Optional[str]
    last_message_at: Optional[datetime]
    created_at: datetime
    unread_count: int = 0
    members: list["MemberOut"] = []

    class Config:
        from_attributes = True


class MemberOut(BaseModel):
    user_id: str
    role: MemberRole
    nickname: Optional[str]
    is_muted: bool
    joined_at: datetime
    user: Optional[UserOut] = None

    class Config:
        from_attributes = True


class UpdateGroup(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None


class AddMembers(BaseModel):
    user_ids: list[str]


# ─── Messages ────────────────────────────────────────────────────────────────

class SendMessage(BaseModel):
    conversation_id: str
    type: MessageType = MessageType.TEXT
    content: Optional[str] = Field(None, max_length=4096)
    reply_to_id: Optional[str] = None
    metadata: Optional[dict] = None

    @field_validator("content")
    @classmethod
    def content_required_for_text(cls, v, info):
        if info.data.get("type") == MessageType.TEXT and not v:
            raise ValueError("Content is required for text messages")
        return v


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    type: MessageType
    content: Optional[str]
    media_url: Optional[str]
    media_thumbnail: Optional[str]
    media_size: Optional[int]
    media_duration: Optional[int]
    metadata: Optional[Any]
    reply_to_id: Optional[str]
    is_forwarded: bool
    is_edited: bool
    edited_at: Optional[datetime]
    status: MessageStatus
    created_at: datetime
    sender: Optional[UserOut] = None
    reactions: list["ReactionOut"] = []

    class Config:
        from_attributes = True


class MessagePage(BaseModel):
    messages: list[MessageOut]
    has_more: bool
    next_cursor: Optional[str]


class EditMessage(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096)


class ForwardMessage(BaseModel):
    message_id: str
    conversation_ids: list[str]


class AddReaction(BaseModel):
    emoji: str = Field(..., max_length=10)


class ReactionOut(BaseModel):
    user_id: str
    emoji: str
    created_at: datetime

    class Config:
        from_attributes = True


class ReadReceipt(BaseModel):
    message_ids: list[str]


# ─── WebSocket Events (inbound from client) ──────────────────────────────────

class WSEventType:
    # Inbound
    SEND_MESSAGE    = "send_message"
    TYPING_START    = "typing_start"
    TYPING_STOP     = "typing_stop"
    READ_RECEIPT    = "read_receipt"
    PING            = "ping"
    # Outbound
    NEW_MESSAGE     = "new_message"
    MESSAGE_UPDATED = "message_updated"
    MESSAGE_DELETED = "message_deleted"
    TYPING_IND      = "typing"
    RECEIPT_UPDATE  = "receipt_update"
    PRESENCE_UPDATE = "presence_update"
    PONG            = "pong"
    ERROR           = "error"


class WSInbound(BaseModel):
    type: str
    data: Optional[dict] = None


# ─── Misc ────────────────────────────────────────────────────────────────────

class PaginationParams(BaseModel):
    limit: int = Field(50, ge=1, le=100)
    before: Optional[str] = None  # cursor (message_id or timestamp)


class PresenceOut(BaseModel):
    user_id: str
    is_online: bool
    last_seen: Optional[str]


ConversationOut.model_rebuild()
MemberOut.model_rebuild()
MessageOut.model_rebuild()

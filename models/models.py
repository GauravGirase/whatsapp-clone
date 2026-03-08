import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey,
    Text, Integer, Enum as SAEnum, Index, JSON
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()


def gen_uuid():
    return str(uuid.uuid4())


# ─── Enums ───────────────────────────────────────────────────────────────────

class MessageStatus(str, enum.Enum):
    PENDING   = "pending"
    SENT      = "sent"
    DELIVERED = "delivered"
    READ      = "read"
    FAILED    = "failed"


class MessageType(str, enum.Enum):
    TEXT     = "text"
    IMAGE    = "image"
    VIDEO    = "video"
    AUDIO    = "audio"
    FILE     = "file"
    LOCATION = "location"
    STICKER  = "sticker"
    REACTION = "reaction"
    DELETED  = "deleted"


class ConversationType(str, enum.Enum):
    DIRECT = "direct"
    GROUP  = "group"


class MemberRole(str, enum.Enum):
    OWNER  = "owner"
    ADMIN  = "admin"
    MEMBER = "member"


# ─── Models ──────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    username        = Column(String(50), unique=True, nullable=False, index=True)
    email           = Column(String(255), unique=True, nullable=False, index=True)
    phone           = Column(String(20), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    display_name    = Column(String(100), nullable=False)
    avatar_url      = Column(String(500), nullable=True)
    bio             = Column(String(300), nullable=True)
    is_active       = Column(Boolean, default=True)
    is_verified     = Column(Boolean, default=True)
    last_seen       = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())

    # Privacy settings
    show_last_seen  = Column(Boolean, default=True)
    show_read_receipts = Column(Boolean, default=True)

    # Relationships
    memberships      = relationship("ConversationMember", back_populates="user")
    sent_messages    = relationship("Message", back_populates="sender", foreign_keys="Message.sender_id")
    refresh_tokens   = relationship("RefreshToken", back_populates="user")
    push_tokens      = relationship("PushToken", back_populates="user")

    def __repr__(self):
        return f"<User {self.username}>"


class Conversation(Base):
    __tablename__ = "conversations"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    type         = Column(SAEnum(ConversationType), nullable=False, default=ConversationType.DIRECT)
    name         = Column(String(100), nullable=True)   # For groups
    description  = Column(String(500), nullable=True)
    avatar_url   = Column(String(500), nullable=True)
    created_by   = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    is_archived  = Column(Boolean, default=False)
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    updated_at   = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    members      = relationship("ConversationMember", back_populates="conversation")
    messages     = relationship("Message", back_populates="conversation", order_by="Message.created_at")

    __table_args__ = (
        Index("ix_conversations_last_message_at", "last_message_at"),
    )


class ConversationMember(Base):
    __tablename__ = "conversation_members"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    conversation_id = Column(UUID(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    user_id         = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role            = Column(SAEnum(MemberRole), default=MemberRole.MEMBER)
    nickname        = Column(String(100), nullable=True)
    is_muted        = Column(Boolean, default=False)
    muted_until     = Column(DateTime(timezone=True), nullable=True)
    last_read_at    = Column(DateTime(timezone=True), nullable=True)
    joined_at       = Column(DateTime(timezone=True), server_default=func.now())
    left_at         = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    conversation    = relationship("Conversation", back_populates="members")
    user            = relationship("User", back_populates="memberships")

    __table_args__ = (
        Index("ix_conv_members_unique", "conversation_id", "user_id", unique=True),
    )


class Message(Base):
    __tablename__ = "messages"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    conversation_id = Column(UUID(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id       = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True)
    type            = Column(SAEnum(MessageType), default=MessageType.TEXT, nullable=False)
    content         = Column(Text, nullable=True)          # Text content
    media_url       = Column(String(500), nullable=True)   # S3 URL for media
    media_thumbnail = Column(String(500), nullable=True)
    media_size      = Column(Integer, nullable=True)       # Bytes
    media_duration  = Column(Integer, nullable=True)       # Seconds for audio/video
    # metadata        = Column(JSON, nullable=True)          # Flexible extra data
    meta_data = Column("metadata", JSON, nullable=True)
    reply_to_id     = Column(UUID(as_uuid=False), ForeignKey("messages.id"), nullable=True)
    is_forwarded    = Column(Boolean, default=False)
    is_edited       = Column(Boolean, default=False)
    edited_at       = Column(DateTime(timezone=True), nullable=True)
    status          = Column(SAEnum(MessageStatus), default=MessageStatus.SENT)
    created_at      = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    deleted_at      = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    conversation    = relationship("Conversation", back_populates="messages")
    sender          = relationship("User", back_populates="sent_messages", foreign_keys=[sender_id])
    reply_to        = relationship("Message", remote_side="Message.id", foreign_keys=[reply_to_id])
    receipts        = relationship("MessageReceipt", back_populates="message",  lazy="selectin")
    reactions       = relationship("MessageReaction", back_populates="message", lazy="selectin")

    __table_args__ = (
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
    )


class MessageReceipt(Base):
    """Tracks per-user delivery and read status."""
    __tablename__ = "message_receipts"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    message_id = Column(UUID(as_uuid=False), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    status     = Column(SAEnum(MessageStatus), default=MessageStatus.DELIVERED)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    message    = relationship("Message", back_populates="receipts")

    __table_args__ = (
        Index("ix_receipt_msg_user", "message_id", "user_id", unique=True),
    )


class MessageReaction(Base):
    __tablename__ = "message_reactions"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    message_id = Column(UUID(as_uuid=False), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    emoji      = Column(String(10), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    message    = relationship("Message", back_populates="reactions")

    __table_args__ = (
        Index("ix_reaction_unique", "message_id", "user_id", "emoji", unique=True),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token      = Column(String(500), unique=True, nullable=False)
    device_id  = Column(String(200), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user       = relationship("User", back_populates="refresh_tokens")


class PushToken(Base):
    """FCM / APNS push notification tokens."""
    __tablename__ = "push_tokens"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token      = Column(String(500), nullable=False)
    platform   = Column(String(20), nullable=False)  # ios, android, web
    device_id  = Column(String(200), nullable=True)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user       = relationship("User", back_populates="push_tokens")

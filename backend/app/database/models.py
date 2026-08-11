"""Platform-owned relational models."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database.base import Base


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class DefaultLanguage(StrEnum):
    AR = "ar"
    EN = "en"


class ToolCallStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"


account_status_enum = ENUM(
    AccountStatus,
    name="account_status",
    values_callable=lambda items: [item.value for item in items],
)
default_language_enum = ENUM(
    DefaultLanguage,
    name="default_language",
    values_callable=lambda items: [item.value for item in items],
)
tool_call_status_enum = ENUM(
    ToolCallStatus,
    name="tool_call_status",
    values_callable=lambda items: [item.value for item in items],
)


def normalize_business_name(value: str) -> str:
    """Collapse whitespace while preserving a clean display name."""
    return " ".join(value.split())


class User(Base):
    """A platform account identified independently from business access."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("btrim(email) <> ''", name="ck_users_email_not_blank"),
        CheckConstraint(
            "btrim(first_name) <> ''", name="ck_users_first_name_not_blank"
        ),
        CheckConstraint("btrim(last_name) <> ''", name="ck_users_last_name_not_blank"),
        Index("uq_users_email_ci", text("lower(email)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        account_status_enum,
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=text("'active'::account_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[BusinessMembership]] = relationship(back_populates="user")
    tool_call_logs: Mapped[list[ToolCallLog]] = relationship(
        back_populates="user", passive_deletes=True
    )
    refresh_sessions: Mapped[list[RefreshSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    email_verification_tokens: Mapped[list[EmailVerificationToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    password_reset_tokens: Mapped[list[PasswordResetToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("email")
    def normalize_email(self, _key: str, value: str) -> str:
        return value.strip().lower()

    @validates("first_name", "last_name")
    def trim_name(self, _key: str, value: str) -> str:
        return value.strip()


class RefreshSession(Base):
    """One hashed refresh-token generation within a device session family."""

    __tablename__ = "refresh_sessions"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_refresh_sessions_hash_format"
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_refresh_sessions_expiration"
        ),
        Index("ix_refresh_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_sessions_family", "session_family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_sessions.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="refresh_sessions")


class EmailVerificationToken(Base):
    """Single-use hashed token proving control of a user's email address."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_email_verification_hash_format"
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_email_verification_expiration"
        ),
        Index("ix_email_verification_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="email_verification_tokens")


class PasswordResetToken(Base):
    """Single-use hashed token authorizing a password reset."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_password_reset_hash_format"
        ),
        CheckConstraint("expires_at > created_at", name="ck_password_reset_expiration"),
        Index("ix_password_reset_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="password_reset_tokens")


class AuthenticationEvent(Base):
    """Persistent, privacy-limited counters for authentication abuse controls."""

    __tablename__ = "authentication_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('login_failure', 'login_block', "
            "'verification_resend', 'password_reset_request')",
            name="ck_auth_events_type",
        ),
        Index(
            "ix_auth_events_scope_created",
            "event_type",
            "normalized_email",
            "client_ip",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    client_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Business(Base):
    """A lightweight business profile owned through one MVP membership."""

    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_businesses_name_not_blank"),
        CheckConstraint(
            "btrim(normalized_name) <> ''",
            name="ck_businesses_normalized_name_not_blank",
        ),
        CheckConstraint(
            "char_length(country) = 2", name="ck_businesses_country_length"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(150))
    country: Mapped[str] = mapped_column(
        String(2), nullable=False, default="LB", server_default="LB"
    )
    timezone: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Asia/Beirut", server_default="Asia/Beirut"
    )
    default_language: Mapped[DefaultLanguage] = mapped_column(
        default_language_enum,
        nullable=False,
        default=DefaultLanguage.AR,
        server_default=text("'ar'::default_language"),
    )
    governorate: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(150))
    address_line: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AccountStatus] = mapped_column(
        account_status_enum,
        nullable=False,
        default=AccountStatus.DISABLED,
        server_default=text("'disabled'::account_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    membership: Mapped[BusinessMembership | None] = relationship(
        back_populates="business", uselist=False
    )
    opening_days: Mapped[list[BusinessOpeningDay]] = relationship(
        back_populates="business", cascade="all, delete-orphan", passive_deletes=True
    )
    tool_call_logs: Mapped[list[ToolCallLog]] = relationship(
        back_populates="business", passive_deletes=True
    )

    def __init__(self, **kwargs: object) -> None:
        if "name" in kwargs and "normalized_name" not in kwargs:
            clean_name = normalize_business_name(str(kwargs["name"]))
            kwargs["name"] = clean_name
            kwargs["normalized_name"] = clean_name.casefold()
        super().__init__(**kwargs)

    @validates("name")
    def clean_name(self, _key: str, value: str) -> str:
        clean_value = normalize_business_name(value)
        self.normalized_name = clean_value.casefold()
        return clean_value


class BusinessMembership(Base):
    """The single MVP owner connection, ready for later role expansion."""

    __tablename__ = "business_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "business_id", name="uq_memberships_user_business"),
        UniqueConstraint("business_id", name="uq_memberships_business"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[AccountStatus] = mapped_column(
        account_status_enum,
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=text("'active'::account_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    business: Mapped[Business] = relationship(back_populates="membership")


class BusinessOpeningDay(Base):
    """One weekly schedule record; Monday is 0 and Sunday is 6."""

    __tablename__ = "business_opening_days"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_opening_days_weekday"),
        UniqueConstraint(
            "business_id", "day_of_week", name="uq_opening_days_business_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="opening_days")
    shifts: Mapped[list[BusinessOpeningShift]] = relationship(
        back_populates="opening_day", cascade="all, delete-orphan", passive_deletes=True
    )


class BusinessOpeningShift(Base):
    """A local wall-clock interval, optionally crossing midnight."""

    __tablename__ = "business_opening_shifts"
    __table_args__ = (
        CheckConstraint(
            "opens_at <> closes_at", name="ck_opening_shifts_distinct_times"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    opening_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_opening_days.id", ondelete="CASCADE"), nullable=False
    )
    opens_at: Mapped[time] = mapped_column(Time, nullable=False)
    closes_at: Mapped[time] = mapped_column(Time, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    opening_day: Mapped[BusinessOpeningDay] = relationship(back_populates="shifts")


class ToolCallLog(Base):
    """Privacy-minimal metadata for a future centralized tool executor."""

    __tablename__ = "tool_call_logs"
    __table_args__ = (
        CheckConstraint(
            "btrim(tool_name) <> ''", name="ck_tool_logs_tool_name_not_blank"
        ),
        CheckConstraint(
            "args_hash ~ '^[0-9a-f]{64}$'", name="ck_tool_logs_args_hash_format"
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_tool_logs_latency_nonnegative",
        ),
        CheckConstraint(
            "(status = 'success' AND error_code IS NULL) OR "
            "(status IN ('error', 'denied') AND error_code IS NOT NULL AND "
            "btrim(error_code) <> '' AND "
            "error_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$')",
            name="ck_tool_logs_status_error_code",
        ),
        Index("ix_tool_logs_business_created", "business_id", "created_at"),
        Index("ix_tool_logs_tool_status", "tool_name", "status"),
        Index(
            "ix_tool_logs_non_success",
            "created_at",
            postgresql_where=text("status <> 'success'::tool_call_status"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        tool_call_status_enum, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(200))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="tool_call_logs")
    user: Mapped[User | None] = relationship(back_populates="tool_call_logs")

    @validates("tool_name", "error_code")
    def trim_audit_text(self, _key: str, value: str | None) -> str | None:
        return value.strip() if value is not None else None

"""Platform-owned relational models."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class BusinessStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class MembershipPermission(StrEnum):
    FULL_ACCESS = "FULL_ACCESS"


class BusinessCategory(StrEnum):
    GROCERY_SUPERMARKET = "GROCERY_SUPERMARKET"
    BAKERY = "BAKERY"
    RESTAURANT = "RESTAURANT"
    CAFE = "CAFE"
    CLOTHING = "CLOTHING"
    ELECTRONICS = "ELECTRONICS"
    PHARMACY = "PHARMACY"
    BEAUTY_COSMETICS = "BEAUTY_COSMETICS"
    HOME_FURNITURE = "HOME_FURNITURE"
    SERVICES = "SERVICES"
    OTHER = "OTHER"


class ToolCallStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"


class ChatMessageRole(StrEnum):
    OWNER = "owner"
    ASSISTANT = "assistant"


class ChatGenerationState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class KnowledgeKind(StrEnum):
    PERMANENT = "permanent"
    TEMPORARY = "temporary"


class KnowledgeCategory(StrEnum):
    DELIVERY = "delivery"
    RETURNS = "returns"
    WARRANTY = "warranty"
    SERVICE = "service"
    POLICY = "policy"
    TEMPORARY_NOTICE = "temporary_notice"
    PROMOTION = "promotion"


class AIUsageReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMPLETED = "completed"
    RELEASED = "released"
    CHARGED = "charged"


class KnowledgeDocumentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


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
business_status_enum = ENUM(
    BusinessStatus,
    name="business_status",
    values_callable=lambda items: [item.value for item in items],
)
membership_permission_enum = ENUM(
    MembershipPermission,
    name="membership_permission",
    values_callable=lambda items: [item.value for item in items],
)
business_category_enum = ENUM(
    BusinessCategory,
    name="business_category",
    values_callable=lambda items: [item.value for item in items],
)
knowledge_document_status_enum = ENUM(
    KnowledgeDocumentStatus,
    name="knowledge_document_status",
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
    owned_businesses: Mapped[list[Business]] = relationship(
        back_populates="owner", foreign_keys="Business.owner_user_id"
    )
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
        Index(
            "ix_auth_events_type_email_created",
            "event_type",
            "normalized_email",
            "created_at",
        ),
        Index(
            "ix_auth_events_type_ip_created",
            "event_type",
            "client_ip",
            "created_at",
        ),
        Index("ix_auth_events_created", "created_at"),
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


class AuthenticationMaintenanceTask(Base):
    """Persistent coordination state for one authentication maintenance task."""

    __tablename__ = "authentication_maintenance_tasks"
    __table_args__ = (
        CheckConstraint(
            "btrim(task_name) <> ''",
            name="ck_auth_maintenance_task_name_not_blank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


BUSINESS_GOVERNORATE_CHECK = """
governorate IS NULL OR governorate IN
('Beirut','Mount Lebanon','North','Akkar','Bekaa','Baalbek-Hermel','South','Nabatieh')
"""
BUSINESS_DISTRICT_CHECK = """
district IS NULL OR district IN
('Beirut','Baabda','Aley','Metn','Keserwan','Chouf','Tripoli','Zgharta','Koura',
 'Akkar','Zahle','West Bekaa','Baalbek','Hermel','Saida','Jezzine','Nabatieh',
 'Bint Jbeil','Marjayoun')
"""
BUSINESS_CITY_CHECK = """
city IS NULL OR city IN
('Beirut','Baabda','Hazmieh','Aley','Choueifat','Antelias','Jdeideh','Sin El Fil',
 'Dekwaneh','Baouchrieh','Jounieh','Zouk Mikael','Kaslik','Beiteddine','Damour',
 'Deir El Qamar','Tripoli','Mina','Zgharta','Ehden','Amioun','Halba','Zahle',
 'Chtaura','Jeb Jennine','Qab Elias','Baalbek','Hermel','Saida','Abra','Ghaziyeh',
 'Jezzine','Nabatieh','Kfar Roummane','Bint Jbeil','Marjayoun','Khiam')
"""
BUSINESS_LOCATION_HIERARCHY_CHECK = """
(governorate IS NULL OR district IS NULL OR city IS NULL) OR
(governorate='Beirut' AND district='Beirut' AND city='Beirut') OR
(governorate='Mount Lebanon' AND (
 (district='Baabda' AND city IN ('Baabda','Hazmieh')) OR
 (district='Aley' AND city IN ('Aley','Choueifat')) OR
 (district='Metn' AND city IN
  ('Antelias','Jdeideh','Sin El Fil','Dekwaneh','Baouchrieh')) OR
 (district='Keserwan' AND city IN ('Jounieh','Zouk Mikael','Kaslik')) OR
 (district='Chouf' AND city IN ('Beiteddine','Damour','Deir El Qamar')))) OR
(governorate='North' AND (
 (district='Tripoli' AND city IN ('Tripoli','Mina')) OR
 (district='Zgharta' AND city IN ('Zgharta','Ehden')) OR
 (district='Koura' AND city='Amioun'))) OR
(governorate='Akkar' AND district='Akkar' AND city='Halba') OR
(governorate='Bekaa' AND (
 (district='Zahle' AND city IN ('Zahle','Chtaura')) OR
 (district='West Bekaa' AND city IN ('Jeb Jennine','Qab Elias')))) OR
(governorate='Baalbek-Hermel' AND (
 (district='Baalbek' AND city='Baalbek') OR
 (district='Hermel' AND city='Hermel'))) OR
(governorate='South' AND (
 (district='Saida' AND city IN ('Saida','Abra','Ghaziyeh')) OR
 (district='Jezzine' AND city='Jezzine'))) OR
(governorate='Nabatieh' AND (
 (district='Nabatieh' AND city IN ('Nabatieh','Kfar Roummane')) OR
 (district='Bint Jbeil' AND city='Bint Jbeil') OR
 (district='Marjayoun' AND city IN ('Marjayoun','Khiam'))))
"""


class Business(Base):
    """An independently onboarded tenant business."""

    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_businesses_name_not_blank"),
        CheckConstraint(
            "btrim(normalized_name) <> ''",
            name="ck_businesses_normalized_name_not_blank",
        ),
        CheckConstraint(
            "char_length(name) BETWEEN 2 AND 120",
            name="ck_businesses_name_length",
        ),
        CheckConstraint(
            "description IS NULL OR "
            "char_length(btrim(description)) BETWEEN 20 AND 2000",
            name="ck_businesses_description_length",
        ),
        CheckConstraint(
            "custom_category IS NULL OR "
            "char_length(btrim(custom_category)) BETWEEN 2 AND 100",
            name="ck_businesses_custom_category_length",
        ),
        CheckConstraint(
            "address_line IS NULL OR "
            "char_length(btrim(address_line)) BETWEEN 5 AND 255",
            name="ck_businesses_address_length",
        ),
        CheckConstraint(
            "(category = 'OTHER' AND custom_category IS NOT NULL) OR "
            "(category IS DISTINCT FROM 'OTHER' AND custom_category IS NULL)",
            name="ck_businesses_custom_category_rule",
        ),
        CheckConstraint(
            BUSINESS_GOVERNORATE_CHECK,
            name="ck_businesses_governorate",
        ),
        CheckConstraint(
            BUSINESS_DISTRICT_CHECK,
            name="ck_businesses_district",
        ),
        CheckConstraint(
            BUSINESS_CITY_CHECK,
            name="ck_businesses_city",
        ),
        CheckConstraint(
            BUSINESS_LOCATION_HIERARCHY_CHECK,
            name="ck_businesses_location_hierarchy",
        ),
        UniqueConstraint(
            "owner_user_id", "normalized_name", name="uq_businesses_owner_name"
        ),
        CheckConstraint(
            "char_length(country) = 2", name="ck_businesses_country_length"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[BusinessCategory | None] = mapped_column(business_category_enum)
    custom_category: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(
        String(2), nullable=False, default="LB", server_default="LB"
    )
    timezone: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Asia/Beirut", server_default="Asia/Beirut"
    )
    governorate: Mapped[str | None] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(150))
    address_line: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[BusinessStatus] = mapped_column(
        business_status_enum,
        nullable=False,
        default=BusinessStatus.PENDING,
        server_default=text("'PENDING'::business_status"),
    )
    onboarding_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
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

    memberships: Mapped[list[BusinessMembership]] = relationship(
        back_populates="business"
    )
    owner: Mapped[User] = relationship(
        back_populates="owned_businesses", foreign_keys=[owner_user_id]
    )
    opening_days: Mapped[list[BusinessOpeningDay]] = relationship(
        back_populates="business", cascade="all, delete-orphan", passive_deletes=True
    )
    tool_call_logs: Mapped[list[ToolCallLog]] = relationship(
        back_populates="business", passive_deletes=True
    )
    owner_conversation: Mapped[OwnerConversation | None] = relationship(
        back_populates="business", cascade="all, delete-orphan", passive_deletes=True
    )
    knowledge: Mapped[list[BusinessKnowledge]] = relationship(
        back_populates="business", cascade="all, delete-orphan", passive_deletes=True
    )
    lifecycle_history: Mapped[list[BusinessLifecycleHistory]] = relationship(
        back_populates="business", passive_deletes=True
    )
    ai_allowance_config: Mapped[BusinessAIAllowanceConfig | None] = relationship(
        back_populates="business", passive_deletes=True
    )
    knowledge_documents: Mapped[list[KnowledgeDocument]] = relationship(
        back_populates="business", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_active(self) -> bool:
        """Derive response compatibility from the authoritative lifecycle status."""
        return self.status is BusinessStatus.ACTIVE

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
    """A user's access relationship to one tenant business."""

    __tablename__ = "business_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "business_id", name="uq_memberships_user_business"),
        Index("ix_memberships_business", "business_id"),
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
    permission: Mapped[MembershipPermission] = mapped_column(
        membership_permission_enum,
        nullable=False,
        default=MembershipPermission.FULL_ACCESS,
        server_default=text("'FULL_ACCESS'::membership_permission"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    business: Mapped[Business] = relationship(back_populates="memberships")


class BusinessLifecycleHistory(Base):
    """Permanent internal audit history for database-controlled lifecycle changes."""

    __tablename__ = "business_lifecycle_history"
    __table_args__ = (
        CheckConstraint(
            "previous_status <> new_status",
            name="ck_business_lifecycle_history_status_changed",
        ),
        CheckConstraint(
            "char_length(btrim(admin_identifier)) BETWEEN 1 AND 320",
            name="ck_business_lifecycle_history_admin_length",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 2000",
            name="ck_business_lifecycle_history_reason_length",
        ),
        Index(
            "ix_business_lifecycle_history_business_changed",
            "business_id",
            "changed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    previous_status: Mapped[BusinessStatus] = mapped_column(
        business_status_enum, nullable=False
    )
    new_status: Mapped[BusinessStatus] = mapped_column(
        business_status_enum, nullable=False
    )
    admin_identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="lifecycle_history")


class RegistrationRateLimitEvent(Base):
    """Privacy-minimal registration-attempt counter."""

    __tablename__ = "registration_rate_limit_events"
    __table_args__ = (
        Index("ix_registration_rate_email_created", "normalized_email", "created_at"),
        Index("ix_registration_rate_ip_created", "client_ip", "created_at"),
        Index("ix_registration_rate_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    client_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OwnerChatRateLimitEvent(Base):
    """One admitted owner-chat generation attempt."""

    __tablename__ = "owner_chat_rate_limit_events"
    __table_args__ = (
        UniqueConstraint(
            "owner_message_id",
            "generation_attempt",
            name="uq_owner_chat_rate_message_attempt",
        ),
        CheckConstraint(
            "generation_attempt > 0", name="ck_owner_chat_rate_attempt_positive"
        ),
        Index("ix_owner_chat_rate_business_created", "business_id", "created_at"),
        Index("ix_owner_chat_rate_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    owner_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owner_chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    generation_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BusinessAIAllowanceConfig(Base):
    """Protected per-business daily AI allowance configuration."""

    __tablename__ = "business_ai_allowance_configs"
    __table_args__ = (
        CheckConstraint(
            "daily_token_allowance BETWEEN 1 AND 1000000000",
            name="ck_ai_allowance_daily_range",
        ),
        CheckConstraint(
            "owner_reserve_percent BETWEEN 0 AND 100",
            name="ck_ai_allowance_reserve_range",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    daily_token_allowance: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("20000")
    )
    owner_reserve_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("25")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    business: Mapped[Business] = relationship(back_populates="ai_allowance_config")


class BusinessAIAllowanceAudit(Base):
    """Permanent append-only audit of administrative allowance changes."""

    __tablename__ = "business_ai_allowance_audit"
    __table_args__ = (
        CheckConstraint(
            "previous_daily_token_allowance <> new_daily_token_allowance OR "
            "previous_owner_reserve_percent <> new_owner_reserve_percent",
            name="ck_ai_allowance_audit_changed",
        ),
        CheckConstraint(
            "char_length(btrim(admin_identifier)) BETWEEN 1 AND 320",
            name="ck_ai_allowance_audit_admin_length",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 2000",
            name="ck_ai_allowance_audit_reason_length",
        ),
        Index("ix_ai_allowance_audit_business_changed", "business_id", "changed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False
    )
    previous_daily_token_allowance: Mapped[int] = mapped_column(Integer, nullable=False)
    new_daily_token_allowance: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_owner_reserve_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    new_owner_reserve_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    admin_identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BusinessAIUsageDaily(Base):
    """Privacy-minimal daily token summary for one business-local window."""

    __tablename__ = "business_ai_usage_daily"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "window_start", name="uq_ai_usage_daily_window"
        ),
        CheckConstraint(
            "window_end > window_start", name="ck_ai_usage_daily_window_order"
        ),
        CheckConstraint(
            "input_tokens_used >= 0 AND output_tokens_used >= 0 AND "
            "total_tokens_used >= 0 AND tokens_reserved >= 0",
            name="ck_ai_usage_daily_nonnegative",
        ),
        CheckConstraint(
            "total_tokens_used = input_tokens_used + output_tokens_used",
            name="ck_ai_usage_daily_total",
        ),
        Index("ix_ai_usage_daily_business_end", "business_id", "window_end"),
        Index("ix_ai_usage_daily_window_end", "window_end"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    input_tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    output_tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    tokens_reserved: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AIUsageReservation(Base):
    """Leased provider-neutral reservation and final token accounting record."""

    __tablename__ = "ai_usage_reservations"
    __table_args__ = (
        UniqueConstraint(
            "owner_message_id",
            "generation_attempt",
            name="uq_ai_reservation_message_attempt",
        ),
        CheckConstraint(
            "channel IN ('owner', 'customer', 'whatsapp')",
            name="ck_ai_reservation_channel",
        ),
        CheckConstraint(
            "status IN ('reserved', 'completed', 'released', 'charged')",
            name="ck_ai_reservation_status",
        ),
        CheckConstraint(
            "estimated_input_tokens >= 0 AND max_output_tokens > 0 AND "
            "reserved_tokens = estimated_input_tokens + max_output_tokens",
            name="ck_ai_reservation_reserved_total",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_ai_reservation_input_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_ai_reservation_output_nonnegative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_ai_reservation_total_nonnegative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens = input_tokens + output_tokens",
            name="ck_ai_reservation_actual_total",
        ),
        CheckConstraint(
            "window_end > window_start AND lease_expires_at > created_at",
            name="ck_ai_reservation_time_order",
        ),
        CheckConstraint(
            "char_length(capability) BETWEEN 1 AND 50",
            name="ck_ai_reservation_capability_length",
        ),
        CheckConstraint(
            "provider_identifier IS NULL OR char_length(provider_identifier) <= 50",
            name="ck_ai_reservation_provider_length",
        ),
        CheckConstraint(
            "model_identifier IS NULL OR char_length(model_identifier) <= 100",
            name="ck_ai_reservation_model_length",
        ),
        Index("ix_ai_reservation_business_window", "business_id", "window_start"),
        Index("ix_ai_reservation_lease", "status", "lease_expires_at"),
        Index("ix_ai_reservation_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    owner_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("owner_chat_messages.id", ondelete="SET NULL")
    )
    generation_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    capability: Mapped[str] = mapped_column(String(50), nullable=False)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    counts_authoritative: Mapped[bool | None] = mapped_column(Boolean)
    provider_identifier: Mapped[str | None] = mapped_column(String(50))
    model_identifier: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[AIUsageReservationStatus] = mapped_column(
        String(20), nullable=False, server_default="reserved"
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    """A same-day local wall-clock interval."""

    __tablename__ = "business_opening_shifts"
    __table_args__ = (
        CheckConstraint("opens_at < closes_at", name="ck_opening_shifts_ordered_times"),
        UniqueConstraint(
            "opening_day_id",
            "opens_at",
            "closes_at",
            name="uq_opening_shifts_day_interval",
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


class OwnerConversation(Base):
    """The single private owner conversation for one business."""

    __tablename__ = "owner_conversations"
    __table_args__ = (
        CheckConstraint(
            "next_turn_number > 0", name="ck_owner_conversations_next_turn_positive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    next_turn_number: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
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

    business: Mapped[Business] = relationship(back_populates="owner_conversation")
    messages: Mapped[list[OwnerChatMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="OwnerChatMessage.conversation_id",
    )


class OwnerChatMessage(Base):
    """One logically ordered owner or assistant message."""

    __tablename__ = "owner_chat_messages"
    __table_args__ = (
        CheckConstraint(
            "(role = 'owner' AND "
            "char_length(btrim(content)) BETWEEN 1 AND 4000) OR "
            "(role = 'assistant' AND "
            "char_length(btrim(content)) BETWEEN 1 AND 14000)",
            name="ck_owner_chat_messages_content_length",
        ),
        CheckConstraint(
            "role IN ('owner', 'assistant')",
            name="ck_owner_chat_messages_role",
        ),
        CheckConstraint(
            "generation_state IS NULL OR generation_state IN "
            "('pending', 'processing', 'completed', 'failed')",
            name="ck_owner_chat_messages_generation_state",
        ),
        CheckConstraint(
            "(role = 'owner' AND sequence_number % 2 = 1 AND "
            "idempotency_key IS NOT NULL AND reply_to_message_id IS NULL AND "
            "generation_state IS NOT NULL) OR "
            "(role = 'assistant' AND sequence_number % 2 = 0 AND "
            "idempotency_key IS NULL AND reply_to_message_id IS NOT NULL AND "
            "generation_state IS NULL)",
            name="ck_owner_chat_messages_role_fields",
        ),
        CheckConstraint(
            "btrim(idempotency_key) <> ''",
            name="ck_owner_chat_messages_idempotency_not_blank",
        ),
        CheckConstraint(
            "(generation_state = 'processing' AND generation_claim_token IS NOT NULL "
            "AND generation_claim_expires_at IS NOT NULL) OR "
            "(generation_state IS DISTINCT FROM 'processing' AND "
            "generation_claim_token IS NULL AND generation_claim_expires_at IS NULL)",
            name="ck_owner_chat_messages_claim_state",
        ),
        CheckConstraint(
            "generation_attempts >= 0",
            name="ck_owner_chat_messages_attempts_nonnegative",
        ),
        UniqueConstraint(
            "conversation_id", "sequence_number", name="uq_owner_chat_message_order"
        ),
        UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_owner_chat_idempotency"
        ),
        UniqueConstraint(
            "conversation_id", "id", name="uq_owner_chat_message_conversation_id"
        ),
        UniqueConstraint("reply_to_message_id", name="uq_owner_chat_reply"),
        ForeignKeyConstraint(
            ["conversation_id", "reply_to_message_id"],
            ["owner_chat_messages.conversation_id", "owner_chat_messages.id"],
            name="fk_owner_chat_reply_same_conversation",
            ondelete="CASCADE",
        ),
        Index(
            "ix_owner_chat_messages_history",
            "conversation_id",
            text("sequence_number DESC"),
            "id",
        ),
        Index(
            "ix_owner_chat_messages_generation",
            "conversation_id",
            "generation_state",
            "sequence_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("owner_conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[ChatMessageRole] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    generation_state: Mapped[ChatGenerationState | None] = mapped_column(String(20))
    generation_claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    generation_claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    generation_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversation: Mapped[OwnerConversation] = relationship(
        back_populates="messages", foreign_keys=[conversation_id]
    )
    reply_to_message: Mapped[OwnerChatMessage | None] = relationship(
        remote_side=[id], foreign_keys=[reply_to_message_id]
    )


class BusinessKnowledge(Base):
    """Owner-reviewable durable knowledge learned from owner chat."""

    __tablename__ = "business_knowledge"
    __table_args__ = (
        CheckConstraint(
            "subject_key ~ '^[a-z0-9]+(_[a-z0-9]+)*$'",
            name="ck_business_knowledge_subject_key",
        ),
        CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 4000",
            name="ck_business_knowledge_content_length",
        ),
        CheckConstraint(
            "kind IN ('permanent', 'temporary')",
            name="ck_business_knowledge_kind",
        ),
        CheckConstraint(
            "category IN ('delivery', 'returns', 'warranty', 'service', 'policy', "
            "'temporary_notice', 'promotion')",
            name="ck_business_knowledge_category",
        ),
        CheckConstraint(
            "(kind = 'permanent' AND expires_at IS NULL) OR "
            "(kind = 'temporary' AND expires_at IS NOT NULL)",
            name="ck_business_knowledge_expiry",
        ),
        CheckConstraint(
            "source = 'owner_chat'",
            name="ck_business_knowledge_source",
        ),
        UniqueConstraint(
            "business_id", "subject_key", name="uq_business_knowledge_subject"
        ),
        Index("ix_business_knowledge_context", "business_id", "expires_at"),
        Index("ix_business_knowledge_management", "business_id", "updated_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    subject_key: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[KnowledgeKind] = mapped_column(String(20), nullable=False)
    category: Mapped[KnowledgeCategory] = mapped_column(String(30), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="owner_chat", server_default="owner_chat"
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("owner_chat_messages.id", ondelete="SET NULL")
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

    business: Mapped[Business] = relationship(back_populates="knowledge")


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


class KnowledgeDocument(Base):
    """Tenant-owned uploaded-document metadata, separate from owner-chat facts."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(original_filename)) BETWEEN 1 AND 255",
            name="ck_knowledge_documents_filename",
        ),
        CheckConstraint(
            "original_filename !~ '[/\\\\[:cntrl:]]'",
            name="ck_knowledge_documents_filename_safe",
        ),
        CheckConstraint(
            "char_length(btrim(mime_type)) BETWEEN 1 AND 255",
            name="ck_knowledge_documents_mime_type",
        ),
        CheckConstraint("file_size_bytes > 0", name="ck_knowledge_documents_file_size"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_knowledge_documents_hash"
        ),
        CheckConstraint(
            "char_length(btrim(storage_key)) BETWEEN 1 AND 1024 AND storage_key !~ '(^/|^[A-Za-z]:[/\\\\]|^https?://|(^|/)\\.\\.(/|$)|[[:cntrl:]])'",
            name="ck_knowledge_documents_storage_key",
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count > 0",
            name="ck_knowledge_documents_page_count",
        ),
        CheckConstraint(
            "replaces_document_id IS NULL OR replaces_document_id <> id",
            name="ck_knowledge_documents_not_self_replacement",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (char_length(failure_code) BETWEEN 1 AND 100 AND failure_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$')",
            name="ck_knowledge_documents_failure_code",
        ),
        CheckConstraint(
            "failure_message IS NULL OR (char_length(btrim(failure_message)) BETWEEN 1 AND 1000 AND failure_message !~ '[[:cntrl:]]')",
            name="ck_knowledge_documents_failure_message",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND processing_started_at IS NULL AND processing_completed_at IS NULL AND failure_code IS NULL AND failure_message IS NULL) OR (status = 'PROCESSING' AND processing_started_at IS NOT NULL AND processing_completed_at IS NULL AND failure_code IS NULL AND failure_message IS NULL) OR (status = 'READY' AND processing_started_at IS NOT NULL AND processing_completed_at IS NOT NULL AND failure_code IS NULL AND failure_message IS NULL) OR (status = 'FAILED' AND processing_started_at IS NOT NULL AND processing_completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="ck_knowledge_documents_processing_metadata",
        ),
        UniqueConstraint(
            "business_id", "content_sha256", name="uq_knowledge_documents_business_hash"
        ),
        UniqueConstraint(
            "id", "business_id", name="uq_knowledge_documents_id_business"
        ),
        ForeignKeyConstraint(
            ["replaces_document_id", "business_id"],
            ["knowledge_documents.id", "knowledge_documents.business_id"],
            name="fk_knowledge_documents_replacement_same_business",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_knowledge_documents_business_status_created",
            "business_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_knowledge_documents_business_updated", "business_id", "updated_at", "id"
        ),
        Index("ix_knowledge_documents_replaces", "replaces_document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[KnowledgeDocumentStatus] = mapped_column(
        knowledge_document_status_enum,
        nullable=False,
        default=KnowledgeDocumentStatus.PENDING,
        server_default=text("'PENDING'::knowledge_document_status"),
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(1000))
    page_count: Mapped[int | None] = mapped_column(Integer)
    replaces_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    processing_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
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

    business: Mapped[Business] = relationship(back_populates="knowledge_documents")
    chunks: Mapped[list[KnowledgeDocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class KnowledgeDocumentChunk(Base):
    """Traceable normalized text and optional future embedding for one document."""

    __tablename__ = "knowledge_document_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_knowledge_document_chunks_index"),
        CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 100000",
            name="ck_knowledge_document_chunks_content",
        ),
        CheckConstraint(
            "character_count > 0 AND character_count = char_length(content)",
            name="ck_knowledge_document_chunks_character_count",
        ),
        CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR (page_start > 0 AND page_end >= page_start)",
            name="ck_knowledge_document_chunks_pages",
        ),
        CheckConstraint(
            "section_title IS NULL OR char_length(btrim(section_title)) BETWEEN 1 AND 500",
            name="ck_knowledge_document_chunks_section",
        ),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL) OR (embedding IS NOT NULL AND char_length(btrim(embedding_model)) BETWEEN 1 AND 255 AND embedded_at IS NOT NULL)",
            name="ck_knowledge_document_chunks_embedding_metadata",
        ),
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_knowledge_document_chunks_order"
        ),
        ForeignKeyConstraint(
            ["document_id", "business_id"],
            ["knowledge_documents.id", "knowledge_documents.business_id"],
            name="fk_knowledge_document_chunks_document_same_business",
            ondelete="CASCADE",
        ),
        Index(
            "ix_knowledge_document_chunks_business_document_order",
            "business_id",
            "document_id",
            "chunk_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(500))
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")

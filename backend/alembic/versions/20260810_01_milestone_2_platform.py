"""Create Milestone 2 PostgreSQL platform infrastructure.

Revision ID: 20260810_01
Revises:
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

account_status = postgresql.ENUM(
    "active", "disabled", name="account_status", create_type=False
)
default_language = postgresql.ENUM(
    "ar", "en", name="default_language", create_type=False
)
tool_call_status = postgresql.ENUM(
    "success", "error", "denied", name="tool_call_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    account_status.create(bind, checkfirst=True)
    default_language.create(bind, checkfirst=True)
    tool_call_status.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "status", account_status, server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(email) <> ''", name="ck_users_email_not_blank"),
        sa.CheckConstraint(
            "btrim(first_name) <> ''", name="ck_users_first_name_not_blank"
        ),
        sa.CheckConstraint(
            "btrim(last_name) <> ''", name="ck_users_last_name_not_blank"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_users_email_ci", "users", [sa.text("lower(email)")], unique=True
    )

    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("industry", sa.String(150)),
        sa.Column("country", sa.String(2), server_default="LB", nullable=False),
        sa.Column(
            "timezone", sa.String(100), server_default="Asia/Beirut", nullable=False
        ),
        sa.Column(
            "default_language",
            default_language,
            server_default=sa.text("'ar'"),
            nullable=False,
        ),
        sa.Column("governorate", sa.String(100)),
        sa.Column("city", sa.String(150)),
        sa.Column("address_line", sa.Text()),
        sa.Column(
            "status",
            account_status,
            server_default=sa.text("'disabled'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_businesses_name_not_blank"),
        sa.CheckConstraint(
            "btrim(normalized_name) <> ''",
            name="ck_businesses_normalized_name_not_blank",
        ),
        sa.CheckConstraint(
            "char_length(country) = 2", name="ck_businesses_country_length"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "business_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", account_status, server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", name="uq_memberships_business"),
        sa.UniqueConstraint(
            "user_id", "business_id", name="uq_memberships_user_business"
        ),
    )

    op.create_table(
        "business_opening_days",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "day_of_week BETWEEN 0 AND 6", name="ck_opening_days_weekday"
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "day_of_week", name="uq_opening_days_business_day"
        ),
    )

    op.create_table(
        "business_opening_shifts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opening_day_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("closes_at", sa.Time(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "opens_at <> closes_at", name="ck_opening_shifts_distinct_times"
        ),
        sa.ForeignKeyConstraint(
            ["opening_day_id"], ["business_opening_days.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "tool_call_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("status", tool_call_status, nullable=False),
        sa.Column("error_code", sa.String(200)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "args_hash ~ '^[0-9a-f]{64}$'", name="ck_tool_logs_args_hash_format"
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_tool_logs_latency_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'success' AND error_code IS NULL) OR "
            "(status IN ('error', 'denied') AND error_code IS NOT NULL AND "
            "btrim(error_code) <> '' AND "
            "error_code ~ '^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)*$')",
            name="ck_tool_logs_status_error_code",
        ),
        sa.CheckConstraint(
            "btrim(tool_name) <> ''", name="ck_tool_logs_tool_name_not_blank"
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_logs_business_created",
        "tool_call_logs",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_tool_logs_tool_status", "tool_call_logs", ["tool_name", "status"]
    )
    op.create_index(
        "ix_tool_logs_non_success",
        "tool_call_logs",
        ["created_at"],
        postgresql_where=sa.text("status <> 'success'::tool_call_status"),
    )

    _create_database_functions_and_triggers()


def _create_database_functions_and_triggers() -> None:
    op.execute(
        r"""
        CREATE FUNCTION sou2ai_normalize_user() RETURNS trigger AS $$
        BEGIN
            NEW.email := lower(btrim(NEW.email));
            NEW.first_name := btrim(NEW.first_name);
            NEW.last_name := btrim(NEW.last_name);
            IF TG_OP = 'UPDATE' THEN NEW.updated_at := clock_timestamp(); END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_users_normalize
        BEFORE INSERT OR UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION sou2ai_normalize_user();

        CREATE FUNCTION sou2ai_normalize_business() RETURNS trigger AS $$
        BEGIN
            NEW.name := regexp_replace(btrim(NEW.name), '\s+', ' ', 'g');
            NEW.normalized_name := lower(NEW.name);
            IF TG_OP = 'UPDATE' THEN NEW.updated_at := clock_timestamp(); END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_a_businesses_normalize
        BEFORE INSERT OR UPDATE ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_normalize_business();
        """
    )
    op.execute(
        """
        CREATE FUNCTION sou2ai_enforce_owner_business_name() RETURNS trigger AS $$
        DECLARE target_name text;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.user_id::text, 0));
            SELECT normalized_name INTO target_name FROM businesses WHERE id = NEW.business_id;
            IF EXISTS (
                SELECT 1 FROM business_memberships membership
                JOIN businesses business ON business.id = membership.business_id
                WHERE membership.user_id = NEW.user_id
                  AND business.normalized_name = target_name
                  AND membership.business_id <> NEW.business_id
            ) THEN
                RAISE EXCEPTION 'A user cannot own businesses with equivalent names.'
                    USING ERRCODE = '23505';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_memberships_owner_business_name
        BEFORE INSERT OR UPDATE OF user_id, business_id ON business_memberships
        FOR EACH ROW EXECUTE FUNCTION sou2ai_enforce_owner_business_name();

        CREATE FUNCTION sou2ai_enforce_business_rename() RETURNS trigger AS $$
        DECLARE owner_id uuid;
        BEGIN
            SELECT user_id INTO owner_id FROM business_memberships
            WHERE business_id = NEW.id;
            IF owner_id IS NOT NULL THEN
                PERFORM pg_advisory_xact_lock(hashtextextended(owner_id::text, 0));
                IF EXISTS (
                    SELECT 1 FROM business_memberships membership
                    JOIN businesses business ON business.id = membership.business_id
                    WHERE membership.user_id = owner_id
                      AND membership.business_id <> NEW.id
                      AND business.normalized_name = NEW.normalized_name
                ) THEN
                    RAISE EXCEPTION 'A user cannot own businesses with equivalent names.'
                        USING ERRCODE = '23505';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_b_businesses_owner_name
        BEFORE UPDATE OF name ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_enforce_business_rename();
        """
    )
    op.execute(
        """
        CREATE FUNCTION sou2ai_business_profile_complete(target_business uuid)
        RETURNS boolean AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM businesses
                WHERE id = target_business
                  AND NULLIF(btrim(description), '') IS NOT NULL
                  AND NULLIF(btrim(industry), '') IS NOT NULL
                  AND NULLIF(btrim(governorate), '') IS NOT NULL
                  AND NULLIF(btrim(city), '') IS NOT NULL
                  AND NULLIF(btrim(address_line), '') IS NOT NULL
                  AND default_language IN ('ar', 'en')
            ) THEN RETURN false; END IF;

            IF (SELECT count(*) FROM business_opening_days
                WHERE business_id = target_business) <> 7 THEN RETURN false; END IF;

            IF EXISTS (
                SELECT 1 FROM business_opening_days day
                LEFT JOIN business_opening_shifts shift ON shift.opening_day_id = day.id
                WHERE day.business_id = target_business
                GROUP BY day.id, day.is_open
                HAVING (NOT day.is_open AND count(shift.id) <> 0)
                    OR (day.is_open AND count(shift.id) NOT BETWEEN 1 AND 3)
            ) THEN RETURN false; END IF;

            IF EXISTS (
                WITH intervals AS (
                    SELECT shift.id, shift.opening_day_id,
                        extract(hour FROM shift.opens_at)::integer * 60
                            + extract(minute FROM shift.opens_at)::integer AS starts,
                        extract(hour FROM shift.opens_at)::integer * 60
                            + extract(minute FROM shift.opens_at)::integer
                            + CASE WHEN shift.closes_at < shift.opens_at THEN 1440 ELSE 0 END
                            + extract(hour FROM shift.closes_at)::integer * 60
                            + extract(minute FROM shift.closes_at)::integer
                            - extract(hour FROM shift.opens_at)::integer * 60
                            - extract(minute FROM shift.opens_at)::integer AS ends
                    FROM business_opening_shifts shift
                    JOIN business_opening_days day ON day.id = shift.opening_day_id
                    WHERE day.business_id = target_business
                )
                SELECT 1 FROM intervals left_shift
                JOIN intervals right_shift
                  ON left_shift.opening_day_id = right_shift.opening_day_id
                 AND left_shift.id < right_shift.id
                CROSS JOIN generate_series(-1, 1) offset_day
                WHERE greatest(left_shift.starts, right_shift.starts + 1440 * offset_day)
                   <= least(left_shift.ends, right_shift.ends + 1440 * offset_day)
            ) THEN RETURN false; END IF;

            RETURN true;
        END;
        $$ LANGUAGE plpgsql STABLE;

        CREATE FUNCTION sou2ai_guard_business_activation() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'disabled' AND NEW.status = 'active'
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'Business profile must be complete before activation.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_activation
        AFTER UPDATE OF status ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_business_activation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION sou2ai_guard_active_schedule() RETURNS trigger AS $$
        DECLARE target_business uuid;
        BEGIN
            IF TG_TABLE_NAME = 'business_opening_days' THEN
                target_business := COALESCE(NEW.business_id, OLD.business_id);
            ELSE
                SELECT business_id INTO target_business FROM business_opening_days
                WHERE id = COALESCE(NEW.opening_day_id, OLD.opening_day_id);
            END IF;
            IF target_business IS NOT NULL
               AND EXISTS (SELECT 1 FROM businesses
                           WHERE id = target_business AND status = 'active')
               AND NOT sou2ai_business_profile_complete(target_business) THEN
                RAISE EXCEPTION 'An active business must retain a valid profile.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_opening_days_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_days
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();

        CREATE CONSTRAINT TRIGGER trg_opening_shifts_active_profile
        AFTER INSERT OR UPDATE OR DELETE ON business_opening_shifts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_active_schedule();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_shifts_active_profile ON business_opening_shifts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_days_active_profile ON business_opening_days"
    )
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_active_schedule()")
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_activation ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_business_activation()")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_business_profile_complete(uuid)")
    op.execute("DROP TRIGGER IF EXISTS trg_b_businesses_owner_name ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_enforce_business_rename()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_memberships_owner_business_name ON business_memberships"
    )
    op.execute("DROP FUNCTION IF EXISTS sou2ai_enforce_owner_business_name()")
    op.execute("DROP TRIGGER IF EXISTS trg_a_businesses_normalize ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_normalize_business()")
    op.execute("DROP TRIGGER IF EXISTS trg_users_normalize ON users")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_normalize_user()")

    op.drop_table("tool_call_logs")
    op.drop_table("business_opening_shifts")
    op.drop_table("business_opening_days")
    op.drop_table("business_memberships")
    op.drop_table("businesses")
    op.drop_table("users")

    tool_call_status.drop(op.get_bind(), checkfirst=True)
    default_language.drop(op.get_bind(), checkfirst=True)
    account_status.drop(op.get_bind(), checkfirst=True)

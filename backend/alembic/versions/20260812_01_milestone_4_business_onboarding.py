"""Implement Milestone 4 business management and onboarding.

Revision ID: 20260812_01
Revises: 20260811_03
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_01"
down_revision: str | None = "20260811_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

business_status = postgresql.ENUM("PENDING", name="business_status", create_type=False)
membership_permission = postgresql.ENUM(
    "FULL_ACCESS", name="membership_permission", create_type=False
)
business_category = postgresql.ENUM(
    "GROCERY_SUPERMARKET",
    "BAKERY",
    "RESTAURANT",
    "CAFE",
    "CLOTHING",
    "ELECTRONICS",
    "PHARMACY",
    "BEAUTY_COSMETICS",
    "HOME_FURNITURE",
    "SERVICES",
    "OTHER",
    name="business_category",
    create_type=False,
)


def _drop_milestone_2_business_functions() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_shifts_active_profile ON business_opening_shifts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_opening_days_active_profile ON business_opening_days"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_activation ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_active_schedule()")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_guard_business_activation()")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_business_profile_complete(uuid)")
    op.execute("DROP TRIGGER IF EXISTS trg_b_businesses_owner_name ON businesses")
    op.execute("DROP FUNCTION IF EXISTS sou2ai_enforce_business_rename()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_memberships_owner_business_name ON business_memberships"
    )
    op.execute("DROP FUNCTION IF EXISTS sou2ai_enforce_owner_business_name()")


def _create_milestone_4_profile_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sou2ai_business_profile_complete(target_business uuid)
        RETURNS boolean AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM businesses
                WHERE id = target_business
                  AND char_length(btrim(name)) BETWEEN 2 AND 120
                  AND char_length(btrim(description)) BETWEEN 20 AND 2000
                  AND category IS NOT NULL
                  AND ((category = 'OTHER' AND
                        char_length(btrim(custom_category)) BETWEEN 2 AND 100)
                       OR (category <> 'OTHER' AND custom_category IS NULL))
                  AND governorate IS NOT NULL
                  AND district IS NOT NULL
                  AND city IS NOT NULL
                  AND char_length(btrim(address_line)) BETWEEN 5 AND 255
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
                SELECT 1 FROM business_opening_shifts left_shift
                JOIN business_opening_shifts right_shift
                  ON left_shift.opening_day_id = right_shift.opening_day_id
                 AND left_shift.id < right_shift.id
                 AND left_shift.opens_at < right_shift.closes_at
                 AND right_shift.opens_at < left_shift.closes_at
                JOIN business_opening_days day ON day.id = left_shift.opening_day_id
                WHERE day.business_id = target_business
            ) THEN RETURN false; END IF;
            RETURN true;
        END;
        $$ LANGUAGE plpgsql STABLE;

        CREATE FUNCTION sou2ai_guard_business_activation() RETURNS trigger AS $$
        BEGIN
            IF NOT OLD.is_active AND NEW.is_active
               AND NOT sou2ai_business_profile_complete(NEW.id) THEN
                RAISE EXCEPTION 'Business profile must be complete before activation.'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_businesses_activation
        AFTER UPDATE OF is_active ON businesses
        FOR EACH ROW EXECUTE FUNCTION sou2ai_guard_business_activation();

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
                           WHERE id = target_business AND is_active)
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


def _restore_milestone_2_business_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION sou2ai_enforce_owner_business_name() RETURNS trigger AS $$
        DECLARE target_name text;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.user_id::text, 0));
            SELECT normalized_name INTO target_name FROM businesses
            WHERE id = NEW.business_id;
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


def upgrade() -> None:
    bind = op.get_bind()
    business_status.create(bind, checkfirst=True)
    membership_permission.create(bind, checkfirst=True)
    business_category.create(bind, checkfirst=True)
    _drop_milestone_2_business_functions()

    op.add_column(
        "businesses",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE businesses business SET owner_user_id = membership.user_id
        FROM business_memberships membership
        WHERE membership.business_id = business.id
        """
    )
    op.alter_column("businesses", "owner_user_id", nullable=False)
    op.create_foreign_key(
        "fk_businesses_owner_user",
        "businesses",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_column("businesses", "industry")
    op.drop_column("businesses", "default_language")
    op.add_column("businesses", sa.Column("category", business_category))
    op.add_column("businesses", sa.Column("custom_category", sa.String(100)))
    op.add_column("businesses", sa.Column("district", sa.String(100)))
    op.add_column(
        "businesses",
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE businesses SET is_active = true WHERE status = 'active'")
    op.add_column(
        "businesses",
        sa.Column("onboarding_submitted_at", sa.DateTime(timezone=True)),
    )
    op.drop_column("businesses", "status")
    op.add_column(
        "businesses",
        sa.Column(
            "status",
            business_status,
            server_default=sa.text("'PENDING'::business_status"),
            nullable=False,
        ),
    )
    op.alter_column("businesses", "name", type_=sa.String(120))
    op.alter_column("businesses", "normalized_name", type_=sa.String(120))
    op.alter_column("businesses", "address_line", type_=sa.String(255))
    op.create_unique_constraint(
        "uq_businesses_owner_name",
        "businesses",
        ["owner_user_id", "normalized_name"],
    )
    op.create_check_constraint(
        "ck_businesses_name_length",
        "businesses",
        "char_length(name) BETWEEN 2 AND 120",
    )
    op.create_check_constraint(
        "ck_businesses_description_length",
        "businesses",
        "description IS NULL OR char_length(btrim(description)) BETWEEN 20 AND 2000",
    )
    op.create_check_constraint(
        "ck_businesses_custom_category_length",
        "businesses",
        "custom_category IS NULL OR char_length(btrim(custom_category)) BETWEEN 2 AND 100",
    )
    op.create_check_constraint(
        "ck_businesses_address_length",
        "businesses",
        "address_line IS NULL OR char_length(btrim(address_line)) BETWEEN 5 AND 255",
    )
    op.create_check_constraint(
        "ck_businesses_custom_category_rule",
        "businesses",
        "(category = 'OTHER' AND custom_category IS NOT NULL) OR "
        "(category IS DISTINCT FROM 'OTHER' AND custom_category IS NULL)",
    )
    op.create_check_constraint(
        "ck_businesses_governorate",
        "businesses",
        "governorate IS NULL OR governorate IN ('Beirut', 'Mount Lebanon', 'North', "
        "'Akkar', 'Bekaa', 'Baalbek-Hermel', 'South', 'Nabatieh')",
    )
    op.create_check_constraint(
        "ck_businesses_district",
        "businesses",
        "district IS NULL OR district IN ('Beirut','Baabda','Aley','Metn','Keserwan','Chouf','Tripoli','Zgharta','Koura','Akkar','Zahle','West Bekaa','Baalbek','Hermel','Saida','Jezzine','Nabatieh','Bint Jbeil','Marjayoun')",
    )
    op.create_check_constraint(
        "ck_businesses_city",
        "businesses",
        "city IS NULL OR city IN ('Beirut','Baabda','Hazmieh','Aley','Choueifat','Antelias','Jdeideh','Sin El Fil','Dekwaneh','Baouchrieh','Jounieh','Zouk Mikael','Kaslik','Beiteddine','Damour','Deir El Qamar','Tripoli','Mina','Zgharta','Ehden','Amioun','Halba','Zahle','Chtaura','Jeb Jennine','Qab Elias','Baalbek','Hermel','Saida','Abra','Ghaziyeh','Jezzine','Nabatieh','Kfar Roummane','Bint Jbeil','Marjayoun','Khiam')",
    )
    op.create_check_constraint(
        "ck_businesses_location_hierarchy",
        "businesses",
        "(governorate IS NULL OR district IS NULL OR city IS NULL) OR "
        "(governorate = 'Beirut' AND district = 'Beirut' AND city = 'Beirut') OR "
        "(governorate = 'Mount Lebanon' AND ((district = 'Baabda' AND city IN ('Baabda','Hazmieh')) OR (district = 'Aley' AND city IN ('Aley','Choueifat')) OR (district = 'Metn' AND city IN ('Antelias','Jdeideh','Sin El Fil','Dekwaneh','Baouchrieh')) OR (district = 'Keserwan' AND city IN ('Jounieh','Zouk Mikael','Kaslik')) OR (district = 'Chouf' AND city IN ('Beiteddine','Damour','Deir El Qamar')))) OR "
        "(governorate = 'North' AND ((district = 'Tripoli' AND city IN ('Tripoli','Mina')) OR (district = 'Zgharta' AND city IN ('Zgharta','Ehden')) OR (district = 'Koura' AND city = 'Amioun'))) OR "
        "(governorate = 'Akkar' AND district = 'Akkar' AND city = 'Halba') OR "
        "(governorate = 'Bekaa' AND ((district = 'Zahle' AND city IN ('Zahle','Chtaura')) OR (district = 'West Bekaa' AND city IN ('Jeb Jennine','Qab Elias')))) OR "
        "(governorate = 'Baalbek-Hermel' AND ((district = 'Baalbek' AND city = 'Baalbek') OR (district = 'Hermel' AND city = 'Hermel'))) OR "
        "(governorate = 'South' AND ((district = 'Saida' AND city IN ('Saida','Abra','Ghaziyeh')) OR (district = 'Jezzine' AND city = 'Jezzine'))) OR "
        "(governorate = 'Nabatieh' AND ((district = 'Nabatieh' AND city IN ('Nabatieh','Kfar Roummane')) OR (district = 'Bint Jbeil' AND city = 'Bint Jbeil') OR (district = 'Marjayoun' AND city IN ('Marjayoun','Khiam'))))",
    )

    op.drop_constraint(
        "uq_memberships_business", "business_memberships", type_="unique"
    )
    op.drop_column("business_memberships", "status")
    op.add_column(
        "business_memberships",
        sa.Column(
            "permission",
            membership_permission,
            server_default=sa.text("'FULL_ACCESS'::membership_permission"),
            nullable=False,
        ),
    )
    op.create_index("ix_memberships_business", "business_memberships", ["business_id"])

    op.drop_constraint(
        "ck_opening_shifts_distinct_times",
        "business_opening_shifts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_opening_shifts_ordered_times",
        "business_opening_shifts",
        "opens_at < closes_at",
    )
    op.create_unique_constraint(
        "uq_opening_shifts_day_interval",
        "business_opening_shifts",
        ["opening_day_id", "opens_at", "closes_at"],
    )
    _create_milestone_4_profile_functions()


def downgrade() -> None:
    _drop_milestone_2_business_functions()
    op.drop_constraint(
        "uq_opening_shifts_day_interval",
        "business_opening_shifts",
        type_="unique",
    )
    op.drop_constraint(
        "ck_opening_shifts_ordered_times",
        "business_opening_shifts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_opening_shifts_distinct_times",
        "business_opening_shifts",
        "opens_at <> closes_at",
    )

    op.drop_index("ix_memberships_business", table_name="business_memberships")
    op.drop_column("business_memberships", "permission")
    op.add_column(
        "business_memberships",
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "disabled", name="account_status", create_type=False
            ),
            server_default=sa.text("'active'::account_status"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_memberships_business", "business_memberships", ["business_id"]
    )

    for constraint in (
        "ck_businesses_location_hierarchy",
        "ck_businesses_city",
        "ck_businesses_district",
        "ck_businesses_governorate",
        "ck_businesses_custom_category_rule",
        "ck_businesses_address_length",
        "ck_businesses_custom_category_length",
        "ck_businesses_description_length",
        "ck_businesses_name_length",
        "uq_businesses_owner_name",
    ):
        op.drop_constraint(
            constraint,
            "businesses",
            type_="unique" if constraint.startswith("uq_") else "check",
        )
    op.drop_column("businesses", "status")
    op.add_column(
        "businesses",
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "disabled", name="account_status", create_type=False
            ),
            server_default=sa.text("'disabled'::account_status"),
            nullable=False,
        ),
    )
    op.execute("UPDATE businesses SET status = 'active' WHERE is_active")
    op.alter_column("businesses", "name", type_=sa.String(200))
    op.alter_column("businesses", "normalized_name", type_=sa.String(200))
    op.alter_column("businesses", "address_line", type_=sa.Text())
    op.add_column("businesses", sa.Column("industry", sa.String(150)))
    op.add_column(
        "businesses",
        sa.Column(
            "default_language",
            postgresql.ENUM("ar", "en", name="default_language", create_type=False),
            server_default=sa.text("'ar'::default_language"),
            nullable=False,
        ),
    )
    op.drop_column("businesses", "onboarding_submitted_at")
    op.drop_column("businesses", "is_active")
    op.drop_column("businesses", "district")
    op.drop_column("businesses", "custom_category")
    op.drop_column("businesses", "category")
    op.drop_constraint("fk_businesses_owner_user", "businesses", type_="foreignkey")
    op.drop_column("businesses", "owner_user_id")

    _restore_milestone_2_business_functions()
    business_category.drop(op.get_bind(), checkfirst=True)
    membership_permission.drop(op.get_bind(), checkfirst=True)
    business_status.drop(op.get_bind(), checkfirst=True)

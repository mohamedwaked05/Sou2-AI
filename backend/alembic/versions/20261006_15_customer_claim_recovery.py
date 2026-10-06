"""Recover expired customer consumption through the restricted budget boundary."""

from alembic import op

revision = "20261006_15"
down_revision = "20261005_14"
branch_labels = None
depends_on = None

FUNCTION = "public.sou2ai_recover_customer_usage(uuid)"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_recover_customer_usage(target_message_id uuid)
        RETURNS void AS $function$
        DECLARE
            message_record record;
            reservation_id uuid;
        BEGIN
            SELECT message.* INTO message_record
            FROM public.customer_messages AS message
            WHERE message.id = target_message_id FOR UPDATE;
            IF NOT FOUND OR message_record.direction <> 'inbound'
               OR message_record.status <> 'PROCESSING'
               OR message_record.claim_expires_at IS NULL
               OR message_record.claim_expires_at > pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'Customer claim is not expired.' USING ERRCODE = '22023';
            END IF;
            SELECT reservation.id INTO reservation_id
            FROM public.ai_usage_reservations AS reservation
            WHERE reservation.customer_message_id = target_message_id
              AND reservation.business_id = message_record.business_id
              AND reservation.channel = 'whatsapp'
              AND reservation.capability = 'customer_chat'
              AND reservation.status = 'reserved';
            IF FOUND THEN
                PERFORM public.sou2ai_reconcile_ai_usage(
                    reservation_id, NULL, NULL, false, NULL, NULL, 'uncertain'
                );
            END IF;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO sou2ai_migrator")
    op.execute(
        f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC, sou2ai_lifecycle_operator"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO sou2ai_runtime")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {FUNCTION}")

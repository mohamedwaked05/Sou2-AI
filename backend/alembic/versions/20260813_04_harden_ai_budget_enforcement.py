"""Harden rate admission, retention, and AI budget authorization.

Revision ID: 20260813_04
Revises: 20260813_03
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260813_04"
down_revision: str | None = "20260813_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATOR_ROLE = "sou2ai_migrator"
RUNTIME_ROLE = "sou2ai_runtime"
OPERATOR_ROLE = "sou2ai_lifecycle_operator"

REGISTRATION_FUNCTION = "public.sou2ai_admit_registration_attempt(text, text)"
OWNER_ADMISSION_FUNCTION = (
    "public.sou2ai_admit_owner_chat_generation(uuid, uuid, integer)"
)
OWNER_UNDO_FUNCTION = (
    "public.sou2ai_undo_owner_chat_generation_admission(uuid, uuid, integer, uuid)"
)
NEW_CLEANUP_FUNCTION = "public.sou2ai_cleanup_security_records(integer)"
OLD_CLEANUP_FUNCTION = "public.sou2ai_cleanup_security_records(timestamptz, integer)"


def _create_registration_admission() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_admit_registration_attempt(
            target_normalized_email text,
            target_client_ip text
        ) RETURNS TABLE(
            admitted boolean,
            limit_code text,
            retry_after_seconds integer,
            reset_at timestamptz
        ) AS $function$
        DECLARE
            database_now timestamptz := pg_catalog.clock_timestamp();
            clean_email text := pg_catalog.btrim(target_normalized_email);
            clean_client_ip text := pg_catalog.btrim(target_client_ip);
            email_scope text;
            ip_scope text;
            lock_scope text;
            event_count bigint;
            oldest_event timestamptz;
            blocked_reset_at timestamptz;
        BEGIN
            IF clean_email IS NULL
               OR pg_catalog.char_length(clean_email) NOT BETWEEN 1 AND 320
               OR clean_client_ip IS NULL
               OR pg_catalog.char_length(clean_client_ip) NOT BETWEEN 1 AND 45 THEN
                RAISE EXCEPTION 'Invalid registration admission request.'
                    USING ERRCODE = '22023';
            END IF;

            email_scope := 'registration:email:' || clean_email;
            ip_scope := 'registration:ip:' || clean_client_ip;
            FOR lock_scope IN
                SELECT scope.value
                FROM pg_catalog.unnest(ARRAY[email_scope, ip_scope]) AS scope(value)
                ORDER BY scope.value
            LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(lock_scope, 0)
                );
            END LOOP;

            SELECT pg_catalog.count(*), pg_catalog.min(event.created_at)
            INTO event_count, oldest_event
            FROM public.registration_rate_limit_events AS event
            WHERE event.normalized_email = clean_email
              AND event.created_at >= database_now - interval '1 hour';
            IF event_count >= 5 THEN
                blocked_reset_at := oldest_event + interval '1 hour';
                RETURN QUERY SELECT false, 'registration_email_rate_limited'::text,
                    GREATEST(
                        1,
                        pg_catalog.ceil(
                            EXTRACT(
                                epoch FROM blocked_reset_at - database_now
                            )
                        )::integer
                    ),
                    blocked_reset_at;
                RETURN;
            END IF;

            SELECT pg_catalog.count(*), pg_catalog.min(event.created_at)
            INTO event_count, oldest_event
            FROM public.registration_rate_limit_events AS event
            WHERE event.client_ip = clean_client_ip
              AND event.created_at >= database_now - interval '15 minutes';
            IF event_count >= 30 THEN
                blocked_reset_at := oldest_event + interval '15 minutes';
                RETURN QUERY SELECT false, 'registration_ip_rate_limited'::text,
                    GREATEST(
                        1,
                        pg_catalog.ceil(
                            EXTRACT(
                                epoch FROM blocked_reset_at - database_now
                            )
                        )::integer
                    ),
                    blocked_reset_at;
                RETURN;
            END IF;

            SELECT pg_catalog.count(*), pg_catalog.min(event.created_at)
            INTO event_count, oldest_event
            FROM public.registration_rate_limit_events AS event
            WHERE event.client_ip = clean_client_ip
              AND event.created_at >= database_now - interval '24 hours';
            IF event_count >= 100 THEN
                blocked_reset_at := oldest_event + interval '24 hours';
                RETURN QUERY SELECT false,
                    'registration_ip_daily_rate_limited'::text,
                    GREATEST(
                        1,
                        pg_catalog.ceil(
                            EXTRACT(
                                epoch FROM blocked_reset_at - database_now
                            )
                        )::integer
                    ),
                    blocked_reset_at;
                RETURN;
            END IF;

            INSERT INTO public.registration_rate_limit_events (
                id, normalized_email, client_ip, created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), clean_email, clean_client_ip,
                database_now
            );
            RETURN QUERY SELECT true, NULL::text, NULL::integer,
                                NULL::timestamptz;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _create_owner_admission() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_admit_owner_chat_generation(
            target_business_id uuid,
            target_owner_message_id uuid,
            target_generation_attempt integer
        ) RETURNS TABLE(
            admitted boolean,
            already_recorded boolean,
            retry_after_seconds integer,
            reset_at timestamptz
        ) AS $function$
        DECLARE
            database_now timestamptz := pg_catalog.clock_timestamp();
            event_count bigint;
            oldest_event timestamptz;
            blocked_reset_at timestamptz;
            current_attempt integer;
        BEGIN
            IF target_generation_attempt < 1 THEN
                RAISE EXCEPTION 'Invalid owner generation admission request.'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'owner-chat-generation:' || target_business_id::text, 0
                )
            );

            SELECT message.generation_attempts INTO current_attempt
            FROM public.owner_chat_messages AS message
            JOIN public.owner_conversations AS conversation
              ON conversation.id = message.conversation_id
            WHERE message.id = target_owner_message_id
              AND conversation.business_id = target_business_id
              AND message.role = 'owner'
            FOR UPDATE OF message;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Owner message was not found.'
                    USING ERRCODE = 'P0002';
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.owner_chat_rate_limit_events AS event
                WHERE event.business_id = target_business_id
                  AND event.owner_message_id = target_owner_message_id
                  AND event.generation_attempt = target_generation_attempt
            ) THEN
                RETURN QUERY SELECT true, true, NULL::integer,
                                    NULL::timestamptz;
                RETURN;
            END IF;
            IF target_generation_attempt <> current_attempt + 1 THEN
                RAISE EXCEPTION 'Owner generation attempt is not current.'
                    USING ERRCODE = '23514';
            END IF;

            SELECT pg_catalog.count(*), pg_catalog.min(event.created_at)
            INTO event_count, oldest_event
            FROM public.owner_chat_rate_limit_events AS event
            WHERE event.business_id = target_business_id
              AND event.created_at >= database_now - interval '1 minute';
            IF event_count >= 3 THEN
                blocked_reset_at := oldest_event + interval '1 minute';
                RETURN QUERY SELECT false, false,
                    GREATEST(
                        1,
                        pg_catalog.ceil(
                            EXTRACT(
                                epoch FROM blocked_reset_at - database_now
                            )
                        )::integer
                    ),
                    blocked_reset_at;
                RETURN;
            END IF;

            SELECT pg_catalog.count(*), pg_catalog.min(event.created_at)
            INTO event_count, oldest_event
            FROM public.owner_chat_rate_limit_events AS event
            WHERE event.business_id = target_business_id
              AND event.created_at >= database_now - interval '1 hour';
            IF event_count >= 20 THEN
                blocked_reset_at := oldest_event + interval '1 hour';
                RETURN QUERY SELECT false, false,
                    GREATEST(
                        1,
                        pg_catalog.ceil(
                            EXTRACT(
                                epoch FROM blocked_reset_at - database_now
                            )
                        )::integer
                    ),
                    blocked_reset_at;
                RETURN;
            END IF;

            INSERT INTO public.owner_chat_rate_limit_events (
                id, business_id, owner_message_id, generation_attempt, created_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_business_id,
                target_owner_message_id, target_generation_attempt, database_now
            );
            RETURN QUERY SELECT true, false, NULL::integer, NULL::timestamptz;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _create_owner_admission_undo() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_undo_owner_chat_generation_admission(
            target_business_id uuid,
            target_owner_message_id uuid,
            target_generation_attempt integer,
            target_generation_claim_token uuid
        ) RETURNS boolean AS $function$
        DECLARE
            deleted_count integer := 0;
        BEGIN
            IF target_generation_attempt < 1
               OR target_generation_claim_token IS NULL THEN
                RAISE EXCEPTION 'Invalid owner generation undo request.'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'owner-chat-generation:' || target_business_id::text, 0
                )
            );
            PERFORM 1
            FROM public.owner_chat_messages AS message
            JOIN public.owner_conversations AS conversation
              ON conversation.id = message.conversation_id
            WHERE message.id = target_owner_message_id
              AND conversation.business_id = target_business_id
              AND message.role = 'owner'
              AND message.generation_state = 'processing'
              AND message.generation_attempts = target_generation_attempt
              AND message.generation_claim_token = target_generation_claim_token
            FOR UPDATE OF message;
            IF NOT FOUND OR EXISTS (
                SELECT 1 FROM public.ai_usage_reservations AS reservation
                WHERE reservation.owner_message_id = target_owner_message_id
                  AND reservation.generation_attempt = target_generation_attempt
            ) THEN
                RETURN false;
            END IF;

            DELETE FROM public.owner_chat_rate_limit_events AS event
            WHERE event.business_id = target_business_id
              AND event.owner_message_id = target_owner_message_id
              AND event.generation_attempt = target_generation_attempt;
            GET DIAGNOSTICS deleted_count = ROW_COUNT;
            IF deleted_count <> 1 THEN
                RETURN false;
            END IF;

            UPDATE public.owner_chat_messages AS message
            SET generation_state = 'failed',
                generation_attempts = generation_attempts - 1,
                generation_claim_token = NULL,
                generation_claim_expires_at = NULL
            WHERE message.id = target_owner_message_id;
            RETURN true;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _create_secure_cleanup() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {OLD_CLEANUP_FUNCTION}")
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_cleanup_security_records(
            target_batch_size integer
        ) RETURNS TABLE(
            owner_events_deleted integer,
            registration_events_deleted integer,
            reservations_deleted integer,
            summaries_deleted integer
        ) AS $function$
        DECLARE
            target record;
            target_now timestamptz := pg_catalog.clock_timestamp();
        BEGIN
            IF target_batch_size IS NULL
               OR target_batch_size NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION
                    'Security retention batch size must be between 1 and 1000.'
                    USING ERRCODE = '22023';
            END IF;
            FOR target IN
                SELECT DISTINCT reservation.business_id, reservation.window_start
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.status = 'reserved'
                  AND reservation.lease_expires_at <= target_now
            LOOP
                PERFORM public.sou2ai_charge_expired_ai_reservations(
                    target.business_id, target.window_start
                );
            END LOOP;

            WITH expired AS (
                SELECT event.id FROM public.owner_chat_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '24 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.owner_chat_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS owner_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT event.id FROM public.registration_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '48 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.registration_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS registration_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT reservation.id
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.created_at < target_now - interval '90 days'
                  AND reservation.status <> 'reserved'
                ORDER BY reservation.created_at, reservation.id
                LIMIT target_batch_size FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.ai_usage_reservations AS reservation
            USING expired WHERE reservation.id = expired.id;
            GET DIAGNOSTICS reservations_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT summary.id FROM public.business_ai_usage_daily AS summary
                WHERE summary.window_end < target_now - interval '12 months'
                ORDER BY summary.window_end, summary.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.business_ai_usage_daily AS summary
            USING expired WHERE summary.id = expired.id;
            GET DIAGNOSTICS summaries_deleted = ROW_COUNT;
            RETURN NEXT;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _replace_reconciliation_function(*, restore_legacy_body: bool = False) -> None:
    terminal_total = (
        "pg_catalog.coalesce(reservation_record.total_tokens, 0)"
        if restore_legacy_body
        else "COALESCE(reservation_record.total_tokens, 0)"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.sou2ai_reconcile_ai_usage(
            target_reservation_id uuid,
            target_input_tokens integer,
            target_output_tokens integer,
            target_counts_authoritative boolean,
            target_provider_identifier text,
            target_model_identifier text,
            target_outcome text
        ) RETURNS TABLE(
            charged_tokens integer,
            reconciled boolean
        ) AS $function$
        DECLARE
            reservation_record record;
            final_input integer;
            final_output integer;
            final_total integer;
            final_status text;
        BEGIN
            SELECT reservation.* INTO reservation_record
            FROM public.ai_usage_reservations AS reservation
            WHERE reservation.id = target_reservation_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'AI usage reservation was not found.'
                    USING ERRCODE = 'P0002';
            END IF;
            IF reservation_record.status <> 'reserved' THEN
                RETURN QUERY SELECT {terminal_total}, false;
                RETURN;
            END IF;
            IF target_outcome NOT IN (
                'completed', 'reported_failure', 'release', 'uncertain'
            ) THEN
                RAISE EXCEPTION 'Invalid AI usage outcome.' USING ERRCODE = '22023';
            END IF;

            IF target_outcome = 'release' THEN
                final_input := 0;
                final_output := 0;
                final_status := 'released';
            ELSIF target_outcome = 'uncertain' THEN
                final_input := reservation_record.estimated_input_tokens;
                final_output := reservation_record.max_output_tokens;
                target_counts_authoritative := false;
                final_status := 'charged';
            ELSE
                IF target_input_tokens IS NULL OR target_input_tokens < 0
                   OR target_output_tokens IS NULL OR target_output_tokens < 0 THEN
                    RAISE EXCEPTION 'Token counts must be nonnegative.'
                        USING ERRCODE = '22023';
                END IF;
                final_input := target_input_tokens;
                final_output := target_output_tokens;
                final_status := CASE WHEN target_outcome = 'completed'
                                     THEN 'completed' ELSE 'charged' END;
            END IF;
            final_total := final_input + final_output;

            UPDATE public.business_ai_usage_daily
            SET input_tokens_used = input_tokens_used + final_input,
                output_tokens_used = output_tokens_used + final_output,
                total_tokens_used = total_tokens_used + final_total,
                tokens_reserved = tokens_reserved
                    - reservation_record.reserved_tokens,
                updated_at = pg_catalog.clock_timestamp()
            WHERE business_id = reservation_record.business_id
              AND window_start = reservation_record.window_start;

            UPDATE public.ai_usage_reservations
            SET input_tokens = final_input,
                output_tokens = final_output,
                total_tokens = final_total,
                counts_authoritative = target_counts_authoritative,
                provider_identifier = CASE
                    WHEN target_provider_identifier IS NULL
                      OR pg_catalog.btrim(target_provider_identifier) = '' THEN NULL
                    ELSE pg_catalog.left(
                        pg_catalog.btrim(target_provider_identifier), 50
                    )
                END,
                model_identifier = CASE
                    WHEN target_model_identifier IS NULL
                      OR pg_catalog.btrim(target_model_identifier) = '' THEN NULL
                    ELSE pg_catalog.left(
                        pg_catalog.btrim(target_model_identifier), 100
                    )
                END,
                status = final_status,
                reconciled_at = pg_catalog.clock_timestamp()
            WHERE id = target_reservation_id;
            RETURN QUERY SELECT final_total, true;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )


def _secure_objects() -> None:
    op.execute(
        f"GRANT SELECT ON TABLE public.owner_conversations, "
        f"public.owner_chat_messages TO {MIGRATOR_ROLE}"
    )
    op.execute(
        "GRANT UPDATE (generation_state, generation_attempts, "
        "generation_claim_token, generation_claim_expires_at) "
        f"ON TABLE public.owner_chat_messages TO {MIGRATOR_ROLE}"
    )
    for table in (
        "registration_rate_limit_events",
        "owner_chat_rate_limit_events",
    ):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {OPERATOR_ROLE}")

    for function in (
        REGISTRATION_FUNCTION,
        OWNER_ADMISSION_FUNCTION,
        OWNER_UNDO_FUNCTION,
        NEW_CLEANUP_FUNCTION,
    ):
        op.execute(f"ALTER FUNCTION {function} OWNER TO {MIGRATOR_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {RUNTIME_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM {OPERATOR_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {RUNTIME_ROLE}")


def upgrade() -> None:
    _create_registration_admission()
    _create_owner_admission()
    _create_owner_admission_undo()
    _create_secure_cleanup()
    _replace_reconciliation_function()
    _secure_objects()


def _restore_legacy_cleanup() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_cleanup_security_records(
            target_now timestamptz,
            target_batch_size integer
        ) RETURNS TABLE(
            owner_events_deleted integer,
            registration_events_deleted integer,
            reservations_deleted integer,
            summaries_deleted integer
        ) AS $function$
        DECLARE
            target record;
        BEGIN
            IF target_now IS NULL OR target_batch_size < 1 THEN
                RAISE EXCEPTION 'Invalid security retention request.'
                    USING ERRCODE = '22023';
            END IF;
            FOR target IN
                SELECT DISTINCT reservation.business_id, reservation.window_start
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.status = 'reserved'
                  AND reservation.lease_expires_at <= target_now
            LOOP
                PERFORM public.sou2ai_charge_expired_ai_reservations(
                    target.business_id, target.window_start
                );
            END LOOP;

            WITH expired AS (
                SELECT event.id FROM public.owner_chat_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '24 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.owner_chat_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS owner_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT event.id FROM public.registration_rate_limit_events AS event
                WHERE event.created_at < target_now - interval '48 hours'
                ORDER BY event.created_at, event.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.registration_rate_limit_events AS event
            USING expired WHERE event.id = expired.id;
            GET DIAGNOSTICS registration_events_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT reservation.id
                FROM public.ai_usage_reservations AS reservation
                WHERE reservation.created_at < target_now - interval '90 days'
                  AND reservation.status <> 'reserved'
                ORDER BY reservation.created_at, reservation.id
                LIMIT target_batch_size FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.ai_usage_reservations AS reservation
            USING expired WHERE reservation.id = expired.id;
            GET DIAGNOSTICS reservations_deleted = ROW_COUNT;

            WITH expired AS (
                SELECT summary.id FROM public.business_ai_usage_daily AS summary
                WHERE summary.window_end < target_now - interval '12 months'
                ORDER BY summary.window_end, summary.id LIMIT target_batch_size
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM public.business_ai_usage_daily AS summary
            USING expired WHERE summary.id = expired.id;
            GET DIAGNOSTICS summaries_deleted = ROW_COUNT;
            RETURN NEXT;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        """
    )
    op.execute(f"ALTER FUNCTION {OLD_CLEANUP_FUNCTION} OWNER TO {MIGRATOR_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {OLD_CLEANUP_FUNCTION} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {OLD_CLEANUP_FUNCTION} FROM {OPERATOR_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {OLD_CLEANUP_FUNCTION} TO {RUNTIME_ROLE}")


def downgrade() -> None:
    for function in (
        NEW_CLEANUP_FUNCTION,
        OWNER_UNDO_FUNCTION,
        OWNER_ADMISSION_FUNCTION,
        REGISTRATION_FUNCTION,
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    _replace_reconciliation_function(restore_legacy_body=True)
    _restore_legacy_cleanup()
    op.execute(
        "REVOKE UPDATE (generation_state, generation_attempts, "
        "generation_claim_token, generation_claim_expires_at) "
        f"ON TABLE public.owner_chat_messages FROM {MIGRATOR_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE public.owner_conversations, "
        f"public.owner_chat_messages FROM {MIGRATOR_ROLE}"
    )
    for table in (
        "registration_rate_limit_events",
        "owner_chat_rate_limit_events",
    ):
        op.execute(
            f"GRANT SELECT, INSERT, DELETE ON TABLE public.{table} TO {RUNTIME_ROLE}"
        )

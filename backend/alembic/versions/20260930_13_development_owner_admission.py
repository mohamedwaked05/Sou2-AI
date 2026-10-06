"""Opt-in local development admission, keeping production defaults unchanged."""

from alembic import op

revision = "20260930_13"
down_revision = "20260930_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_development_admission()
    op.execute(
        "ALTER FUNCTION public.sou2ai_admit_development_owner_chat_generation(uuid, uuid, integer, integer, integer) OWNER TO sou2ai_migrator"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sou2ai_admit_development_owner_chat_generation(uuid, uuid, integer, integer, integer) FROM PUBLIC, sou2ai_lifecycle_operator"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.sou2ai_admit_development_owner_chat_generation(uuid, uuid, integer, integer, integer) TO sou2ai_runtime"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION public.sou2ai_admit_development_owner_chat_generation(uuid, uuid, integer, integer, integer)"
    )


def _create_development_admission() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sou2ai_admit_development_owner_chat_generation(
            target_business_id uuid,
            target_owner_message_id uuid,
            target_generation_attempt integer,
            minute_limit integer,
            hour_limit integer
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
            IF pg_catalog.current_database() NOT IN ('sou2ai_dev', 'sou2ai_test')
               OR minute_limit IS NULL OR minute_limit NOT BETWEEN 3 AND 120
               OR hour_limit IS NULL OR hour_limit NOT BETWEEN 20 AND 2000 THEN
                RAISE EXCEPTION 'Development owner admission is restricted to local databases.'
                    USING ERRCODE = '42501';
            END IF;
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
            IF event_count >= minute_limit THEN
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
            IF event_count >= hour_limit THEN
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

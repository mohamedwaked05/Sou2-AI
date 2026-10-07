"""Versioned catalogue mapping review and capability-specific sources."""

from alembic import op

revision = "20261007_16"
down_revision = "20261006_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.operational_data_sources
          DROP CONSTRAINT ck_operational_sources_adapter,
          DROP CONSTRAINT ck_operational_sources_connection_profile,
          DROP CONSTRAINT ck_operational_sources_mapping_profile;
        ALTER TABLE public.operational_data_sources
          ADD CONSTRAINT ck_operational_sources_adapter CHECK
            (adapter_type IN ('postgresql_readonly', 'sqlserver_readonly')),
          ADD CONSTRAINT ck_operational_sources_connection_profile CHECK
            (connection_profile_key ~ '^[a-z][a-z0-9_]{1,99}$'),
          ADD CONSTRAINT ck_operational_sources_mapping_profile CHECK
            (mapping_profile_key IN ('fake_store_minimarket', 'discovered_products')
             AND mapping_profile_version = 1);
        DROP INDEX public.uq_operational_sources_active_type;
        CREATE UNIQUE INDEX uq_operational_sources_active_business
          ON public.operational_data_sources(business_id)
          WHERE status = 'ACTIVE'::public.operational_data_source_status;
        CREATE TABLE public.source_mapping_revisions (
          id uuid PRIMARY KEY,
          business_id uuid NOT NULL,
          source_id uuid NOT NULL,
          requested_by uuid NOT NULL REFERENCES public.users(id),
          idempotency_key uuid NOT NULL,
          version integer NOT NULL,
          status varchar(16) NOT NULL,
          discovery jsonb NOT NULL,
          schema_fingerprint varchar(64) NOT NULL,
          proposal jsonb,
          approved_mapping jsonb,
          validation_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
          approved_by uuid REFERENCES public.users(id),
          approved_at timestamptz,
          reservation_id uuid REFERENCES public.ai_usage_reservations(id),
          failure_code varchar(100),
          created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
          CONSTRAINT fk_mapping_revision_source_scope FOREIGN KEY (source_id, business_id)
            REFERENCES public.operational_data_sources(id, business_id) ON DELETE CASCADE,
          CONSTRAINT uq_mapping_revision_version UNIQUE (source_id, version),
          CONSTRAINT uq_mapping_revision_replay UNIQUE (source_id, idempotency_key),
          CONSTRAINT ck_mapping_revision_version CHECK (version > 0),
          CONSTRAINT ck_mapping_revision_status CHECK (status IN ('proposing', 'review', 'approved', 'failed')),
          CONSTRAINT ck_mapping_revision_approval CHECK
            ((status = 'approved') = (approved_mapping IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL))
        );
        ALTER TABLE public.source_mapping_revisions OWNER TO sou2ai_migrator;
        REVOKE ALL ON public.source_mapping_revisions FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        GRANT SELECT, INSERT ON public.source_mapping_revisions TO sou2ai_runtime;
        GRANT UPDATE (status, proposal, approved_mapping, validation_notes, approved_by,
                      approved_at, failure_code) ON public.source_mapping_revisions TO sou2ai_runtime;

        CREATE FUNCTION public.sou2ai_guard_source_mapping_revision() RETURNS trigger
        AS $function$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'proposing' OR NEW.reservation_id IS NOT NULL OR
               NEW.proposal IS NOT NULL OR NEW.approved_mapping IS NOT NULL THEN
              RAISE EXCEPTION 'New mapping revisions must begin with review pending.' USING ERRCODE = '22023';
            END IF;
            RETURN NEW;
          END IF;
          IF OLD.status IN ('approved', 'failed') OR
             (NEW.id, NEW.business_id, NEW.source_id, NEW.requested_by, NEW.idempotency_key,
              NEW.version, NEW.discovery, NEW.schema_fingerprint, NEW.created_at)
             IS DISTINCT FROM
             (OLD.id, OLD.business_id, OLD.source_id, OLD.requested_by, OLD.idempotency_key,
              OLD.version, OLD.discovery, OLD.schema_fingerprint, OLD.created_at) OR
             (OLD.proposal IS NOT NULL AND NEW.proposal IS DISTINCT FROM OLD.proposal) OR
             (OLD.reservation_id IS NOT NULL AND NEW.reservation_id IS DISTINCT FROM OLD.reservation_id) OR
             (NEW.status = 'approved' AND OLD.status <> 'review') OR
             (OLD.status = 'review' AND NEW.status NOT IN ('review', 'approved')) THEN
            RAISE EXCEPTION 'Mapping revision is immutable or transition is invalid.' USING ERRCODE = '22023';
          END IF;
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        ALTER FUNCTION public.sou2ai_guard_source_mapping_revision() OWNER TO sou2ai_migrator;
        REVOKE ALL ON FUNCTION public.sou2ai_guard_source_mapping_revision() FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        CREATE TRIGGER trg_source_mapping_revision_guard BEFORE INSERT OR UPDATE
          ON public.source_mapping_revisions FOR EACH ROW
          EXECUTE FUNCTION public.sou2ai_guard_source_mapping_revision();

        CREATE FUNCTION public.sou2ai_reserve_source_mapping_usage(
          target_revision_id uuid, target_user_id uuid,
          target_estimated_input integer, target_max_output integer, target_lease_seconds integer
        ) RETURNS TABLE(reservation_id uuid, reserved_tokens integer, reset_at timestamptz)
        AS $function$
        DECLARE revision_record record; reservation_record record;
        BEGIN
          SELECT r.* INTO revision_record FROM public.source_mapping_revisions r
          WHERE r.id = target_revision_id AND r.requested_by = target_user_id FOR UPDATE;
          IF NOT FOUND OR revision_record.status <> 'proposing' OR revision_record.reservation_id IS NOT NULL THEN
            RAISE EXCEPTION 'Mapping request is not claimable.' USING ERRCODE = '22023';
          END IF;
          SELECT * INTO reservation_record FROM public.sou2ai_reserve_ai_usage(
            revision_record.business_id, target_user_id, NULL, 1, 'owner', 'source_mapping',
            target_estimated_input, target_max_output, target_lease_seconds);
          UPDATE public.source_mapping_revisions r SET reservation_id = reservation_record.reservation_id
            WHERE r.id = target_revision_id;
          RETURN QUERY SELECT reservation_record.reservation_id,
            reservation_record.reserved_tokens, reservation_record.reset_at;
        END;
        $function$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;
        ALTER FUNCTION public.sou2ai_reserve_source_mapping_usage(uuid, uuid, integer, integer, integer) OWNER TO sou2ai_migrator;
        REVOKE ALL ON FUNCTION public.sou2ai_reserve_source_mapping_usage(uuid, uuid, integer, integer, integer)
          FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        GRANT EXECUTE ON FUNCTION public.sou2ai_reserve_source_mapping_usage(uuid, uuid, integer, integer, integer) TO sou2ai_runtime;
    """)


def downgrade() -> None:
    # Refuse rollback while mappings exist; never delete customer configuration.
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.source_mapping_revisions) OR EXISTS
          (SELECT 1 FROM public.operational_data_sources WHERE mapping_profile_key <> 'fake_store_minimarket') THEN
          RAISE EXCEPTION 'Remove mapped source configurations through an approved migration before downgrade.';
        END IF;
      END $$;
      DROP FUNCTION public.sou2ai_reserve_source_mapping_usage(uuid, uuid, integer, integer, integer);
      DROP TABLE public.source_mapping_revisions;
      DROP FUNCTION public.sou2ai_guard_source_mapping_revision();
      DROP INDEX IF EXISTS public.uq_operational_sources_active_business;
      CREATE UNIQUE INDEX IF NOT EXISTS uq_operational_sources_active_type
        ON public.operational_data_sources(business_id,adapter_type)
        WHERE status = 'ACTIVE'::public.operational_data_source_status;
      ALTER TABLE public.operational_data_sources
        DROP CONSTRAINT ck_operational_sources_adapter,
        DROP CONSTRAINT ck_operational_sources_connection_profile,
        DROP CONSTRAINT ck_operational_sources_mapping_profile;
      ALTER TABLE public.operational_data_sources
        ADD CONSTRAINT ck_operational_sources_adapter CHECK (adapter_type = 'postgresql_readonly'),
        ADD CONSTRAINT ck_operational_sources_connection_profile CHECK (connection_profile_key = 'fake_store_postgresql'),
        ADD CONSTRAINT ck_operational_sources_mapping_profile CHECK (mapping_profile_key = 'fake_store_minimarket' AND mapping_profile_version = 1);
    """)

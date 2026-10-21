"""Append-only, capability-specific operational mappings. No existing data rewrite."""

from alembic import op

revision = "20261015_18"
down_revision = "20261013_17"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE public.source_operational_mapping_revisions (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL,
            source_id uuid NOT NULL,
            catalogue_revision_id uuid NOT NULL REFERENCES public.source_mapping_revisions(id) ON DELETE RESTRICT,
            version integer NOT NULL CHECK (version > 0),
            mapping jsonb NOT NULL,
            discovery jsonb NOT NULL,
            approved_by uuid NOT NULL REFERENCES public.users(id),
            approved_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            CONSTRAINT fk_operational_mapping_source FOREIGN KEY (source_id, business_id)
                REFERENCES public.operational_data_sources(id, business_id) ON DELETE RESTRICT,
            CONSTRAINT uq_operational_mapping_version UNIQUE (source_id, version),
            CONSTRAINT ck_operational_mapping_payload CHECK (
                (mapping->>'contract_version') IS NOT DISTINCT FROM '1' AND
                (mapping->>'business_id') IS NOT DISTINCT FROM business_id::text AND
                (mapping->>'source_id') IS NOT DISTINCT FROM source_id::text)
        );
        ALTER TABLE public.source_operational_mapping_revisions OWNER TO sou2ai_migrator;
        REVOKE ALL ON public.source_operational_mapping_revisions FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        GRANT SELECT, INSERT ON public.source_operational_mapping_revisions TO sou2ai_runtime;
        CREATE FUNCTION public.sou2ai_guard_operational_mapping() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Operational approvals are append-only.' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.source_mapping_revisions c
                WHERE c.id = NEW.catalogue_revision_id AND c.source_id = NEW.source_id
                  AND c.business_id = NEW.business_id AND c.status = 'approved'
                  AND c.version::text = NEW.mapping->>'catalogue_revision'
            ) THEN
                RAISE EXCEPTION 'Catalogue approval scope differs.' USING ERRCODE = '22023';
            END IF;
            RETURN NEW;
        END;
        $function$;
        ALTER FUNCTION public.sou2ai_guard_operational_mapping() OWNER TO sou2ai_migrator;
        REVOKE ALL ON FUNCTION public.sou2ai_guard_operational_mapping() FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        CREATE TRIGGER trg_operational_mapping_immutable BEFORE INSERT OR UPDATE OR DELETE
            ON public.source_operational_mapping_revisions FOR EACH ROW
            EXECUTE FUNCTION public.sou2ai_guard_operational_mapping();
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.source_operational_mapping_revisions) THEN
                RAISE EXCEPTION 'Preserve operational mapping approvals; downgrade refused.';
            END IF;
        END $$;
        DROP TABLE public.source_operational_mapping_revisions;
        DROP FUNCTION public.sou2ai_guard_operational_mapping();
    """)

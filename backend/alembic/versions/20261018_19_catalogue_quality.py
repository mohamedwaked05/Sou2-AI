"""Append-only catalogue quality evidence and explicit coverage approvals."""

from alembic import op

revision = "20261018_19"
down_revision = "20261015_18"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION public.sou2ai_guard_source_mapping_revision() RETURNS trigger
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
             (OLD.status = 'review' AND NEW.status NOT IN ('review', 'approved', 'failed')) THEN
            RAISE EXCEPTION 'Mapping revision is immutable or transition is invalid.' USING ERRCODE = '22023';
          END IF;
          RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql SET search_path = pg_catalog;
        ALTER FUNCTION public.sou2ai_guard_source_mapping_revision() OWNER TO sou2ai_migrator;
        REVOKE ALL ON FUNCTION public.sou2ai_guard_source_mapping_revision() FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator;
        CREATE TABLE public.catalogue_quality_reports (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL,
            source_id uuid NOT NULL,
            capability varchar(16) NOT NULL DEFAULT 'catalogue' CHECK(capability IN ('catalogue','inventory','pricing','sales','financial')),
            revision_id uuid REFERENCES public.source_mapping_revisions(id) ON DELETE RESTRICT,
            requested_by uuid NOT NULL REFERENCES public.users(id),
            mapping jsonb NOT NULL,
            source_updated_at timestamptz NOT NULL,
            report jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            FOREIGN KEY (source_id,business_id) REFERENCES public.operational_data_sources(id,business_id) ON DELETE RESTRICT,
            CHECK (pg_catalog.octet_length(report::text) <= 131072),
            CHECK ((report->>'capability') IS NOT DISTINCT FROM capability
                AND (report->>'contract_version') IS NOT DISTINCT FROM '1'
                AND (report->>'id') IS NOT DISTINCT FROM id::text
                AND (report->>'business_id') IS NOT DISTINCT FROM business_id::text
                AND (report->>'source_id') IS NOT DISTINCT FROM source_id::text
                AND (report->>'revision_id') IS NOT DISTINCT FROM revision_id::text)
        );
        CREATE INDEX ix_quality_report_source ON public.catalogue_quality_reports(source_id,capability,created_at DESC,id);
        CREATE TABLE public.catalogue_quality_approvals (
            id uuid PRIMARY KEY,
            business_id uuid NOT NULL,
            source_id uuid NOT NULL,
            capability varchar(16) NOT NULL DEFAULT 'catalogue' CHECK(capability IN ('catalogue','inventory','pricing','sales','financial')),
            report_id uuid NOT NULL REFERENCES public.catalogue_quality_reports(id) ON DELETE RESTRICT,
            version integer NOT NULL CHECK(version > 0),
            approval_type varchar(16) NOT NULL CHECK(approval_type IN ('full','partial','revoked')),
            approved_by uuid NOT NULL REFERENCES public.users(id),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            FOREIGN KEY(source_id,business_id) REFERENCES public.operational_data_sources(id,business_id) ON DELETE RESTRICT,
            CONSTRAINT uq_quality_approval_version UNIQUE(source_id,version)
        );
        CREATE UNIQUE INDEX uq_quality_report_approval ON public.catalogue_quality_approvals(report_id) WHERE approval_type IN ('full','partial');
        CREATE INDEX ix_quality_approval_capability ON public.catalogue_quality_approvals(source_id,capability,version DESC);
        ALTER TABLE public.catalogue_quality_reports OWNER TO sou2ai_migrator;
        ALTER TABLE public.catalogue_quality_approvals OWNER TO sou2ai_migrator;
        REVOKE ALL ON public.catalogue_quality_reports,public.catalogue_quality_approvals FROM PUBLIC,sou2ai_runtime,sou2ai_lifecycle_operator;
        GRANT SELECT,INSERT ON public.catalogue_quality_reports,public.catalogue_quality_approvals TO sou2ai_runtime;
        CREATE FUNCTION public.sou2ai_guard_catalogue_quality() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $fn$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Quality evidence and approvals are append-only.' USING ERRCODE='22023';
            END IF;
            IF TG_TABLE_NAME='catalogue_quality_reports' THEN
                IF (NEW.capability='catalogue' OR NEW.revision_id IS NOT NULL) AND NOT EXISTS(SELECT 1 FROM public.source_mapping_revisions r WHERE r.id=NEW.revision_id
                    AND r.source_id=NEW.source_id AND r.business_id=NEW.business_id) THEN
                    RAISE EXCEPTION 'Quality mapping scope differs.' USING ERRCODE='22023';
                END IF;
            ELSE
                PERFORM 1 FROM public.operational_data_sources WHERE id=NEW.source_id AND business_id=NEW.business_id FOR UPDATE;
                IF NOT EXISTS(SELECT 1 FROM public.catalogue_quality_reports q
                    LEFT JOIN public.source_mapping_revisions r ON r.id=q.revision_id
                    WHERE q.id=NEW.report_id AND q.source_id=NEW.source_id AND q.business_id=NEW.business_id
                    AND q.capability=NEW.capability
                    AND (NEW.capability <> 'catalogue' OR (r.status='approved' AND r.approved_mapping=q.mapping))
                    AND (NEW.approval_type='revoked' OR
                        ((q.report->>'structural_status')='PASS'
                        AND (q.report->>'expires_at')::timestamptz > pg_catalog.now()
                        AND ((NEW.approval_type='full' AND (q.report->>'invalid_records')::bigint=0)
                          OR (NEW.approval_type='partial' AND q.report->>'eligible'='true'
                              AND q.report->>'impact' IN ('SAFE_FOR_PARTIAL_CATALOGUE','SAFE_WITH_EXCLUSIONS')))))) THEN
                    RAISE EXCEPTION 'Quality approval evidence differs.' USING ERRCODE='22023';
                END IF;
                IF NEW.version <> COALESCE((SELECT max(version) FROM public.catalogue_quality_approvals WHERE source_id=NEW.source_id),0)+1 THEN
                    RAISE EXCEPTION 'Quality approval version differs.' USING ERRCODE='22023';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $fn$;
        ALTER FUNCTION public.sou2ai_guard_catalogue_quality() OWNER TO sou2ai_migrator;
        REVOKE ALL ON FUNCTION public.sou2ai_guard_catalogue_quality() FROM PUBLIC,sou2ai_runtime,sou2ai_lifecycle_operator;
        CREATE TRIGGER trg_quality_report_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.catalogue_quality_reports FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_catalogue_quality();
        CREATE TRIGGER trg_quality_report_no_truncate BEFORE TRUNCATE ON public.catalogue_quality_reports FOR EACH STATEMENT EXECUTE FUNCTION public.sou2ai_guard_catalogue_quality();
        CREATE TRIGGER trg_quality_approval_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.catalogue_quality_approvals FOR EACH ROW EXECUTE FUNCTION public.sou2ai_guard_catalogue_quality();
        CREATE TRIGGER trg_quality_approval_no_truncate BEFORE TRUNCATE ON public.catalogue_quality_approvals FOR EACH STATEMENT EXECUTE FUNCTION public.sou2ai_guard_catalogue_quality();
    """)


def downgrade():
    raise RuntimeError(
        "Quality evidence is permanent. Use a reviewed forward migration."
    )

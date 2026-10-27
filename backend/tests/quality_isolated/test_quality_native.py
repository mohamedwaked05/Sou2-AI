"""Production SQL, restricted reads, immutable evidence and real owner HTTP calls."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.core.config import get_settings
from app.database.models import OperationalDataSourceConfig, SourceMappingRevision, User
from app.integrations.catalogue_quality import (
    RULE_EXPLANATIONS,
    evaluate_quality,
    mapping_digest,
)
from app.integrations.discovery import (
    EngineConnector,
    SourceConnection,
    SourceMappingError,
)
from app.integrations.mapped_products import MappedProductSource
from app.integrations.profiles import get_connection_profile_registry
from app.main import app
from app.schemas.catalogue_quality import CatalogueCoverage, ExclusionPolicy
from app.schemas.source_mapping import CatalogueRequest, ProductMapping
from pydantic import SecretStr
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    text,
)
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def catalogue(quality_platform):
    platform = quality_platform
    suffix = uuid.uuid4().hex[:8]
    schema = f"quality_{suffix}"
    with platform["admin"].begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE "
                "rolname='quality_reader') THEN CREATE ROLE "
                "quality_reader LOGIN; END IF; END $$"
            )
        )
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO quality_reader'))
    metadata = MetaData(schema=schema)
    product = Table(
        "items",
        metadata,
        Column("id", String(128), primary_key=True),
        Column("name", String(300)),
        Column("group", String(128)),
        Column("family", String(128)),
    )
    category = Table(
        "groups",
        metadata,
        Column("code", String(128), primary_key=True),
        Column("family", String(128), primary_key=True),
        Column("name", String(300)),
    )
    barcode = Table(
        "barcodes",
        metadata,
        Column("id", String(128), primary_key=True),
        Column("product", String(128)),
        ForeignKeyConstraint(["product"], [product.c.id]),
    )
    metadata.create_all(platform["admin"])
    with platform["admin"].begin() as connection:
        connection.execute(
            category.insert(), [{"code": "g", "family": "f", "name": "Food"}]
        )
        connection.execute(
            product.insert(),
            [
                {
                    "id": f"p{i:04d}",
                    "name": "Product " + str(i),
                    "group": "g",
                    "family": "f",
                }
                for i in range(1000)
            ],
        )
        for table in metadata.tables.values():
            connection.execute(
                text(f'GRANT SELECT ON "{schema}"."{table.name}" TO quality_reader')
            )
    business_id = uuid.uuid4()
    config = SourceConnection(
        key="quality_fixture",
        display_name="Quality fixture",
        engine="postgresql",
        url=SecretStr(
            platform["admin_url"].replace("reservation_test_admin@", "quality_reader@")
        ),
        business_ids=(business_id,),
        discovery_scope=tuple(
            {
                "schema_name": schema,
                "object_name": table.name,
                "columns": tuple(c.name for c in table.c),
            }
            for table in (product, category, barcode)
        ),
    )
    connector = EngineConnector(config)
    discovery = connector.discover()
    mapping = ProductMapping(
        capability="products",
        object_id="o1",
        key_columns=("id",),
        name_columns=("name",),
        categories=(
            {
                "object_id": "o2",
                "label_column": "name",
                "joins": (
                    {"product_column": "group", "related_column": "code"},
                    {"product_column": "family", "related_column": "family"},
                ),
            },
        ),
        identifiers=(
            {
                "object_id": "o3",
                "value_column": "id",
                "kind": "barcode",
                "joins": ({"product_column": "id", "related_column": "product"},),
            },
        ),
    )
    adapter = MappedProductSource(connector, discovery, mapping)
    yield SimpleNamespace(
        platform=platform,
        admin=platform["admin"],
        product=product,
        category=category,
        barcode=barcode,
        adapter=adapter,
        config=config,
    )
    connector.engine.dispose()


def evaluate(source):
    with source.adapter.connector.connection(consistent=True) as connection:
        return evaluate_quality(source.adapter, connection)


def invalidate(source, count):
    with source.admin.begin() as connection:
        connection.execute(
            source.product.update()
            .where(source.product.c.id.in_([f"p{i:04d}" for i in range(count)]))
            .values(name="")
        )


@pytest.mark.parametrize("invalid", [0, 1, 9, 10, 11])
def test_exact_full_scope_threshold_and_boundary(catalogue, invalid):
    invalidate(catalogue, invalid)
    result = evaluate(catalogue)
    assert result.total == 1000 and result.invalid == invalid
    assert result.percent == Decimal(invalid) / 10
    assert result.reasons["missing_name"] == invalid
    if invalid:
        assert result.impact == "SAFE_WITH_EXCLUSIONS"
        assert (result.percent <= get_settings().catalogue_max_invalid_percent) == (
            invalid <= 10
        )
    else:
        assert catalogue.adapter.validate_results()


def test_overlapping_rules_count_each_source_row_once(catalogue):
    with catalogue.admin.begin() as connection:
        connection.execute(
            catalogue.product.insert(),
            {"id": "", "name": "", "group": "g", "family": "f"},
        )
    result = evaluate(catalogue)
    assert result.total == 1001 and result.invalid == 1
    assert result.reasons["invalid_identifier"] == result.reasons["missing_name"] == 1
    assert result.diagnostics[0].identifier is None


def test_dependent_barcode_cannot_reintroduce_excluded_product(catalogue):
    invalidate(catalogue, 1)
    with catalogue.admin.begin() as connection:
        connection.execute(
            catalogue.barcode.insert(), {"id": "barcode", "product": "p0000"}
        )
    result = evaluate(catalogue)
    assert result.impact == "SAFE_WITH_EXCLUSIONS"
    assert (
        not partial_adapter(catalogue, result)
        .search(CatalogueRequest(query="barcode"))
        .items
    )


def test_unknown_unrelated_relationship_inventory_does_not_block(
    catalogue, monkeypatch
):
    invalidate(catalogue, 1)
    monkeypatch.setattr(
        catalogue.adapter.connector, "relationship_inventory", lambda *args: None
    )
    assert evaluate(catalogue).impact == "SAFE_WITH_EXCLUSIONS"


def test_unrelated_dependency_scope_does_not_block_catalogue_only(catalogue):
    invalidate(catalogue, 1)
    table = catalogue.product
    with catalogue.admin.begin() as connection:
        connection.execute(
            text(
                f'CREATE TABLE "{table.schema}".hidden_refs '
                f'(product text REFERENCES "{table.schema}".items(id))'
            )
        )
    assert evaluate(catalogue).impact == "SAFE_WITH_EXCLUSIONS"


def partial_adapter(source, result):
    return MappedProductSource(
        source.adapter.connector,
        source.adapter.discovery,
        source.adapter.mapping,
        quality_policy=ExclusionPolicy(
            rules=tuple(RULE_EXPLANATIONS),
            mapping_digest=mapping_digest(source.adapter.mapping),
            data_digest=result.data_digest,
            relationship_digest=result.relationship_digest,
        ),
        coverage=CatalogueCoverage(
            approval_type="partial",
            excluded_records=result.invalid,
            total_records=result.total,
            valid_records=result.total - result.invalid,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )


def test_filtered_scope_search_category_exact_selection_and_pagination(catalogue):
    invalidate(catalogue, 9)
    adapter = partial_adapter(catalogue, evaluate(catalogue))
    found = []
    continuation = None
    while True:
        page = adapter.search(
            CatalogueRequest(
                browse=True, category="Food", continuation=continuation, limit=50
            )
        )
        found.extend(item.external_product_id for item in page.items)
        assert page.coverage.excluded_records == 9
        continuation = page.continuation
        if not continuation:
            break
    assert len(found) == len(set(found)) == 991
    assert page.matched_records == 991
    assert adapter.search(CatalogueRequest(query="Product")).items
    assert not adapter.search(
        CatalogueRequest(external_product_id="p0000", mapping_version=1)
    ).items


def test_changed_data_blocks_partial_runtime(catalogue):
    invalidate(catalogue, 1)
    adapter = partial_adapter(catalogue, evaluate(catalogue))
    with catalogue.admin.begin() as connection:
        connection.execute(
            catalogue.product.update()
            .where(catalogue.product.c.id == "p0001")
            .values(name="changed")
        )
    with pytest.raises(SourceMappingError, match="catalogue_partial_data_changed"):
        adapter.search(CatalogueRequest(query="Product"))


def test_unrelated_product_values_do_not_change_catalogue_coverage(catalogue):
    table = catalogue.product
    with catalogue.admin.begin() as connection:
        connection.execute(
            text(
                f'ALTER TABLE "{table.schema}".items ADD COLUMN unrelated_value numeric'
            )
        )
    scope = catalogue.config.discovery_scope
    config = catalogue.config.model_copy(
        update={
            "discovery_scope": (
                scope[0].model_copy(
                    update={"columns": (*scope[0].columns, "unrelated_value")}
                ),
                *scope[1:],
            )
        }
    )
    connector = EngineConnector(config)
    discovery = connector.discover()
    original = catalogue.adapter
    catalogue.adapter = MappedProductSource(connector, discovery, original.mapping)
    try:
        invalidate(catalogue, 1)
        before = evaluate(catalogue)
        partial = partial_adapter(catalogue, before)
        with catalogue.admin.begin() as connection:
            connection.execute(
                text(f'UPDATE "{table.schema}".items SET unrelated_value=123')
            )
        after = evaluate(catalogue)
        assert before.data_digest == after.data_digest
        assert partial.search(CatalogueRequest(query="Product")).matched_records == 999
    finally:
        catalogue.adapter = original
        connector.engine.dispose()


def test_incomplete_composite_key_blocked_before_quality_tolerance(catalogue):
    mapping = catalogue.adapter.mapping
    category = mapping.categories[0].model_copy(
        update={"joins": mapping.categories[0].joins[:1]}
    )
    with pytest.raises(SourceMappingError, match="mapping_category_join_not_unique"):
        MappedProductSource(
            catalogue.adapter.connector,
            catalogue.adapter.discovery,
            mapping.model_copy(update={"categories": (category,)}),
        )


@pytest.fixture
def owner_catalogue(catalogue, tenant, monkeypatch):
    user_auth, business_id, client = tenant
    config = catalogue.config.model_copy(update={"business_ids": (business_id,)})
    profile = SimpleNamespace(
        business_ids=(business_id,),
        adapter_type="postgresql_readonly",
        mapping_profile_key="discovered_products",
    )
    connector = EngineConnector(config)
    registry = SimpleNamespace(
        get_profile=lambda key: profile, connector=lambda *args: connector
    )
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    with catalogue.platform["factory"]() as session:
        user = session.get(User, user_auth.id)
        source = OperationalDataSourceConfig(
            business_id=business_id,
            display_name="Quality source",
            adapter_type="postgresql_readonly",
            connection_profile_key=config.key,
            mapping_profile_key="discovered_products",
            mapping_profile_version=1,
            status="CONFIGURED",
        )
        session.add(source)
        session.flush()
        rev = SourceMappingRevision(
            business_id=business_id,
            source_id=source.id,
            requested_by=user.id,
            idempotency_key=uuid.uuid4(),
            version=1,
            status="proposing",
            discovery=catalogue.adapter.discovery.model_dump(mode="json"),
            schema_fingerprint=catalogue.adapter.discovery.schema_fingerprint,
            validation_notes=[],
        )
        session.add(rev)
        session.flush()
        rev.status = "review"
        rev.proposal = {
            "mapping": catalogue.adapter.mapping.model_dump(mode="json"),
            "rationale": "Review",
            "uncertainties": [],
        }
        session.commit()
        source_id, revision_id = source.id, rev.id
    from tests.test_data_sources import headers

    yield (
        catalogue,
        client,
        headers(user_auth),
        f"/api/v1/businesses/{business_id}/data-sources/{source_id}",
        revision_id,
    )


def validation_request(system):
    source, client, auth, url, revision = system
    response = client.post(
        url + "/quality/validate", headers=auth, json={"revision_id": str(revision)}
    )
    assert response.status_code == 200, response.text
    return response.json()


def approval_body(report):
    return dict(
        report_id=report["id"],
        integrity=report["integrity"],
        excluded_records=report["invalid_records"],
        total_records=report["total_records"],
        confirm_semantics=True,
        acknowledge_uncertainties=True,
        confirm_catalogue_only=True,
    )


def test_real_tenant_partial_approval_replay_activation_revocation(owner_catalogue):
    source, client, auth, url, _ = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    assert report["eligible"] and report["structural_status"] == "PASS"
    approved = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert approved.status_code == 200, approved.text
    replay = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert replay.json()["approval_id"] == approved.json()["approval_id"]
    assert client.post(url + "/activate", headers=auth).status_code == 200
    detail = client.get(url, headers=auth).json()
    assert detail["catalogue_coverage"]["excluded_records"] == 1
    assert detail["capabilities"] == ["products"]
    found = client.post(
        url + "/products/search", headers=auth, json={"query": "Product", "limit": 20}
    )
    assert found.status_code == 200, found.text
    assert found.json()["matched_records"] == 999
    assert "p0000" not in [p["external_product_id"] for p in found.json()["items"]]
    revoked = client.post(url + "/quality/revoke", headers=auth)
    assert revoked.status_code == 204
    assert client.post(url + "/activate", headers=auth).status_code == 409
    assert (
        client.post(
            url + "/products/search", headers=auth, json={"query": "Product"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("expired", [False, True])
def test_unchanged_active_catalogue_renewal_keeps_source_active(
    owner_catalogue, monkeypatch, expired
):
    from app.services import catalogue_quality

    source, client, auth, url, revision_id = owner_catalogue
    invalidate(source, 1)
    original = validation_request(owner_catalogue)
    first = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(original)
    )
    assert first.status_code == 200, first.text
    assert client.post(url + "/activate", headers=auth).status_code == 200
    if expired:
        offset = get_settings().catalogue_partial_approval_ttl_seconds + 1

        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(UTC) + timedelta(seconds=offset)

        monkeypatch.setattr(catalogue_quality, "datetime", Future)
        replay = client.post(
            url + "/quality/approve", headers=auth, json=approval_body(original)
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "catalogue_quality_report_stale"
    fresh = validation_request(owner_catalogue)
    assert fresh["revision_id"] == str(revision_id)
    body = approval_body(fresh)
    missing_confirmation = client.post(
        url + "/quality/approve",
        headers=auth,
        json={**body, "confirm_semantics": False},
    )
    assert missing_confirmation.status_code == 422
    renewed = client.post(url + "/quality/approve", headers=auth, json=body)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["approval_id"] != first.json()["approval_id"]
    assert renewed.json()["approval_version"] == first.json()["approval_version"] + 1
    assert client.get(url, headers=auth).json()["status"] == "ACTIVE"
    with source.platform["factory"]() as session:
        revision = session.get(SourceMappingRevision, revision_id)
        assert revision.version == 1 and revision.approved_mapping == fresh["mapping"]
    replay = client.post(url + "/quality/approve", headers=auth, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["approval_id"] == renewed.json()["approval_id"]


def test_active_renewal_rechecks_source_data_and_keeps_prior_approval(owner_catalogue):
    source, client, auth, url, _ = owner_catalogue
    invalidate(source, 1)
    original = validation_request(owner_catalogue)
    first = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(original)
    )
    assert first.status_code == 200, first.text
    assert client.post(url + "/activate", headers=auth).status_code == 200
    fresh = validation_request(owner_catalogue)
    invalidate(source, 2)
    renewed = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(fresh)
    )
    assert renewed.status_code == 409
    assert renewed.json()["error"]["code"] == "catalogue_quality_report_stale"
    with source.platform["factory"]() as session:
        from app.services.catalogue_quality import latest_event

        config = session.get(
            OperationalDataSourceConfig, uuid.UUID(url.rsplit("/", 1)[1])
        )
        assert config.status.value == "ACTIVE"
        assert str(latest_event(session, config).id) == first.json()["approval_id"]


def test_active_source_cannot_approve_a_review_mapping(owner_catalogue):
    source, client, auth, url, revision_id = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    assert (
        client.post(
            url + "/quality/approve", headers=auth, json=approval_body(report)
        ).status_code
        == 200
    )
    assert client.post(url + "/activate", headers=auth).status_code == 200
    with source.platform["factory"]() as session:
        original = session.get(SourceMappingRevision, revision_id)
        draft = SourceMappingRevision(
            business_id=original.business_id,
            source_id=original.source_id,
            requested_by=original.requested_by,
            idempotency_key=uuid.uuid4(),
            version=2,
            status="proposing",
            discovery=original.discovery,
            schema_fingerprint=original.schema_fingerprint,
            validation_notes=[],
        )
        session.add(draft)
        session.flush()
        draft.status = "review"
        draft.proposal = original.proposal
        session.commit()
        draft_id = draft.id
    report = validation_request((*owner_catalogue[:4], draft_id))
    response = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mapping_disable_before_approval"


def test_stale_data_cannot_be_approved(owner_catalogue):
    source, client, auth, url, _ = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    invalidate(source, 2)
    response = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert response.status_code == 409, response.text


def test_partial_policy_does_not_implicitly_authorize_legacy_whole_mapping(
    owner_catalogue, monkeypatch
):
    from app.core.exceptions import ApplicationError
    from app.services import operational_mapping

    source, client, auth, url, revision_id = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    response = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert response.status_code == 200, response.text
    registry = app.dependency_overrides[get_connection_profile_registry]()

    def unexpected_discovery():
        pytest.fail("Partial catalogue must block operational reads before discovery")

    monkeypatch.setattr(
        registry.connector(None, None), "discover", unexpected_discovery
    )
    with source.platform["factory"]() as session:
        revision = session.get(SourceMappingRevision, revision_id)
        user = session.get(User, revision.requested_by)
        arguments = (session, user, revision.business_id, revision.source_id)
        body = SimpleNamespace(
            mapping=SimpleNamespace(
                business_id=revision.business_id,
                source_id=revision.source_id,
                catalogue_revision=revision.version,
            )
        )
        for action in (
            operational_mapping.validate_operational_draft,
            operational_mapping.preview_operational_draft,
            operational_mapping.approve_operational_mapping,
        ):
            with pytest.raises(ApplicationError) as error:
                action(*arguments, body, registry)
            assert error.value.error_code == "catalogue_partial_operations_unapproved"
        with pytest.raises(ApplicationError) as error:
            operational_mapping.operational_onboarding(*arguments, registry)
        assert error.value.error_code == "catalogue_partial_operations_unapproved"


def test_tampered_confirmation_and_wrong_business_are_rejected(owner_catalogue):
    source, client, auth, url, _ = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    body = approval_body(report)
    body["excluded_records"] = 2
    assert (
        client.post(url + "/quality/approve", headers=auth, json=body).status_code
        == 409
    )
    parts = url.split("/")
    parts[4] = str(uuid.uuid4())
    wrong = "/".join(parts)
    assert client.get(wrong + "/quality", headers=auth).status_code == 404
    assert (
        client.post(
            wrong + "/quality/approve", headers=auth, json=approval_body(report)
        ).status_code
        == 404
    )


def test_revision19_acl_and_append_only_evidence(quality_platform):
    with quality_platform["runtime"].connect() as connection:
        for table in ("catalogue_quality_reports", "catalogue_quality_approvals"):
            for operation in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not connection.scalar(
                    text("SELECT has_table_privilege(current_user,:table,:operation)"),
                    {"table": table, "operation": operation},
                )
    with quality_platform["admin"].connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261018_19"
        )
        for sql in (
            "TRUNCATE catalogue_quality_reports",
            "TRUNCATE catalogue_quality_approvals",
        ):
            with pytest.raises(DBAPIError):
                connection.execute(text(sql))
            connection.rollback()


@pytest.mark.parametrize(
    "count,eligible", [(1, True), (9, True), (10, True), (11, False)]
)
def test_owner_report_threshold_decision_is_inclusive(owner_catalogue, count, eligible):
    invalidate(owner_catalogue[0], count)
    report = validation_request(owner_catalogue)
    assert report["eligible"] is eligible
    assert (
        report["invalid_records"] == count and report["valid_records"] == 1000 - count
    )


def test_full_catalogue_has_explicit_distinct_approval(owner_catalogue):
    _, client, auth, url, _ = owner_catalogue
    report = validation_request(owner_catalogue)
    body = approval_body(report)
    body["approval_type"] = "full"
    assert not report["eligible"] and report["invalid_records"] == 0
    response = client.post(url + "/quality/approve", headers=auth, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["approval_type"] == "full"


def test_diagnostics_are_bounded_without_sampling_counts(owner_catalogue):
    invalidate(owner_catalogue[0], 60)
    report = validation_request(owner_catalogue)
    assert report["invalid_records"] == 60 and len(report["diagnostics"]) == 50
    assert report["diagnostics_truncated"] and not report["eligible"]


def test_expired_validation_requires_new_report(owner_catalogue, monkeypatch):
    from app.services import catalogue_quality

    invalidate(owner_catalogue[0], 1)
    report = validation_request(owner_catalogue)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=2)

    monkeypatch.setattr(catalogue_quality, "datetime", Future)
    response = owner_catalogue[1].post(
        owner_catalogue[3] + "/quality/approve",
        headers=owner_catalogue[2],
        json=approval_body(report),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "catalogue_quality_report_stale"


def test_changed_approved_mapping_revision_rejects_old_report(owner_catalogue):
    source, client, auth, url, revision_id = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    with source.platform["factory"]() as session:
        old = session.get(SourceMappingRevision, revision_id)
        revision = SourceMappingRevision(
            business_id=old.business_id,
            source_id=old.source_id,
            requested_by=old.requested_by,
            idempotency_key=uuid.uuid4(),
            version=2,
            status="proposing",
            discovery=old.discovery,
            schema_fingerprint=old.schema_fingerprint,
            validation_notes=[],
        )
        session.add(revision)
        session.flush()
        revision.status = "review"
        revision.proposal = old.proposal
        session.flush()
        revision.status = "approved"
        revision.approved_mapping = old.proposal["mapping"]
        revision.approved_by = old.requested_by
        revision.approved_at = datetime.now(UTC)
        session.commit()
    assert (
        client.post(
            url + "/quality/approve", headers=auth, json=approval_body(report)
        ).status_code
        == 409
    )


def test_concurrent_source_validation_is_rejected(owner_catalogue):
    from app.services.catalogue_quality import _serialize_validation

    source, client, auth, url, revision_id = owner_catalogue
    with source.platform["factory"]() as session:
        revision = session.get(SourceMappingRevision, revision_id)
        configured = session.get(OperationalDataSourceConfig, revision.source_id)
        _serialize_validation(session, configured)
        response = client.post(
            url + "/quality/validate",
            headers=auth,
            json={"revision_id": str(revision_id)},
        )
        assert response.status_code == 409
        session.rollback()


def test_reject_proposal_preserves_immutable_history(owner_catalogue):
    source, client, auth, url, revision_id = owner_catalogue
    response = client.post(url + f"/mappings/{revision_id}/reject", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["failure_code"] == "mapping_rejected_by_owner"
    with source.platform["factory"]() as session:
        assert session.get(SourceMappingRevision, revision_id).proposal is not None


@pytest.mark.parametrize("nulls", [1, 2])
def test_nullable_unique_identity_is_safe_only_without_duplicate_nulls(
    catalogue, nulls
):
    metadata = MetaData(schema=catalogue.product.schema)
    product = Table(
        "nullable_items",
        metadata,
        Column("id", String(128), unique=True),
        Column("name", String(300)),
    )
    metadata.create_all(catalogue.admin)
    with catalogue.admin.begin() as connection:
        connection.execute(
            product.insert(),
            [
                {"id": None if i < nulls else str(i), "name": "Product"}
                for i in range(1000)
            ],
        )
        connection.execute(
            text(f'GRANT SELECT ON "{product.schema}".nullable_items TO quality_reader')
        )
    config = catalogue.config.model_copy(
        update={
            "discovery_scope": (
                catalogue.config.discovery_scope[0].model_copy(
                    update={"object_name": "nullable_items", "columns": ("id", "name")}
                ),
            )
        }
    )
    connector = EngineConnector(config)
    adapter = MappedProductSource(
        connector,
        connector.discover(),
        ProductMapping(
            capability="products",
            object_id="o1",
            key_columns=("id",),
            name_columns=("name",),
        ),
    )
    with connector.connection(consistent=True) as connection:
        result = evaluate_quality(adapter, connection)
    assert result.invalid == nulls
    assert result.impact == (
        "SAFE_WITH_EXCLUSIONS" if nulls == 1 else "BLOCKED_BY_DATA_QUALITY"
    )
    assert result.reasons["duplicate_identity"] == (0 if nulls == 1 else 2)
    assert all(row.identifier is None for row in result.diagnostics)
    connector.engine.dispose()


def test_optional_missing_category_is_a_coverage_diagnostic(catalogue):
    with catalogue.admin.begin() as connection:
        connection.execute(
            catalogue.product.update()
            .where(catalogue.product.c.id == "p0000")
            .values(family=None)
        )
    result = evaluate(catalogue)
    assert result.invalid == 0
    assert result.relationship_counts["optional_category_0_unmatched"] == 1
    assert any("optional category" in note for note in result.findings)


def test_source_configuration_fingerprint_change_blocks_approval(owner_catalogue):
    source, client, auth, url, _ = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    registry = app.dependency_overrides[get_connection_profile_registry]()
    registry.connector(None, None).source_fingerprint = "f" * 64
    response = client.post(
        url + "/quality/approve", headers=auth, json=approval_body(report)
    )
    assert response.status_code == 409


def test_actual_other_tenant_cannot_inspect_or_approve(owner_catalogue):
    from tests.test_business_api import create_user
    from tests.test_data_sources import headers

    source, client, _, url, _ = owner_catalogue
    invalidate(source, 1)
    report = validation_request(owner_catalogue)
    with source.platform["factory"]() as session:
        other = create_user(session, f"other-{uuid.uuid4().hex}@example.invalid")
        auth = headers(other)
    assert client.get(url + "/quality", headers=auth).status_code == 404
    assert (
        client.post(
            url + "/quality/approve", headers=auth, json=approval_body(report)
        ).status_code
        == 404
    )


def test_pagination_token_is_bound_to_the_approval_version(catalogue):
    invalidate(catalogue, 1)
    adapter = partial_adapter(catalogue, evaluate(catalogue))
    adapter.coverage = adapter.coverage.model_copy(update={"approval_id": uuid.uuid4()})
    first = adapter.search(CatalogueRequest(browse=True, limit=10))
    adapter.coverage = adapter.coverage.model_copy(update={"approval_id": uuid.uuid4()})
    with pytest.raises(SourceMappingError, match="invalid_catalogue_continuation"):
        adapter.search(
            CatalogueRequest(browse=True, limit=10, continuation=first.continuation)
        )

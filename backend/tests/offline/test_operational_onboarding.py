"""Draft workflow on real synthetic SQL; platform authorization is native-tested."""

import uuid
from types import SimpleNamespace

import pytest
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.database.models import OperationalDataSourceStatus, ToolCallLog
from app.integrations.discovery import SourceMappingError
from app.schemas.operational_mapping import (
    OperationalMappingDraft,
    OperationalMappingPreview,
    OperationalRead,
)
from app.services import operational_mapping as service
from test_mapped_operations import source as source


@pytest.fixture
def workflow(source, monkeypatch):
    monkeypatch.setenv("TOOL_CALL_AUDIT_HMAC_SECRET", "synthetic-audit-key")
    get_settings.cache_clear()
    record = SimpleNamespace(
        status=OperationalDataSourceStatus.CONFIGURED, updated_at=1
    )
    session = SimpleNamespace(
        commit=lambda: None,
        refresh=lambda _: None,
        scalars=lambda _: SimpleNamespace(all=lambda: []),
        added=[],
    )
    session.add = session.added.append
    monkeypatch.setattr(service, "_source", lambda *_: record)
    monkeypatch.setattr(service, "mapped_source", lambda *_: source.catalogue)
    monkeypatch.setattr(
        service, "latest_approved", lambda *_: SimpleNamespace(version=1)
    )
    monkeypatch.setattr(
        source.catalogue.connector, "discover", lambda: source.discovery, raising=False
    )
    body = OperationalMappingDraft(mapping=source.mapping, discovery=source.discovery)
    args = (
        session,
        SimpleNamespace(id=uuid.uuid4()),
        source.mapping.business_id,
        source.mapping.source_id,
    )
    return SimpleNamespace(
        source=source, record=record, session=session, body=body, args=args
    )


def test_metadata_proposals_do_not_assign_or_approve_semantics(workflow):
    result = service.operational_onboarding(*workflow.args, None)
    assert result["history"] == []
    assert len(result["proposals"]["capabilities"]) == 8
    assert all(
        p["status"] == "needs_owner_semantics"
        for p in result["proposals"]["capabilities"]
    )
    assert "prices" in result["mapping_schema"]["properties"]
    assert not workflow.session.added


def test_validation_and_preview_cannot_persist_approval(workflow):
    result = service.validate_operational_draft(*workflow.args, workflow.body, None)
    assert result["approved"] is False and "stock" in result["capabilities"]
    preview = OperationalMappingPreview(
        **workflow.body.model_dump(),
        request=OperationalRead(
            capability="stock",
            catalogue_revision=1,
            mapping_revision=1,
            limit=1,
        ),
    )
    result = service.preview_operational_draft(*workflow.args, preview, None)
    assert result["draft"] is True and result["approved"] is False
    assert len(result["report"]["rows"]) == 1
    assert len(workflow.session.added) == 1
    assert isinstance(workflow.session.added[0], ToolCallLog)


@pytest.mark.parametrize(
    "failure", ["active", "scope", "drift", "permissions", "unauthorized"]
)
def test_drafts_fail_before_execution(workflow, monkeypatch, failure):
    if failure == "active":
        workflow.record.status = OperationalDataSourceStatus.ACTIVE
    elif failure == "scope":
        workflow.args = (*workflow.args[:2], uuid.uuid4(), workflow.args[3])
    elif failure == "drift":
        monkeypatch.setattr(
            workflow.source.catalogue.connector,
            "discover",
            lambda: workflow.source.discovery.model_copy(
                update={"source_fingerprint": "f" * 64}
            ),
        )
    elif failure == "permissions":

        def denied():
            raise SourceMappingError("source_query_rejected")

        monkeypatch.setattr(workflow.source.catalogue.connector, "discover", denied)
    else:

        def unauthorized(*_):
            raise service.mapping_error("not_authorized", 403)

        monkeypatch.setattr(service, "_source", unauthorized)
    with pytest.raises(ApplicationError):
        service.validate_operational_draft(*workflow.args, workflow.body, None)
    assert not workflow.session.added


def test_preview_rechecks_source_and_audits_failure(workflow, monkeypatch):
    monkeypatch.setattr(
        workflow.session, "refresh", lambda _: setattr(workflow.record, "updated_at", 2)
    )
    body = OperationalMappingPreview(
        **workflow.body.model_dump(),
        request=OperationalRead(
            capability="stock",
            catalogue_revision=1,
            mapping_revision=1,
        ),
    )
    with pytest.raises(ApplicationError) as failure:
        service.preview_operational_draft(*workflow.args, body, None)
    assert failure.value.error_code == "mapping_selection_stale"
    assert workflow.session.added[0].error_code == "adapter_failure"

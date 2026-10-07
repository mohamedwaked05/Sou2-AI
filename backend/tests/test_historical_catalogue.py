"""Opt-in source validation. The private harness supplies credentials in memory."""

import json
import os
import uuid

import httpx
import pytest
from app.agent.mapping_provider import GeminiMappingProvider, get_mapping_provider
from app.agent.owner_chat_provider import GeminiOwnerChatProvider
from app.core.config import get_settings
from app.integrations.discovery import EngineConnector, SourceConnection
from app.integrations.mapped_products import compile_catalogue, encode_product_key
from app.integrations.profiles import get_connection_profile_registry
from app.main import app
from app.schemas.source_mapping import MappingProposal, SchemaDiscovery
from pydantic import SecretStr
from sqlalchemy import text

from tests.test_business_api import complete_profile
from tests.test_data_sources import create_owner_business, headers
from tests.test_source_mapping import (
    FixtureRegistry,
    call,
    exercise_catalogue_owner_chat,
)


def test_historical_catalogue_application_flow(
    api_client, db_session, migration_engine, monkeypatch
):
    supplied = os.getenv("MAPPING_HISTORICAL_CONNECTION")
    if supplied is None:
        pytest.skip("Requires the private restricted-reader validation harness.")
    user, business = create_owner_business(
        db_session, "historical-test@example.com", "Isolated Historical Catalogue"
    )
    assert complete_profile(api_client, user, str(business.id)).status_code == 200
    assert (
        api_client.post(
            f"/api/v1/businesses/{business.id}/onboarding/confirm",
            headers=headers(user),
        ).status_code
        == 200
    )
    with migration_engine.begin() as connection:
        connection.execute(
            text("UPDATE public.businesses SET status='ACTIVE' WHERE id=:id"),
            {"id": business.id},
        )
    db_session.expire_all()
    config = json.loads(supplied)
    config["business_ids"] = [str(business.id)]
    connector = EngineConnector(SourceConnection.model_validate(config))
    registry = FixtureRegistry(connector, business.id)
    # This is an explicitly mocked proposal fixture, not live classification.
    proposal = MappingProposal.model_validate(
        json.loads(os.environ["MAPPING_HISTORICAL_PROPOSAL"])
    )
    attempts = []

    def respond(request):
        payload = json.loads(request.content)
        attempts.append(payload)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": proposal.model_dump_json()}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 160,
                    "candidatesTokenCount": 120,
                    "totalTokenCount": 280,
                },
            },
        )

    provider = GeminiMappingProvider(
        GeminiOwnerChatProvider(
            api_key="mock-only",
            model="gemini-3-flash-preview",
            timeout_seconds=2,
            transport=httpx.MockTransport(respond),
        )
    )
    app.dependency_overrides[get_mapping_provider] = lambda: provider
    app.dependency_overrides[get_connection_profile_registry] = lambda: registry
    monkeypatch.setattr(
        get_settings(),
        "tool_call_audit_hmac_secret",
        SecretStr("historical-test-audit-secret-32-chars"),
    )
    root = f"/api/v1/businesses/{business.id}/data-sources"
    result = api_client.post(
        root,
        headers=headers(user),
        json={
            "display_name": "Historical catalogue",
            "connection_profile_key": "opaque_catalogue",
            "mapping_profile_key": "discovered_products",
            "mapping_profile_version": 1,
        },
    )
    assert result.status_code == 201
    path = root + "/" + result.json()["id"]
    key = {"idempotency_key": str(uuid.uuid4())}
    review = call(api_client, user, path + "/mappings/propose", key)
    assert review.status_code == 200 and review.json()["status"] == "review"
    metadata = SchemaDiscovery.model_validate(review.json()["discovery"])
    payload = provider._payload(metadata)
    input_estimate = provider.estimate_input_tokens(metadata)
    exposure = {
        "objects": len(metadata.objects),
        "columns": sum(len(obj.columns) for obj in metadata.objects),
        "metadata_utf8_bytes": len(payload["contents"][0]["parts"][0]["text"].encode()),
        "estimated_input_tokens": input_estimate,
        "max_output_tokens": 2048,
        "initial_reservation": input_estimate + 2048,
        "product_records_exposed": 0,
    }
    print("Bounded live proposal preflight: " + json.dumps(exposure, sort_keys=True))
    revision_id = review.json()["id"]
    approved = call(
        api_client,
        user,
        path + f"/mappings/{revision_id}/approve",
        {
            "mapping": review.json()["proposal"]["mapping"],
            "confirm_semantics": True,
            "acknowledge_uncertainties": True,
        },
    )
    assert approved.status_code == 200
    assert call(api_client, user, path + "/activate").status_code == 200
    english = call(
        api_client, user, path + "/products/search", {"query": "Pepsi", "limit": 50}
    )
    arabic = call(
        api_client, user, path + "/products/search", {"query": "بيبسي", "limit": 50}
    )
    # Assertions deliberately avoid dumping the historical source records.
    assert english.status_code == arabic.status_code == 200
    matches = english.json()
    assert matches["status"] == "ambiguous" and len(matches["items"]) == 13
    assert not matches["truncated"]
    # Use an actual secondary name, not an invented translation/alias.
    assert arabic.json()["status"] == "not_found"
    # Local-only sample from the approved plan; no record reaches the provider.
    discovery = SchemaDiscovery.model_validate(review.json()["discovery"])
    statement, _ = compile_catalogue(proposal.mapping, discovery, None)
    secondary_column = statement.selected_columns["name_1"].element
    statement = statement.where(
        secondary_column.is_not(None), secondary_column != ""
    ).limit(1)
    with connector.connection() as connection:
        sample = connection.execute(statement).mappings().first()
    assert sample is not None
    secondary_id = encode_product_key(
        tuple(str(sample[f"key_{i}"]) for i in range(len(proposal.mapping.key_columns)))
    )
    secondary = call(
        api_client,
        user,
        path + "/products/search",
        {
            "query": str(sample["name_1"])[:128],
            "limit": 50,
        },
    )
    assert secondary.status_code == 200
    contains_selected = secondary_id in {
        item["external_product_id"] for item in secondary.json()["items"]
    }
    assert contains_selected
    assert all(
        isinstance(item["external_product_id"], str) and item["stock"] is None
        for item in matches["items"]
    )
    selected = call(
        api_client,
        user,
        path + "/products/search",
        {
            "external_product_id": matches["items"][0]["external_product_id"],
            "mapping_version": matches["mapping_version"],
        },
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "resolved"
    assert selected.json()["items"][0] == matches["items"][0]
    assert (
        call(api_client, user, path + "/mappings/propose", key).json()["id"]
        == revision_id
    )
    assert len(attempts) == 1 and "Pepsi" not in json.dumps(attempts[0])
    with migration_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_tokens_used,tokens_reserved "
                "FROM public.business_ai_usage_daily WHERE business_id=:id"
            ),
            {"id": business.id},
        ).one()
        assert row.total_tokens_used == 280 and row.tokens_reserved == 0
    exercise_catalogue_owner_chat(
        (
            api_client,
            db_session,
            migration_engine,
            user,
            business,
            path,
            registry,
            provider,
        ),
        matches["items"],
    )
    connector.engine.dispose()
    print(
        "Historical catalogue: 13 variants, second-name search passed; "
        "literal Arabic alias unavailable; owner-chat search, eh, selection "
        "and replay passed; "
        "stock unknown; mock proposal/chat usage 820, hold 0."
    )

"""Identity and continuation tests use arbitrary source values, no provider calls."""

import pytest
from app.integrations.discovery import SourceMappingError
from app.schemas.source_mapping import CatalogueRequest
from app.services import owner_chat
from app.utils.product_reference import changes_product_reference
from integration_sources import mapped_catalogue
from test_catalogue_chat import ORIGINAL_PENDING_LOADER
from test_catalogue_chat import flow as flow
from test_owner_chat_state import system as system


@pytest.mark.parametrize("count", [28, 76])
@pytest.mark.parametrize("layout", ["text", "integer", "composite"])
def test_all_valid_identifiers_are_reachable_across_bounded_pages(count, layout):
    fixture = mapped_catalogue(count, layout)
    source = fixture.source
    source.db.execute("PRAGMA query_only = OFF")
    table = f'"schema_{count}"."records_{count}"'
    source.db.execute(f"DELETE FROM {table}")
    source.db.executemany(
        f"INSERT INTO {table} VALUES (?,?,?,?,?,?)",
        [
            (
                i if layout == "integer" else f"{i:04}",
                "west",
                "Random Diet 7 UP",
                None,
                None,
                "g",
            )
            for i in range(count)
        ],
    )
    source.readonly()
    try:
        request = CatalogueRequest(query="7UP Diet Random", limit=20)
        identifiers = []
        while True:
            page = fixture.adapter.search(request)
            assert 0 < len(page.items) <= 20
            identifiers.extend(p.external_product_id for p in page.items)
            if not page.continuation:
                assert not page.truncated
                break
            request = request.model_copy(update={"continuation": page.continuation})
        assert len(identifiers) == len(set(identifiers)) == count
        browsed = fixture.adapter.search(CatalogueRequest(browse=True, limit=50))
        assert len(browsed.items) == min(count, 50)
    finally:
        source.close()


def test_continuation_cannot_change_source_revision_or_search():
    first, second = mapped_catalogue(71), mapped_catalogue(72)
    try:
        page = first.adapter.search(CatalogueRequest(browse=True, limit=1))
        assert page.continuation
        for adapter, request in (
            (
                second.adapter,
                CatalogueRequest(browse=True, continuation=page.continuation),
            ),
            (
                first.adapter,
                CatalogueRequest(query="other", continuation=page.continuation),
            ),
            (
                first.adapter,
                CatalogueRequest(
                    browse=True, mapping_version=2, continuation=page.continuation
                ),
            ),
            (first.adapter, CatalogueRequest(browse=True, continuation="untrusted")),
        ):
            with pytest.raises(SourceMappingError):
                adapter.search(request)
    finally:
        first.source.close()
        second.source.close()


def test_approved_category_is_not_a_guessed_synonym():
    fixture = mapped_catalogue(61)
    try:
        assert (
            len(
                fixture.adapter.search(
                    CatalogueRequest(browse=True, category="Group")
                ).items
            )
            == 2
        )
        assert (
            fixture.adapter.search(
                CatalogueRequest(browse=True, category="drinks")
            ).status
            == "not_found"
        )
        assert (
            fixture.adapter.search(CatalogueRequest(query="gummies")).status
            == "not_found"
        )
    finally:
        fixture.source.close()


@pytest.mark.parametrize("column,value", [("identity", ""), ("label", None)])
def test_invalid_catalogue_rows_remain_present_and_fail_closed(column, value):
    fixture = mapped_catalogue(62)
    fixture.source.db.execute("PRAGMA query_only = OFF")
    fixture.source.db.execute(
        f'UPDATE schema_62.records_62 SET "{column}"=? WHERE identity=?',
        (value, "0001"),
    )
    if column == "label":
        fixture.source.db.execute(
            "UPDATE schema_62.records_62 SET local_label=NULL WHERE identity=?",
            ("0001",),
        )
    fixture.source.readonly()
    try:
        with pytest.raises(SourceMappingError, match="catalogue_value_invalid"):
            fixture.adapter.search(CatalogueRequest(browse=True))
        assert (
            fixture.source.db.execute(
                "SELECT count(*) FROM schema_62.records_62"
            ).fetchone()[0]
            == 3
        )
    finally:
        fixture.source.close()


@pytest.mark.parametrize(
    "current,previous,changed",
    [
        ("Random Diet 330ml", "Random 330ml", True),
        ("Diet Random", "Random Diet", False),
        ("list Completely Different", "list Random", True),
        ("next", "list Random", False),
        ("its barcode?", "Random Diet", False),
        ("2", "Random Diet", False),
    ],
)
def test_changed_reference_is_brand_independent(current, previous, changed):
    assert changes_product_reference(current, previous) is changed


def test_actual_pending_loader_preserves_scope_but_marks_changed_modifiers(
    flow, monkeypatch
):
    from test_catalogue_chat import catalogue, product

    output = catalogue(product(), product("0008", "PEPSI LARGE", "0008"))
    executed = flow.executor.execute(
        user=flow.user,
        business_id=flow.business_id,
        tool_name="product_search",
        arguments={"query": "Pepsi"},
    )
    executed = __import__("dataclasses").replace(executed, output=output)
    owner_chat._store_catalogue_clarification(
        flow.session, flow.user, flow.business_id, flow.owner.id, executed
    )
    state = flow.owner.operational_clarification
    monkeypatch.setattr(owner_chat, "_previous_product_clarification", lambda *_: state)
    flow.executor.catalogue_scope = lambda *_: owner_chat.CatalogueSourceScope(
        1, "a" * 64, "b" * 64
    )
    flow.owner.content = "Pepsi Diet 330ml"
    pending = ORIGINAL_PENDING_LOADER(
        flow.session, flow.executor, flow.user, flow.business_id, flow.owner.id
    )
    from app.utils.product_reference import changed_candidate_reference

    assert changed_candidate_reference(flow.owner.content, pending)


def test_duplicate_names_never_become_a_unique_identity():
    fixture = mapped_catalogue(63)
    try:
        result = fixture.adapter.search(CatalogueRequest(query=fixture.family))
        assert result.status == "ambiguous"
        assert result.items[0].names == result.items[1].names
        assert (
            result.items[0].external_product_id != result.items[1].external_product_id
        )
    finally:
        fixture.source.close()


def test_changed_modifier_cannot_dispatch_a_stale_planner_search(system):
    from test_catalogue_chat import offline_provider, product
    from test_owner_chat_state import new_turn, product_plan, run

    system.items[:] = [
        product("R1", "Random 330ml Can"),
        product("R2", "Random 330ml Bottle"),
        product("D1", "Random Diet 330ml Can"),
        product("D2", "Random Diet 330ml Bottle"),
    ]
    new_turn(system, "barcode Random 330ml?")
    provider, _ = offline_provider(
        product_plan(
            "Random 330ml",
            product_presentation=dict(
                intent="details", reference="current", identifiers=["id"]
            ),
        )
    )
    run(system, provider)
    new_turn(system, "Random Diet 330ml barcode")
    provider, _ = offline_provider(
        product_plan(
            "Random 330ml",
            pending_reply="unresolved",
            product_presentation=dict(
                intent="ambiguous_followup", reference="offered", identifiers=["id"]
            ),
        )
    )
    result, _ = run(system, provider)
    assert "diet" in system.reads[-1]["query"].casefold()
    assert {
        item.external_product_id
        for item in result.operational_presentation.result.items
    } == {"D1", "D2"}

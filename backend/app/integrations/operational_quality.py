"""Full-scope SQL evidence for one operational execution contract.

Operational records are never dropped to make quantities or money look valid.
Catalogue display names and unrelated incoming relationships are not dependencies.
"""

import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, case, exists, func, literal, or_, select

from app.integrations.catalogue_quality import (
    CharacterLength,
    TrimWhitespace,
    check_deadline,
    source_data_digest,
)
from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_operational import _refs, compile_facts
from app.schemas.capabilities import CapabilityDiagnostic
from app.schemas.operational_mapping import OperationalMapping, OperationalRead

FAMILY_TOOLS = {
    "inventory": ("stock", "availability", "restocking"),
    "pricing": ("prices",),
    "sales": ("sales", "best_sellers"),
    "financial": ("revenue", "refunds"),
}
EXPLANATIONS = {
    "invalid_identity": (
        "A required fact or product identity is null, blank or "
        "outside the supported bound."
    ),
    "missing_relationship": ("A required execution relationship has no matching row."),
    "duplicate_fact": (
        "The approved fact identity or balance grain repeats, so "
        "joins or aggregation would be ambiguous."
    ),
    "invalid_text": (
        "Required units, location or classification text is missing or too long."
    ),
    "invalid_number": (
        "An authoritative numeric value is missing, non-finite or "
        "outside the supported precision/range."
    ),
    "invalid_currency": "An amount has no supported three-letter currency code.",
    "unclassified_status": (
        "The event status is neither completed nor explicitly "
        "excluded by the owner-reviewed contract."
    ),
    "invalid_event": (
        "A completed event is missing a timestamp or transaction identity."
    ),
    "invalid_amount": (
        "A completed event has missing or invalid amounts, sign or quantity."
    ),
    "invalid_dates": "A pricing validity interval is inverted or empty.",
    "ambiguous_price": (
        "Price variants overlap at the same approved product, "
        "location, unit and effective precedence."
    ),
    "invalid_restock": (
        "Restocking target is below its threshold or a required value is negative."
    ),
}


def family_mapping(mapping: OperationalMapping, family: str) -> OperationalMapping:
    fields = {
        "stock": None,
        "prices": None,
        "sales": None,
        "revenue": None,
        "refunds": None,
    }
    required = {
        "inventory": "stock",
        "pricing": "prices",
        "sales": "sales",
        "financial": "revenue",
    }[family]
    if getattr(mapping, required) is None:
        raise SourceMappingError("capability_mapping_required")
    if family == "inventory":
        fields["stock"] = mapping.stock
    elif family == "pricing":
        fields["prices"] = mapping.prices
    else:
        fields.update(sales=mapping.sales, refunds=mapping.refunds)
        if family == "financial":
            fields["revenue"] = mapping.revenue
    return OperationalMapping.model_validate(
        mapping.model_copy(update=fields).model_dump()
    )


def fact_definitions(mapping):
    return tuple(
        f for f in (mapping.stock, mapping.prices, mapping.sales, mapping.refunds) if f
    )


def execution_columns(mapping, products):
    columns, scopes = {}, {}
    for fact in fact_definitions(mapping):
        columns.setdefault(fact.object_id, set()).update(
            (*fact.key_columns, *fact.product_columns)
        )
        scopes.setdefault(fact.object_id, []).append(
            tuple((s.field.column, s.value) for s in fact.row_scopes)
        )
        for ref in _refs(fact):
            columns.setdefault(ref.object_id, set()).add(ref.column)
        if fact.product_reference == "catalogue_identity":
            columns.setdefault(products.object_id, set()).update(products.key_columns)
        for join in fact.joins:
            columns[fact.object_id].update(join.fact_columns)
            columns.setdefault(join.target_object_id, set()).update(join.target_columns)
    for ref in _refs(mapping.revenue):
        columns.setdefault(ref.object_id, set()).add(ref.column)
    return columns, scopes


def execution_discovery(mapping, discovery, products):
    ids = {products.object_id} if products else set()
    for fact in fact_definitions(mapping):
        ids.add(fact.object_id)
        ids.update(j.target_object_id for j in fact.joins)
    ids.update(r.object_id for r in _refs(mapping))
    return discovery.model_copy(
        update={
            "objects": tuple(o for o in discovery.objects if o.object_id in ids),
            "relationships": tuple(
                r
                for r in discovery.relationships
                if r.source_object_id in ids and r.target_object_id in ids
            ),
        }
    )


def blank(column):
    return or_(column.is_(None), TrimWhitespace(column, literal(" \t\r\n\u00a0")) == "")


def bad_text(column, maximum=255):
    return or_(blank(column), CharacterLength(column) > maximum)


def bad_number(column, *, nonnegative=False):
    # Numeric source types are enforced structurally. PG NaN sorts above finite
    # values and is caught by the upper bound; integer/decimal precision <=38.
    return or_(
        column.is_(None),
        column >= Decimal("1e24"),
        column < (0 if nonnegative else -Decimal("1e24")),
    )


def currency_invalid(column):
    value = func.upper(column)
    return or_(
        column.is_(None),
        CharacterLength(column) != 3,
        *(
            or_(
                func.ascii(func.nullif(func.substring(value, i, 1), "")) < 65,
                func.ascii(func.nullif(func.substring(value, i, 1), "")) > 90,
            )
            for i in (1, 2, 3)
        ),
    )


def invalid_rules(projection, fact, mapping):
    c = projection.c
    keys = [c[f"key_{i}"] for i in range(len(fact.key_columns))]
    product = [c[f"product_{i}"] for i in range(len(fact.product_columns))]
    rules = {}
    from sqlalchemy import Unicode, cast

    rules["invalid_identity"] = or_(
        *(bad_text(cast(v, Unicode), 128) for v in (*keys, *product))
    )
    missing = [c[f"joined_key_{i}"].is_(None) for i in range(len(fact.joins))]
    if fact.product_reference == "catalogue_identity":
        missing.extend(c[f"catalogue_{i}"].is_(None) for i in range(len(product)))
    # SQL Server CASE conditions require a predicate, not a bare boolean value.
    rules["missing_relationship"] = (
        or_(*missing) if missing else literal(1) == literal(0)
    )
    rules["duplicate_fact"] = c._fact_repetitions > 1
    text_fields = [
        name
        for name in ("unit", "location", "branch", "warehouse", "price_list")
        if name in c
    ]
    rules["invalid_text"] = or_(*(bad_text(c[n]) for n in text_fields))
    if hasattr(fact, "occurred_at"):
        completed = c.status.in_(fact.completed_statuses)
        if set(fact.completed_statuses) & set(fact.excluded_statuses):
            raise SourceMappingError("mapping_status_classification_overlap")
        rules["unclassified_status"] = or_(
            c.status.is_(None),
            c.status.not_in((*fact.completed_statuses, *fact.excluded_statuses)),
        )
        rules["invalid_event"] = and_(
            completed,
            or_(
                c.occurred_at.is_(None),
                *(
                    bad_text(cast(c[f"transaction_{i}"], Unicode))
                    for i in range(len(fact.transaction_columns))
                ),
                *(
                    bad_text(cast(c[f"original_transaction_{i}"], Unicode))
                    for i in range(
                        len(getattr(fact, "original_transaction_columns", ()))
                    )
                ),
            ),
        )
        amount_rules = (
            [bad_number(c.quantity * fact.quantity_multiplier, nonnegative=True)]
            if fact.quantity is not None
            else []
        )
        if fact is mapping.sales and mapping.revenue:
            amount_rules.extend(
                (
                    bad_number(
                        c.gross_amount * mapping.revenue.amount_multiplier,
                        nonnegative=True,
                    ),
                    bad_number(
                        (
                            c.net_amount
                            if mapping.revenue.net_rule == "source_net"
                            else c.gross_amount - c.discount_amount
                        )
                        * mapping.revenue.amount_multiplier
                    ),
                )
            )
        if fact is mapping.refunds and mapping.revenue:
            amount_rules.append(
                bad_number(c.amount * fact.amount_multiplier, nonnegative=True)
            )
        if (
            fact is mapping.sales
            and mapping.revenue
            and mapping.revenue.discount_amount
        ):
            amount_rules.append(bad_number(c.discount_amount, nonnegative=True))
        rules["invalid_amount"] = (
            and_(completed, or_(*amount_rules))
            if amount_rules
            else literal(1) == literal(0)
        )
        if mapping.revenue:
            rules["invalid_currency"] = and_(completed, currency_invalid(c.currency))
        # Only classified completed events require product and dimension fields.
        for name in ("invalid_identity", "missing_relationship", "invalid_text"):
            rules[name] = and_(completed, rules[name])
    elif hasattr(fact, "on_hand"):
        numbers = [bad_number(c.on_hand)]
        for name in ("reserved", "available", "reorder_point", "target_quantity"):
            if name in c:
                numbers.append(bad_number(c[name], nonnegative=name != "available"))
        rules["invalid_number"] = or_(*numbers)
        if "target_quantity" in c:
            rules["invalid_restock"] = c.target_quantity < c.reorder_point
        grain = [c[n] for n in c.keys() if n.startswith("product_") or n in text_fields]
        grouped = select(*grain).group_by(*grain).having(func.count() > 1).subquery()
        rules["duplicate_fact"] = or_(
            rules["duplicate_fact"],
            exists(
                select(1)
                .select_from(grouped)
                .where(
                    and_(
                        *(
                            a.is_not_distinct_from(b)
                            for a, b in zip(grain, grouped.c, strict=True)
                        )
                    )
                )
            ),
        )
    else:
        rules["invalid_number"] = bad_number(c.amount, nonnegative=True)
        rules["invalid_currency"] = currency_invalid(c.currency)
        if "valid_from" in c and "valid_until" in c:
            rules["invalid_dates"] = c.valid_until <= c.valid_from
        other = projection.alias("other_price")
        grain = [
            n
            for n in c.keys()
            if n.startswith("product_") or n in (*text_fields, "currency")
        ]
        overlap = [c[n].is_not_distinct_from(other.c[n]) for n in grain]
        overlap.append(or_(*(c[k.key] != other.c[k.key] for k in keys)))
        if fact.precedence == "latest_effective":
            overlap.append(c.valid_from.is_not_distinct_from(other.c.valid_from))
        if "valid_from" in c and "valid_until" in c:
            overlap.extend(
                (
                    or_(
                        c.valid_until.is_(None),
                        other.c.valid_from.is_(None),
                        c.valid_until > other.c.valid_from,
                    ),
                    or_(
                        other.c.valid_until.is_(None),
                        c.valid_from.is_(None),
                        other.c.valid_until > c.valid_from,
                    ),
                )
            )
        rules["ambiguous_price"] = exists(
            select(1).select_from(other).where(and_(*overlap))
        )
    return rules


@dataclass
class OperationalQuality:
    total: int
    invalid: int
    reasons: dict[str, int]
    records: dict[str, int]
    diagnostics: tuple[CapabilityDiagnostic, ...]
    digest: str


def evaluate_operational(adapter, connection, *, diagnostics=True, fingerprint=True):
    connector, mapping = adapter.catalogue.connector, adapter.mapping
    discovery = adapter.catalogue.discovery
    products = adapter.catalogue.mapping
    deadline = time.monotonic() + 30
    connector.assert_schema(discovery, connection, dependencies=True)
    total = invalid_count = 0
    reasons, records, details = {}, {}, []
    for index, fact in enumerate(fact_definitions(mapping)):
        check_deadline(deadline)
        request = OperationalRead(
            capability="stock",
            catalogue_revision=mapping.catalogue_revision,
            mapping_revision=adapter.revision,
        )
        query, params = compile_facts(
            mapping, discovery, products, fact, request, validation=True
        )
        projection = query.order_by(None).subquery(f"capability_facts_{index}")
        # Count each complete fact key once. A correlated duplicate-key scan
        # timed out on the restored source even though its key was enforced.
        projection = select(
            *projection.c,
            func.count()
            .over(
                partition_by=[
                    projection.c[f"key_{i}"] for i in range(len(fact.key_columns))
                ]
            )
            .label("_fact_repetitions"),
        ).subquery(f"counted_facts_{index}")
        rules = invalid_rules(projection, fact, mapping)
        invalid = or_(*rules.values())
        flags = (
            select(
                *(case((rule, 1), else_=0).label(name) for name, rule in rules.items())
            )
            .select_from(projection)
            .subquery("quality_flags")
        )
        flagged_invalid = or_(*(flags.c[name] == 1 for name in rules))
        aggregates = [
            func.count().label("total"),
            func.coalesce(func.sum(case((flagged_invalid, 1), else_=0)), 0).label(
                "invalid"
            ),
        ]
        aggregates.extend(
            func.coalesce(func.sum(flags.c[name]), 0).label(name) for name in rules
        )
        counts = (
            connection.execute(select(*aggregates).select_from(flags), params)
            .mappings()
            .one()
        )
        records[f"{fact.capability}:{fact.object_id}"] = int(counts["total"])
        total += int(counts["total"])
        invalid_count += int(counts["invalid"])
        for name in rules:
            reasons[name] = reasons.get(name, 0) + int(counts[name])
        if diagnostics and len(details) < 50 and counts["invalid"]:
            columns = [projection.c[f"key_{i}"] for i in range(len(fact.key_columns))]
            flags = [
                case((rule, 1), else_=0).label(name) for name, rule in rules.items()
            ]
            rows = connection.execute(
                select(*columns, *flags)
                .where(invalid)
                .order_by(*columns)
                .limit(50 - len(details)),
                params,
            ).mappings()
            for row in rows:
                failed = tuple(name for name in rules if row[name])
                details.append(
                    CapabilityDiagnostic(
                        object_id=fact.object_id,
                        identifier=tuple(
                            str(row[c.key])[:128] if row[c.key] is not None else None
                            for c in columns
                        ),
                        rules=failed,
                        explanation=" ".join(EXPLANATIONS[n] for n in failed),
                    )
                )
    columns, scopes = execution_columns(mapping, products)
    digest = (
        source_data_digest(
            connection,
            products,
            discovery,
            deadline,
            column_scope=columns,
            row_scopes=scopes,
        )
        if fingerprint
        else ""
    )
    connector.assert_schema(discovery, connection, dependencies=True)
    check_deadline(deadline)
    return OperationalQuality(
        total, invalid_count, reasons, records, tuple(details), digest
    )

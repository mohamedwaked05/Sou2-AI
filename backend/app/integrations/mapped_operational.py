"""Bounded mapped facts and deterministic Decimal calculations.

Only approved identifiers and many-to-one joins are compiled. Reports exceeding
the fact budget fail rather than returning deceptively partial financial totals.
"""

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Select,
    Table,
    Unicode,
    and_,
    bindparam,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)

from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import (
    decode_product_key,
    encode_product_key,
)
from app.schemas.operational_mapping import (
    FieldRef,
    OperationalMapping,
    OperationalRead,
    OperationalReport,
    OperationalRow,
    OperationalTotal,
    PriceMapping,
    SourceFacts,
)
from app.schemas.source_mapping import CatalogueRequest, ProductMapping, SchemaDiscovery

MAX_FACTS = 10_000


def _refs(value: object) -> Iterator[FieldRef]:
    if isinstance(value, FieldRef):
        yield value
    elif hasattr(type(value), "model_fields"):
        for name in type(value).model_fields:
            yield from _refs(getattr(value, name))
    elif isinstance(value, tuple):
        for item in value:
            yield from _refs(item)


def validate_operational_mapping(
    mapping: OperationalMapping,
    discovery: SchemaDiscovery,
    product_mapping: ProductMapping | None,
) -> None:
    if (
        mapping.sales
        and mapping.refunds
        and mapping.sales.object_id == mapping.refunds.object_id
    ):
        disjoint = any(
            left.field == right.field
            and type(left.value) is type(right.value)
            and left.value != right.value
            for left in mapping.sales.row_scopes
            for right in mapping.refunds.row_scopes
        )
        if not disjoint:
            raise SourceMappingError("mapping_refund_scope_overlap")
    objects = {o.object_id: o for o in discovery.objects}
    if (
        mapping.source_fingerprint != discovery.source_fingerprint
        or mapping.schema_fingerprint != discovery.schema_fingerprint
    ):
        raise SourceMappingError("mapping_schema_changed")
    product = objects[product_mapping.object_id] if product_mapping else None
    product_columns = {c.name: c for c in product.columns} if product else {}
    product_scopes = product_mapping.row_scopes if product_mapping else ()
    if product_scopes and mapping.external_scope != "partitioned":
        raise SourceMappingError("mapping_fact_partition_required")
    if (
        mapping.external_scope == "partitioned"
        and product_mapping is not None
        and not product_scopes
    ):
        raise SourceMappingError("mapping_catalogue_partition_required")
    for fact in (mapping.stock, mapping.prices, mapping.sales, mapping.refunds):
        if fact is None:
            continue
        if product_mapping is None and fact.product_reference == "catalogue_identity":
            raise SourceMappingError("capability_identity_mapping_required")
        obj = objects.get(fact.object_id)
        if obj is None:
            raise SourceMappingError("mapping_object_unknown")
        cols = {c.name: c for c in obj.columns}
        if not any(tuple(fact.key_columns) == tuple(k) for k in obj.unique_keys) or any(
            n not in cols for n in fact.key_columns
        ):
            raise SourceMappingError("mapping_fact_key_not_unique")
        if (
            product_mapping is not None
            and fact.product_reference != "none"
            and len(fact.product_columns) != len(product_mapping.key_columns)
        ):
            raise SourceMappingError("mapping_product_join_invalid")
        if fact.product_reference == "none":
            for scope in product_scopes:
                if not any(
                    type(s.value) is type(scope.value) and s.value == scope.value
                    for s in fact.row_scopes
                ):
                    raise SourceMappingError("mapping_partition_product_mismatch")
        for left, right in zip(
            fact.product_columns,
            product_mapping.key_columns
            if product_mapping and fact.product_reference != "none"
            else fact.product_columns,
            strict=True,
        ):
            if (
                left not in cols
                or (
                    product_mapping is not None
                    and cols[left].kind != product_columns[right].kind
                )
                or cols[left].kind not in {"text", "integer"}
            ):
                raise SourceMappingError("mapping_product_join_invalid")
            for scope in product_scopes:
                if scope.column == right and not any(
                    part.field.object_id == fact.object_id
                    and part.field.column == left
                    and type(part.value) is type(scope.value)
                    and part.value == scope.value
                    for part in fact.row_scopes
                ):
                    raise SourceMappingError("mapping_partition_product_mismatch")
        reachable = {fact.object_id}
        if fact.product_reference == "catalogue_identity":
            reachable.add(product_mapping.object_id)
        for join in fact.joins:
            target = objects.get(join.target_object_id)
            if target is None or join.target_object_id in reachable:
                raise SourceMappingError("mapping_relationship_unknown")
            target_cols = {c.name: c for c in target.columns}
            if not any(
                set(join.target_columns) == set(k) for k in target.unique_keys
            ) or len(set(join.fact_columns)) != len(join.fact_columns):
                raise SourceMappingError("mapping_join_not_unique")
            for left, right in zip(join.fact_columns, join.target_columns, strict=True):
                if (
                    left not in cols
                    or right not in target_cols
                    or cols[left].kind != target_cols[right].kind
                ):
                    raise SourceMappingError("mapping_join_type")
            reachable.add(join.target_object_id)
        extras = (mapping.revenue,) if fact is mapping.sales and mapping.revenue else ()
        for ref in (*_refs(fact), *_refs(extras)):
            if ref.object_id not in reachable or ref.column not in {
                c.name for c in objects[ref.object_id].columns
            }:
                raise SourceMappingError("mapping_column_unknown")
        transactions = getattr(fact, "transaction_columns", ())
        if transactions:
            transaction_objects = {ref.object_id for ref in transactions}
            if len(transaction_objects) != 1:
                raise SourceMappingError("mapping_transaction_identity_invalid")
            document = objects[transactions[0].object_id]
            columns = tuple(ref.column for ref in transactions)
            if len(set(columns)) != len(columns):
                raise SourceMappingError("mapping_transaction_identity_invalid")
            if fact.transaction_identity == "enforced_header_key" and not any(
                set(columns) == set(key) for key in document.unique_keys
            ):
                raise SourceMappingError("mapping_transaction_identity_unverified")
        numeric_fields = (
            "on_hand",
            "reserved",
            "available",
            "amount",
            "quantity",
            "reorder_point",
            "target_quantity",
        )
        for name in numeric_fields:
            ref = getattr(fact, name, None)
            if ref and next(
                c.kind for c in objects[ref.object_id].columns if c.name == ref.column
            ) not in {"decimal", "integer"}:
                raise SourceMappingError("mapping_numeric_type")
        for name in ("quantity", "amount", "on_hand", "reserved", "available"):
            ref = getattr(fact, name, None)
            if ref and ref.object_id != fact.object_id:
                raise SourceMappingError("mapping_value_not_at_fact_grain")
        if fact is mapping.sales and mapping.revenue:
            for ref in (
                mapping.revenue.gross_amount,
                mapping.revenue.net_amount,
                mapping.revenue.discount_amount,
            ):
                if ref is None:
                    continue
                if ref.object_id != fact.object_id:
                    raise SourceMappingError("mapping_value_not_at_fact_grain")
        expected_types = {
            "unit": {"text"},
            "location": {"text", "integer"},
            "branch": {"text", "integer"},
            "warehouse": {"text", "integer"},
            "currency": {"text"},
            "price_list": {"text"},
            "category": {"text"},
            "status": {"text"},
            "observed_at": {"date"},
            "occurred_at": {"date"},
            "valid_from": {"date"},
            "valid_until": {"date"},
        }
        for name, allowed in expected_types.items():
            ref = getattr(fact, name, None)
            if (
                ref
                and next(
                    c.kind
                    for c in objects[ref.object_id].columns
                    if c.name == ref.column
                )
                not in allowed
            ):
                raise SourceMappingError("mapping_field_type")
        for name in ("valid_from", "valid_until", "occurred_at", "observed_at"):
            ref = getattr(fact, name, None)
            if ref is None:
                continue
            column = next(
                c for c in objects[ref.object_id].columns if c.name == ref.column
            )
            signature = (column.type_signature or "").lower().split(":")[0]
            if signature == "date" and not (
                name == "occurred_at" and fact.timestamp_basis == "business_date"
            ):
                raise SourceMappingError("mapping_date_only_time_semantics_unsupported")
            if (
                name == "occurred_at"
                and fact.timestamp_basis == "business_date"
                and signature != "date"
            ):
                raise SourceMappingError("mapping_business_date_type_required")
            if (
                name == "occurred_at"
                and fact.timestamp_basis == "source_local"
                and signature
                not in {
                    "timestamp without time zone",
                    "datetime",
                    "datetime2",
                    "smalldatetime",
                }
            ):
                raise SourceMappingError("mapping_local_timestamp_type_unsupported")
            if isinstance(fact, PriceMapping) and not (
                signature == "timestamp with time zone" or signature == "datetimeoffset"
            ):
                raise SourceMappingError("mapping_price_timezone_unsupported")
        if fact is mapping.sales and mapping.revenue:
            for name in ("gross_amount", "net_amount", "discount_amount"):
                ref = getattr(mapping.revenue, name)
                if ref is None:
                    continue
                if next(
                    c.kind
                    for c in objects[ref.object_id].columns
                    if c.name == ref.column
                ) not in {"decimal", "integer"}:
                    raise SourceMappingError("mapping_numeric_type")
        for scope in fact.row_scopes:
            if scope.field.object_id != fact.object_id:
                raise SourceMappingError("mapping_partition_must_scope_fact")
            col = next(
                c
                for c in objects[scope.field.object_id].columns
                if c.name == scope.field.column
            )
            if (
                (col.kind == "integer" and type(scope.value) is not int)
                or (col.kind == "text" and type(scope.value) is not str)
                or col.kind not in {"integer", "text"}
            ):
                raise SourceMappingError("mapping_partition_type")


def compile_facts(
    mapping: OperationalMapping,
    discovery: SchemaDiscovery,
    products: ProductMapping | None,
    fact: SourceFacts,
    request: OperationalRead,
    *,
    validation: bool = False,
) -> tuple[Select, dict[str, Any]]:
    """Return SELECT and bound parameters, portable to PostgreSQL/SQL Server."""
    validate_operational_mapping(mapping, discovery, products)
    metadata = MetaData()
    kinds = {
        "integer": Integer,
        "decimal": Numeric(asdecimal=True),
        "date": DateTime(timezone=True),
    }
    tables = {
        o.object_id: Table(
            o.name,
            metadata,
            *(
                Column(
                    c.name,
                    Date
                    if (c.type_signature or "").lower().split(":")[0] == "date"
                    else kinds.get(c.kind, Unicode),
                    quote=True,
                )
                for c in o.columns
            ),
            schema=o.schema_name,
            quote=True,
            quote_schema=True,
        )
        for o in discovery.objects
    }
    root = tables[fact.object_id]
    product = tables[products.object_id] if products else None
    # A self-fact product source is not a second occurrence of the same table.
    if products and fact.object_id == products.object_id:
        product = product.alias("catalogue_product")
    conditions = (
        [
            root.c[a] == product.c[b]
            for a, b in zip(
                fact.product_columns,
                products.key_columns
                if products and fact.product_reference != "none"
                else fact.product_columns,
                strict=True,
            )
        ]
        if fact.product_reference == "catalogue_identity"
        else []
    )
    if products:
        conditions.extend(product.c[s.column] == s.value for s in products.row_scopes)
    joined = (
        root.outerjoin(product, and_(*conditions))
        if fact.product_reference == "catalogue_identity"
        else root
    )
    join_keys = []
    missing = (
        [product.c[products.key_columns[0]].is_(None)]
        if fact.product_reference == "catalogue_identity"
        else []
    )
    for join in fact.joins:
        target = tables[join.target_object_id]
        join_keys.append(
            target.c[join.target_columns[0]].label(f"joined_key_{len(join_keys)}")
        )
        missing.append(target.c[join.target_columns[0]].is_(None))
        joined = joined.outerjoin(
            target,
            and_(
                *(
                    root.c[a] == target.c[b]
                    for a, b in zip(join.fact_columns, join.target_columns, strict=True)
                )
            ),
        )
    fields = {
        name: getattr(fact, name)
        for name in type(fact).model_fields
        if isinstance(getattr(fact, name), FieldRef)
    }
    if fact is mapping.sales and mapping.revenue:
        fields.update(
            {
                name: getattr(mapping.revenue, name)
                for name in type(mapping.revenue).model_fields
                if isinstance(getattr(mapping.revenue, name), FieldRef)
            }
        )
    fields.update(
        {
            f"transaction_{i}": ref
            for i, ref in enumerate(getattr(fact, "transaction_columns", ()))
        }
    )
    fields.update(
        {
            f"original_transaction_{i}": ref
            for i, ref in enumerate(getattr(fact, "original_transaction_columns", ()))
        }
    )
    selected = [root.c[n].label(f"key_{i}") for i, n in enumerate(fact.key_columns)]
    selected += join_keys
    selected += [
        root.c[n].label(f"product_{i}") for i, n in enumerate(fact.product_columns)
    ]
    if fact.product_reference == "catalogue_identity":
        selected += [
            product.c[n].label(f"catalogue_{i}")
            for i, n in enumerate(products.key_columns)
        ]
    money = mapping.revenue if fact is mapping.sales else fact
    currency_codes = getattr(money, "currency_codes", {})
    for name, ref in fields.items():
        column = tables[ref.object_id].c[ref.column]
        if name == "currency" and currency_codes:
            # The reviewed dictionary is bound data. Unmapped codes stay invalid
            # rather than inheriting a guessed default currency.
            column = case(currency_codes, value=column, else_=None)
        selected.append(column.label(name))
    missing_join = or_(*missing) if missing else literal(1) == literal(0)
    query, params = select(*selected).select_from(joined), {}
    for i, scope in enumerate(fact.row_scopes):
        params[f"scope_{i}"] = scope.value
        query = query.where(
            tables[scope.field.object_id].c[scope.field.column]
            == bindparam(f"scope_{i}")
        )
    if request.product_id:
        if not fact.product_columns:
            raise SourceMappingError("product_filter_unsupported")
        keys = decode_product_key(
            request.product_id,
            len(products.key_columns) if products else len(fact.product_columns),
        )
        obj = next(o for o in discovery.objects if o.object_id == fact.object_id)
        for i, (name, key) in enumerate(zip(fact.product_columns, keys, strict=True)):
            kind = next(c.kind for c in obj.columns if c.name == name)
            try:
                params[f"product_{i}"] = int(key) if kind == "integer" else key
            except ValueError:
                raise SourceMappingError("invalid_product_identifier") from None
            query = query.where(root.c[name] == bindparam(f"product_{i}"))
    for name in ("location", "branch", "warehouse", "category", "price_list"):
        value = getattr(request, name)
        if value is not None:
            if name not in fields:
                raise SourceMappingError(f"{name}_filter_unsupported")
            ref = fields[name]
            kind = next(
                c.kind
                for o in discovery.objects
                if o.object_id == ref.object_id
                for c in o.columns
                if c.name == ref.column
            )
            try:
                params[f"filter_{name}"] = int(value) if kind == "integer" else value
            except ValueError:
                raise SourceMappingError("invalid_location_identifier") from None
            query = query.where(
                or_(
                    tables[ref.object_id].c[ref.column] == bindparam(f"filter_{name}"),
                    missing_join,
                )
            )
    if hasattr(fact, "occurred_at") and not validation:
        ref = fields["occurred_at"]
        column = tables[ref.object_id].c[ref.column]
        tz = UTC if fact.timestamp_basis == "utc" else ZoneInfo(mapping.timezone)
        # UTC timestamps are required to be stored as UTC; source_local uses
        # naive local wall-clock columns and rejects ambiguous DST intervals.
        start, end = request.start.astimezone(tz), request.end.astimezone(tz)
        if fact.timestamp_basis == "business_date":
            if any(
                (bound.hour, bound.minute, bound.second, bound.microsecond)
                != (0, 0, 0, 0)
                for bound in (start, end)
            ):
                raise SourceMappingError("business_date_requires_complete_local_days")
            start, end = start.date(), end.date()
        elif fact.timestamp_basis == "source_local":
            cursor = request.start.astimezone(UTC)
            offset = cursor.astimezone(tz).utcoffset()
            while cursor <= request.end.astimezone(UTC):
                if cursor.astimezone(tz).utcoffset() != offset:
                    raise SourceMappingError("local_timestamp_dst_interval_unsupported")
                cursor += timedelta(minutes=30)
            if start.fold or end.fold:
                raise SourceMappingError("local_timestamp_dst_interval_unsupported")
            start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        else:
            source_column = next(
                c
                for o in discovery.objects
                if o.object_id == ref.object_id
                for c in o.columns
                if c.name == ref.column
            )
            signature = (source_column.type_signature or "").lower().split(":")[0]
            if signature in {
                "timestamp without time zone",
                "datetime",
                "datetime2",
                "smalldatetime",
            }:
                # The approved UTC basis is explicit. Bind naive UTC instants
                # to naive columns without consulting the database session zone.
                start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        params.update(start=start, end=end, statuses=fact.completed_statuses)
        status = fields["status"]
        query = query.where(
            column >= bindparam("start"),
            column < bindparam("end"),
            tables[status.object_id]
            .c[status.column]
            .in_(bindparam("statuses", expanding=True)),
        )
    if isinstance(fact, PriceMapping) and not validation:
        params["as_of"] = request.as_of.astimezone(UTC)
        for name, direction in (("valid_from", "start"), ("valid_until", "end")):
            if name in fields:
                ref = fields[name]
                col = tables[ref.object_id].c[ref.column]
                condition = (
                    col <= bindparam("as_of")
                    if direction == "start"
                    else col > bindparam("as_of")
                )
                query = query.where(or_(col.is_(None), condition))
    query = query.order_by(*(root.c[n] for n in fact.key_columns))
    return (query if validation else query.limit(MAX_FACTS + 1)), params


def decimal_value(value: object, *, nonnegative: bool = False) -> Decimal:
    if value is None or isinstance(value, (bool, float)):
        raise SourceMappingError("operational_numeric_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation, TypeError, ValueError:
        raise SourceMappingError("operational_numeric_invalid") from None
    if (
        not result.is_finite()
        or abs(result) >= Decimal("1e24")
        or (nonnegative and result < 0)
    ):
        raise SourceMappingError("operational_numeric_invalid")
    if len(result.as_tuple().digits) > 38 or result.as_tuple().exponent < -38:
        raise SourceMappingError("operational_numeric_precision_unsupported")
    return result


def text_value(value: object) -> str:
    if value is None or not str(value).strip() or len(str(value)) > 255:
        raise SourceMappingError("operational_text_invalid")
    return str(value).strip()


@dataclass(frozen=True)
class OperationalSourceContext:
    """Connection authority for a contract without catalogue dependencies."""

    connector: object
    discovery: SchemaDiscovery
    revision_version: int = 0
    mapping: ProductMapping | None = None

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.connector.enforced_query_timeout_seconds


class MappedOperationalSource:
    def __init__(
        self,
        catalogue,
        mapping: OperationalMapping,
        revision: int,
        *,
        business_id,
        source_id,
    ):
        if (
            mapping.business_id != business_id
            or mapping.source_id != source_id
            or mapping.catalogue_revision != catalogue.revision_version
        ):
            raise SourceMappingError("mapping_scope_mismatch")
        validate_operational_mapping(mapping, catalogue.discovery, catalogue.mapping)
        self.catalogue, self.mapping, self.revision = catalogue, mapping, revision

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.catalogue.enforced_query_timeout_seconds

    def _read(
        self, fact: SourceFacts, request: OperationalRead
    ) -> Sequence[Mapping[str, Any]]:
        statement, params = compile_facts(
            self.mapping,
            self.catalogue.discovery,
            self.catalogue.mapping,
            fact,
            request,
        )
        connector = self.catalogue.connector
        current = getattr(self, "_execution_connection", None)
        with (
            nullcontext(current)
            if current is not None
            else connector.connection() as connection
        ):
            connector.assert_schema(
                self.catalogue.discovery,
                connection,
                **(
                    {"dependencies": True}
                    if getattr(self, "independent", False)
                    else {}
                ),
            )
            rows = (
                connection.execute(statement, params)
                .mappings()
                .fetchmany(MAX_FACTS + 1)
            )
            connector.assert_schema(
                self.catalogue.discovery,
                connection,
                **(
                    {"dependencies": True}
                    if getattr(self, "independent", False)
                    else {}
                ),
            )
        if len(rows) > MAX_FACTS:
            raise SourceMappingError("operational_fact_budget_exceeded")
        seen = set()
        for row in rows:
            key = tuple(row[f"key_{i}"] for i in range(len(fact.key_columns)))
            if any(v is None for v in key) or key in seen:
                raise SourceMappingError("operational_duplicate_fact")
            seen.add(key)
            if any(row[f"joined_key_{i}"] is None for i in range(len(fact.joins))):
                raise SourceMappingError("operational_join_missing")
            for i in range(len(getattr(fact, "original_transaction_columns", ()))):
                text_value(row[f"original_transaction_{i}"])
            if fact.product_reference == "catalogue_identity" and any(
                row[f"catalogue_{i}"] is None for i in range(len(fact.product_columns))
            ):
                raise SourceMappingError("operational_product_invalid")
        return rows

    def _identity(
        self, row: Mapping[str, Any], fact: SourceFacts, *, money: bool = False
    ) -> tuple[str, str | None, str, str | None, str | None, str | None]:
        product = (
            encode_product_key(
                tuple(
                    str(row[f"product_{i}"]) for i in range(len(fact.product_columns))
                )
            )
            if fact.product_columns
            else None
        )
        currency = text_value(row.get("currency")).upper() if money else None
        if currency and (
            len(currency) != 3 or not currency.isascii() or not currency.isalpha()
        ):
            raise SourceMappingError("operational_currency_invalid")
        return (
            product,
            text_value(row["location"]) if fact.location else None,
            text_value(row["unit"]) if fact.unit else None,
            currency,
            text_value(row["branch"])
            if fact.branch and row["branch"] is not None
            else None,
            text_value(row["warehouse"])
            if fact.warehouse and row["warehouse"] is not None
            else None,
        )

    def read(self, request: OperationalRead) -> OperationalReport:
        with localcontext() as context:
            context.prec = 80
            if getattr(self, "independent", False):
                from app.integrations.operational_quality import evaluate_operational

                with self.catalogue.connector.connection(consistent=True) as connection:
                    result = evaluate_operational(
                        self, connection, diagnostics=False, fingerprint=False
                    )
                    if result.invalid:
                        raise SourceMappingError("capability_data_quality_changed")
                    self._execution_connection = connection
                    try:
                        return self._report(request)
                    finally:
                        self._execution_connection = None
            return self._report(request)

    def _report(self, request: OperationalRead) -> OperationalReport:
        if (
            request.mapping_revision != self.revision
            or request.catalogue_revision != self.mapping.catalogue_revision
        ):
            raise SourceMappingError("mapping_selection_stale")
        if request.product_id and not getattr(self, "independent", False):
            product = self.catalogue.search(
                CatalogueRequest(
                    external_product_id=request.product_id,
                    mapping_version=request.catalogue_revision,
                )
            )
            if product.status != "resolved":
                raise SourceMappingError("operational_product_not_found")
        base = dict(
            business_meaning=getattr(self, "business_meaning", None),
            capability=request.capability,
            business_id=self.mapping.business_id,
            source_id=self.mapping.source_id,
            mapping_revision=self.revision,
            catalogue_revision=self.mapping.catalogue_revision,
            source_fingerprint=self.mapping.source_fingerprint,
            schema_fingerprint=self.mapping.schema_fingerprint,
            timezone=self.mapping.timezone,
            start=request.start,
            end=request.end,
            as_of=request.as_of,
            revenue_basis=self.mapping.revenue.net_basis
            if self.mapping.revenue
            else None,
        )
        if request.capability not in self.mapping.capabilities:
            return OperationalReport(
                **base, status="unsupported", reason="capability_not_approved"
            )
        try:
            totals = ()
            if request.capability in {"stock", "availability", "restocking"}:
                rows = self._stock(request)
            elif request.capability == "prices":
                rows = self._prices(request)
                if len(rows) > 1:
                    return OperationalReport(
                        **base,
                        status="ambiguous",
                        reason="price_scope_required",
                        rows=tuple(rows[: request.limit]),
                        truncated=len(rows) > request.limit,
                    )
            else:
                rows, totals = self._sales(request)
                if request.capability == "sales":
                    base.update(self._sales_coverage(request))
            return OperationalReport(
                **base,
                status="ok" if rows else "not_found",
                reason="no_records_in_requested_period"
                if not rows and request.capability == "sales"
                else None,
                rows=tuple(rows[: request.limit]),
                totals=totals,
                truncated=len(rows) > request.limit,
            )
        except ValidationError, InvalidOperation, OverflowError:
            raise SourceMappingError("operational_value_invalid") from None

    def _sales_coverage(self, request: OperationalRead) -> dict:
        """Observed dates for the same approved scope, without a period filter."""
        fact = self.mapping.sales
        statement, params = compile_facts(
            self.mapping,
            self.catalogue.discovery,
            self.catalogue.mapping,
            fact,
            request,
            validation=True,
        )
        events = statement.order_by(None).subquery()
        query = select(
            func.min(events.c.occurred_at), func.max(events.c.occurred_at)
        ).where(events.c.status.in_(fact.completed_statuses))
        connector = self.catalogue.connector
        current = getattr(self, "_execution_connection", None)
        with (
            nullcontext(current)
            if current is not None
            else connector.connection() as connection
        ):
            checks = (
                {"dependencies": True} if getattr(self, "independent", False) else {}
            )
            connector.assert_schema(self.catalogue.discovery, connection, **checks)
            first, last = connection.execute(query, params).one()
            connector.assert_schema(self.catalogue.discovery, connection, **checks)

        def source_date(value):
            if isinstance(value, datetime):
                if fact.timestamp_basis == "utc":
                    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
                    value = value.astimezone(ZoneInfo(self.mapping.timezone))
                return value.date()
            return value

        return {
            "source_first_date": source_date(first),
            "source_last_date": source_date(last),
        }

    def _stock(self, request: OperationalRead) -> list[OperationalRow]:
        fact = self.mapping.stock
        result, seen = [], set()
        for row in self._read(fact, request):
            identity = self._identity(row, fact)
            if identity in seen:
                raise SourceMappingError("operational_stock_grain_ambiguous")
            seen.add(identity)
            on_hand = decimal_value(row["on_hand"])
            reserved = (
                decimal_value(row["reserved"], nonnegative=True)
                if fact.reserved
                else None
            )
            available = None
            if fact.availability_rule == "source":
                available = decimal_value(row["available"])
            elif fact.availability_rule == "subtract_complete_reservations":
                available = on_hand - reserved
            if available is not None and fact.negative_policy == "clamp_available_zero":
                available = max(available, Decimal(0))
            point = (
                decimal_value(row["reorder_point"], nonnegative=True)
                if fact.reorder_point
                else None
            )
            target = (
                decimal_value(row["target_quantity"], nonnegative=True)
                if fact.target_quantity
                else None
            )
            if point is not None and target < point:
                raise SourceMappingError("operational_restock_policy_invalid")
            basis = available if fact.restock_basis == "available" else on_hand
            if request.capability == "restocking":
                needs_stock = (
                    basis < point if fact.reorder_trigger == "below" else basis <= point
                )
                if not needs_stock:
                    continue
            observed = row.get("observed_at")
            if observed is not None and not isinstance(observed, datetime):
                raise SourceMappingError("operational_freshness_timestamp_invalid")
            if observed is not None and observed.tzinfo is None:
                zone = (
                    UTC
                    if fact.observed_timezone == "utc"
                    else ZoneInfo(self.mapping.timezone)
                )
                if (
                    observed.replace(tzinfo=zone, fold=0).utcoffset()
                    != observed.replace(tzinfo=zone, fold=1).utcoffset()
                ):
                    raise SourceMappingError("operational_freshness_time_ambiguous")
                observed = observed.replace(tzinfo=zone)
            result.append(
                OperationalRow(
                    product_id=identity[0],
                    location=identity[1],
                    branch=identity[4],
                    warehouse=identity[5],
                    unit=identity[2],
                    on_hand=on_hand,
                    reserved=reserved,
                    available=available,
                    reorder_point=point,
                    target_quantity=target,
                    recommended_quantity=max(target - basis, Decimal(0))
                    if request.capability == "restocking"
                    else None,
                    observed_at=observed,
                )
            )
        return result

    def _prices(self, request: OperationalRead) -> list[OperationalRow]:
        fact = self.mapping.prices
        rows = self._read(fact, request)
        # Precedence never crosses currencies, selling units, locations or lists.
        groups = defaultdict(list)
        for row in rows:
            identity = (
                *self._identity(row, fact, money=True),
                text_value(row["price_list"]) if fact.price_list else None,
            )
            groups[identity].append(row)
        result = []
        for identity, choices in groups.items():
            if fact.precedence == "latest_effective":
                if any(r["valid_from"] is None for r in choices):
                    raise SourceMappingError("operational_price_precedence_ambiguous")
                newest = max(r["valid_from"] for r in choices)
                choices = [r for r in choices if r["valid_from"] == newest]
            for row in choices:
                result.append(
                    OperationalRow(
                        product_id=identity[0],
                        location=identity[1],
                        branch=identity[4],
                        warehouse=identity[5],
                        unit=identity[2],
                        currency=identity[3],
                        price=decimal_value(row["amount"], nonnegative=True),
                        price_list=identity[6],
                        tax_basis=fact.tax_basis,
                    )
                )
        return result

    def _sales(
        self, request: OperationalRead
    ) -> tuple[list[OperationalRow], tuple[OperationalTotal, ...]]:
        mapping = self.mapping
        if request.rank_by == "net_revenue" and not mapping.revenue:
            raise SourceMappingError("revenue_not_approved")
        if (
            mapping.sales.grain == "transaction_events"
            and mapping.revenue is None
            and mapping.refunds is None
        ):
            return self._transaction_counts(request)
        groups = {}
        money = bool(mapping.revenue)

        def group(identity):
            if identity not in groups:
                groups[identity] = dict(
                    gross_quantity=Decimal(0),
                    returned_quantity=Decimal(0),
                    gross_revenue=Decimal(0) if money else None,
                    net_revenue=Decimal(0) if money else None,
                    refunds=Decimal(0) if money and mapping.refunds else None,
                    transactions=set(),
                )
            return groups[identity]

        for row in self._read(mapping.sales, request):
            identity = self._identity(row, mapping.sales, money=money)
            values = group(identity)
            if mapping.sales.quantity is not None:
                values["gross_quantity"] += decimal_value(
                    decimal_value(row["quantity"]) * mapping.sales.quantity_multiplier,
                    nonnegative=True,
                )
            transaction = tuple(
                row[f"transaction_{i}"]
                for i in range(len(mapping.sales.transaction_columns))
            )
            if any(v is None or not str(v).strip() for v in transaction):
                raise SourceMappingError("operational_transaction_invalid")
            values["transactions"].add(transaction)
            if money:
                values["gross_revenue"] += decimal_value(
                    decimal_value(row["gross_amount"])
                    * mapping.revenue.amount_multiplier,
                    nonnegative=True,
                )
                net = (
                    decimal_value(row["net_amount"])
                    if mapping.revenue.net_rule == "source_net"
                    else decimal_value(row["gross_amount"])
                    - decimal_value(row["discount_amount"], nonnegative=True)
                )
                values["net_revenue"] += net * mapping.revenue.amount_multiplier
        if mapping.refunds:
            for row in self._read(mapping.refunds, request):
                identity = self._identity(row, mapping.refunds, money=money)
                values = group(identity)
                if mapping.refunds.quantity is not None:
                    values["returned_quantity"] += decimal_value(
                        decimal_value(row["quantity"])
                        * mapping.refunds.quantity_multiplier,
                        nonnegative=True,
                    )
                if money:
                    amount = decimal_value(
                        decimal_value(row["amount"])
                        * mapping.refunds.amount_multiplier,
                        nonnegative=True,
                    )
                    values["refunds"] += amount
                    if mapping.revenue.net_basis == "before_refunds":
                        values["net_revenue"] -= amount
        elif request.capability == "refunds":
            raise SourceMappingError("refunds_not_approved")
        if request.capability == "best_sellers" and not any(
            (request.location, request.branch, request.warehouse)
        ):
            consolidated = {}
            for identity, values in groups.items():
                key = (identity[0], None, identity[2], identity[3], None, None)
                if key not in consolidated:
                    consolidated[key] = {
                        **values,
                        "transactions": set(values["transactions"]),
                    }
                else:
                    for name, value in values.items():
                        if name == "transactions":
                            consolidated[key][name].update(value)
                        elif value is not None:
                            consolidated[key][name] += value
            groups = consolidated
        quantity_totals, money_totals = {}, {}
        for identity, values in groups.items():
            unit, currency = identity[2], identity[3]
            if unit is not None:
                qt = quantity_totals.setdefault(
                    unit,
                    dict(
                        gross_quantity=Decimal(0),
                        returned_quantity=Decimal(0),
                        transactions=set(),
                    ),
                )
                qt["gross_quantity"] += values["gross_quantity"]
                qt["returned_quantity"] += values["returned_quantity"]
                qt["transactions"].update(values["transactions"])
            if money:
                mt = money_totals.setdefault(
                    currency,
                    dict(
                        gross_revenue=Decimal(0),
                        net_revenue=Decimal(0),
                        refunds=Decimal(0) if mapping.refunds else None,
                        transactions=set(),
                    ),
                )
                for name in ("gross_revenue", "net_revenue", "refunds"):
                    if values[name] is not None:
                        mt[name] += values[name]
                mt["transactions"].update(values["transactions"])
        totals = []
        for unit, values in sorted(quantity_totals.items()):
            transactions = values.pop("transactions")
            returned = (
                values["returned_quantity"]
                if mapping.refunds or mapping.sales.returns_semantics == "no_returns"
                else None
            )
            totals.append(
                OperationalTotal(
                    unit=unit,
                    gross_quantity=values["gross_quantity"],
                    returned_quantity=returned,
                    net_quantity=values["gross_quantity"] - returned
                    if returned is not None
                    else None,
                    transaction_count=len(transactions),
                )
            )
        for currency, values in sorted(money_totals.items()):
            transactions = values.pop("transactions")
            totals.append(
                OperationalTotal(
                    currency=currency, transaction_count=len(transactions), **values
                )
            )
        if groups and not money and mapping.sales.quantity is None:
            transactions = set().union(
                *(values["transactions"] for values in groups.values())
            )
            totals.append(OperationalTotal(transaction_count=len(transactions)))
        if len(totals) > 50:
            raise SourceMappingError("operational_group_budget_exceeded")
        result = []
        for identity, values in sorted(groups.items(), key=lambda item: repr(item[0])):
            transactions = values.pop("transactions")
            # A source without returns cannot claim a net sold quantity. The
            # explicit gross sales quantities remain usable.
            net_quantity = (
                values["gross_quantity"] - values["returned_quantity"]
                if mapping.refunds
                else values["gross_quantity"]
                if mapping.sales.returns_semantics == "no_returns"
                else None
            )
            if not mapping.refunds:
                values["returned_quantity"] = (
                    Decimal(0)
                    if mapping.sales.returns_semantics == "no_returns"
                    else None
                )
            if mapping.sales.quantity is None:
                values["gross_quantity"] = None
                values["returned_quantity"] = None
                net_quantity = None
            result.append(
                OperationalRow(
                    product_id=identity[0],
                    location=identity[1],
                    branch=identity[4],
                    warehouse=identity[5],
                    unit=identity[2],
                    currency=identity[3],
                    transaction_count=len(transactions),
                    tax_basis=mapping.revenue.tax_basis if mapping.revenue else None,
                    net_quantity=net_quantity,
                    **values,
                )
            )
        if request.capability == "best_sellers":
            rank = request.rank_by
            if any(getattr(row, rank) is None for row in result):
                raise SourceMappingError("ranking_basis_unsupported")
            if len({(r.unit, r.currency) for r in result}) > 1:
                raise SourceMappingError("ranking_incompatible_units_or_currencies")
            result.sort(
                key=lambda r: (-getattr(r, rank), r.product_id, r.location or "")
            )
        return result, tuple(totals)

    def _transaction_counts(
        self, request: OperationalRead
    ) -> tuple[list[OperationalRow], tuple[OperationalTotal, ...]]:
        """Count enforced header keys in SQL; never load an unbounded event set."""
        fact = self.mapping.sales
        statement, params = compile_facts(
            self.mapping,
            self.catalogue.discovery,
            self.catalogue.mapping,
            fact,
            request,
        )
        # transaction_events requires the complete enforced root key, and all
        # joins target enforced unique keys. Each selected row is one document.
        events = statement.order_by(None).limit(None).subquery()
        from app.integrations.operational_quality import bad_text

        invalid = or_(
            *(
                bad_text(cast(events.c[f"key_{i}"], Unicode), 128)
                for i in range(len(fact.key_columns))
            ),
            *(events.c[f"joined_key_{i}"].is_(None) for i in range(len(fact.joins))),
        )
        dimensions = [
            name for name in ("location", "branch", "warehouse") if getattr(fact, name)
        ]
        columns = [events.c[name] for name in dimensions]
        query = select(
            *columns,
            func.count().label("transaction_count"),
            func.sum(case((invalid, 1), else_=0)).label("invalid_records"),
        )
        query = query.select_from(events)
        if columns:
            query = query.group_by(*columns).order_by(*columns)
        connector = self.catalogue.connector
        current = getattr(self, "_execution_connection", None)
        with (
            nullcontext(current)
            if current is not None
            else connector.connection() as connection
        ):
            checks = (
                {"dependencies": True} if getattr(self, "independent", False) else {}
            )
            connector.assert_schema(self.catalogue.discovery, connection, **checks)
            groups = (
                connection.execute(query.limit(51), params).mappings().fetchmany(51)
            )
            connector.assert_schema(self.catalogue.discovery, connection, **checks)
        if len(groups) > 50:
            raise SourceMappingError("operational_group_budget_exceeded")
        if any(row["invalid_records"] for row in groups):
            raise SourceMappingError("operational_transaction_invalid")
        rows = [
            OperationalRow(
                **{name: text_value(row[name]) for name in dimensions},
                transaction_count=row["transaction_count"],
            )
            for row in groups
            if row["transaction_count"]
        ]
        return rows, (
            (
                OperationalTotal(
                    transaction_count=sum(r.transaction_count for r in rows)
                ),
            )
            if rows
            else ()
        )

"""Exact SQL-side validation over the approved scope; never source-data repair."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Integer,
    Table,
    Unicode,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import visitors
from sqlalchemy.sql.functions import FunctionElement

from app.integrations.discovery import SourceMappingError
from app.schemas.catalogue_quality import InvalidCatalogueRecord
from app.schemas.source_mapping import ProductMapping, SchemaDiscovery

if TYPE_CHECKING:
    from app.integrations.mapped_products import MappedProductSource

RULE_EXPLANATIONS = {
    "duplicate_identity": (
        "The source contains repeated approved identity values; "
        "safe exclusion cannot preserve identity."
    ),
    "invalid_identifier": (
        "An approved identity component is null, blank or longer than 128 characters."
    ),
    "missing_name": "All approved product names are null or blank.",
    "invalid_name": "An approved name exceeds 255 characters.",
    "invalid_sku": "The approved SKU exceeds 128 characters.",
    "invalid_category": "An approved category label exceeds 255 characters.",
}


class CharacterLength(FunctionElement):
    type = Integer()
    inherit_cache = True


class TrimWhitespace(FunctionElement):
    type = Unicode()
    inherit_cache = True


@compiles(TrimWhitespace)
def _trim_default(element, compiler, **kw):
    values = [compiler.process(c, **kw) for c in element.clauses]
    return f"trim({values[0]}, {values[1]})"


@compiles(TrimWhitespace, "postgresql")
def _trim_postgresql(element, compiler, **kw):
    values = [compiler.process(c, **kw) for c in element.clauses]
    return f"btrim({values[0]}, {values[1]})"


@compiles(TrimWhitespace, "mssql")
def _trim_mssql(element, compiler, **kw):
    values = [compiler.process(c, **kw) for c in element.clauses]
    return f"TRIM(CAST({values[1]} AS nvarchar(64)) FROM {values[0]})"


@compiles(CharacterLength)
def _length_default(element, compiler, **kw):
    return "length(" + compiler.process(list(element.clauses)[0], **kw) + ")"


@compiles(CharacterLength, "mssql")
def _length_mssql(element, compiler, **kw):
    value = compiler.process(list(element.clauses)[0], **kw)
    return f"(LEN(CAST({value} AS nvarchar(max)) + N'#') - 1)"


def mapping_digest(mapping: ProductMapping) -> str:
    return hashlib.sha256(
        json.dumps(mapping.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def within_tolerance(invalid: int, total: int, maximum: Decimal) -> bool:
    """Compare integer counts without rounding a repeating percentage."""
    return invalid * 100 <= Decimal(total) * maximum


def value_rules(columns, mapping):
    """One shared predicate for validation, search, counts and pagination."""

    def blank(column):
        whitespace = (
            " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003"
            "\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
        )
        normalized = TrimWhitespace(cast(column, Unicode), literal(whitespace))
        return or_(column.is_(None), CharacterLength(normalized) == 0)

    keys = [columns[f"key_{i}"] for i in range(len(mapping.key_columns))]
    names = [columns[f"name_{i}"] for i in range(len(mapping.name_columns))]
    rules = {
        "invalid_identifier": or_(
            *(or_(blank(c), CharacterLength(cast(c, Unicode)) > 128) for c in keys)
        ),
        "missing_name": and_(*(blank(c) for c in names)),
        "invalid_name": or_(
            *(func.coalesce(CharacterLength(c), 0) > 255 for c in names)
        ),
    }
    if mapping.sku_column:
        rules["invalid_sku"] = func.coalesce(CharacterLength(columns["sku"]), 0) > 128
    if mapping.categories:
        rules["invalid_category"] = or_(
            *(
                func.coalesce(CharacterLength(columns[f"category_{i}"]), 0) > 255
                for i in range(len(mapping.categories))
            )
        )
    return rules


def valid_values(columns, mapping):
    return not_(or_(*value_rules(columns, mapping).values()))


@dataclass(frozen=True)
class QualityEvaluation:
    total: int
    invalid: int
    reasons: dict[str, int]
    impact: str
    findings: tuple[str, ...]
    data_digest: str
    diagnostics: tuple[InvalidCatalogueRecord, ...]
    relationship_digest: str | None = None
    relationship_counts: dict[str, int] | None = None

    @property
    def percent(self):
        return (
            Decimal(self.invalid) * 100 / Decimal(self.total)
            if self.total
            else Decimal(0)
        )


def source_data_digest(
    connection: Connection,
    mapping: ProductMapping,
    discovery: SchemaDiscovery,
    deadline: float,
    *,
    column_scope: dict | None = None,
    row_scopes: dict | None = None,
) -> str:
    """Hash complete approved values in the database; return only fixed-size hashes.

    Sorting per-row hashes retains duplicates and avoids unstable row locators.
    SQL Server uses ordered XML aggregation, including compatibility level 100.
    """
    quote = connection.dialect.identifier_preparer.quote_identifier
    digests = []
    for obj in discovery.objects:
        if column_scope is not None and obj.object_id not in column_scope:
            continue
        check_deadline(deadline)
        table = f"{quote(obj.schema_name)}.{quote(obj.name)}"
        values = [
            f"t.{quote(c.name)}"
            for c in obj.columns
            if column_scope is None or c.name in column_scope[obj.object_id]
        ]
        params = {}
        scope = ""
        if (
            mapping is not None
            and obj.object_id == mapping.object_id
            and mapping.row_scopes
        ):
            scope = " WHERE " + " AND ".join(
                f"t.{quote(s.column)} = :scope_{i}"
                for i, s in enumerate(mapping.row_scopes)
            )
            params = {f"scope_{i}": s.value for i, s in enumerate(mapping.row_scopes)}
        if row_scopes is not None and obj.object_id in row_scopes:
            groups = row_scopes[obj.object_id]
            if any(not group for group in groups):
                scope, params = "", {}
            else:
                clauses, params = [], {}
                for group_index, group in enumerate(groups):
                    parts = []
                    for index, (column, value) in enumerate(group):
                        name = f"scope_{group_index}_{index}"
                        parts.append(f"t.{quote(column)} = :{name}")
                        params[name] = value
                    clauses.append("(" + " AND ".join(parts) + ")")
                scope = " WHERE " + " OR ".join(clauses)
        if discovery.engine == "postgresql":
            encoded = ",".join(f"CAST({c} AS text)" for c in values)
            row_hash = (
                f"encode(sha256(convert_to(jsonb_build_array({encoded})::text, "
                "'UTF8')), 'hex')"
            )
            sql = (
                "SELECT encode(sha256(convert_to(COALESCE(string_agg(h, "
                "'' ORDER BY h), ''), 'UTF8')), 'hex') "
                f"FROM (SELECT {row_hash} AS h FROM {table} t{scope}) q"
            )
        else:
            encoded = ",".join(
                f"CAST({c} AS nvarchar(max)) AS c{i}" for i, c in enumerate(values)
            )
            row_hash = (
                "CONVERT(varchar(64), HASHBYTES('SHA2_256', "
                f"(SELECT {encoded} FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, "
                "INCLUDE_NULL_VALUES)), 2)"
            )
            sql = (
                "SELECT CONVERT(varchar(64), HASHBYTES('SHA2_256', COALESCE("
                "(SELECT h AS [text()] FROM "
                f"(SELECT {row_hash} AS h FROM {table} t{scope}) q "
                "ORDER BY h FOR XML PATH(''), TYPE).value('.', "
                "'varchar(max)'), '')), 2)"
            )
        digests.append((obj.object_id, connection.scalar(text(sql), params)))
    return hashlib.sha256(json.dumps(digests).encode()).hexdigest()


def check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise SourceMappingError("catalogue_validation_incomplete")


def catalogue_dependencies(
    mapping: ProductMapping, discovery: SchemaDiscovery
) -> SchemaDiscovery:
    """Only approved execution objects participate in catalogue freshness."""
    ids = {
        mapping.object_id,
        *(r.object_id for r in mapping.categories),
        *(r.object_id for r in mapping.identifiers),
    }
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


def catalogue_columns(mapping: ProductMapping) -> dict[str, set[str]]:
    columns = {mapping.object_id: set((*mapping.key_columns, *mapping.name_columns))}
    columns[mapping.object_id].update(s.column for s in mapping.row_scopes)
    if mapping.sku_column:
        columns[mapping.object_id].add(mapping.sku_column)
    for related in (*mapping.categories, *mapping.identifiers):
        columns[mapping.object_id].update(p.product_column for p in related.joins)
        columns.setdefault(related.object_id, set()).update(
            p.related_column for p in related.joins
        )
        columns[related.object_id].add(
            getattr(related, "label_column", None) or related.value_column
        )
    return columns


def relationship_digest(adapter: MappedProductSource, connection: Connection) -> str:
    """Bind the executable relationship contract, not unrelated incoming FKs."""
    payload = {
        "policy_version": 2,
        "mapping": adapter.mapping.model_dump(mode="json"),
        "schema": catalogue_dependencies(adapter.mapping, adapter.discovery).model_dump(
            mode="json"
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _relationship_impact(
    adapter, connection, tables, invalid_products, deadline, params
):
    # Catalogue queries start at the product source and use the same validity
    # predicate before paging. Barcode EXISTS cannot reintroduce excluded rows.
    # Category targets have complete unique keys, checked by validate_mapping;
    # evaluate_quality additionally compares the full joined/base cardinality.
    check_deadline(deadline)
    return "SAFE_WITH_EXCLUSIONS", (
        "Approved product identity, row scope and required join cardinality verified.",
        (
            "Search, barcode lookup, categories, counts and pagination "
            "share the valid-product predicate."
        ),
        (
            "Unrelated incoming relationships are outside this "
            "catalogue execution contract; no source rows or "
            "transactions are changed or excluded by it."
        ),
        (
            "Inventory, pricing, sales and financial reporting require "
            "independent approval and retain their own source facts."
        ),
    )


def evaluate_quality(
    adapter: MappedProductSource,
    connection: Connection,
    *,
    diagnostics: bool = True,
    impact: bool = True,
    fingerprint: bool = True,
) -> QualityEvaluation:
    from app.integrations.mapped_products import compile_catalogue

    deadline = time.monotonic() + 30
    adapter.assert_catalogue_schema(connection)
    statement, params = compile_catalogue(adapter.mapping, adapter.discovery, None)
    projection = statement.limit(None).order_by(None).subquery("quality_products")
    rules = value_rules(projection.c, adapter.mapping)
    keys = [projection.c[f"key_{i}"] for i in range(len(adapter.mapping.key_columns))]
    duplicates = (
        select(*keys)
        .select_from(projection)
        .group_by(*keys)
        .having(func.count() > 1)
        .subquery("duplicate_keys")
    )
    has_duplicates = (
        connection.execute(
            select(func.count()).select_from(duplicates), params
        ).scalar_one()
        > 0
    )
    rules["duplicate_identity"] = (
        exists(
            select(1)
            .select_from(duplicates)
            .where(
                and_(
                    *(
                        or_(
                            key == duplicates.c[key.name],
                            and_(key.is_(None), duplicates.c[key.name].is_(None)),
                        )
                        for key in keys
                    )
                )
            )
        )
        if has_duplicates
        else literal(0) == 1
    )
    relationship_rules = {
        f"optional_category_{i}_unmatched": and_(
            or_(
                *(
                    projection.c[f"category_input_{i}_{j}"].is_not(None)
                    for j in range(len(category.joins))
                )
            ),
            projection.c[f"category_key_{i}"].is_(None),
        )
        for i, category in enumerate(adapter.mapping.categories)
    }
    invalid = or_(*rules.values())
    flagged = select(
        *projection.c,
        *(
            case((predicate, 1), else_=0).label(f"rule_{name}")
            for name, predicate in rules.items()
        ),
        *(
            case((predicate, 1), else_=0).label(name)
            for name, predicate in relationship_rules.items()
        ),
        case((invalid, 1), else_=0).label("is_invalid"),
    ).subquery("quality_flags")
    counters = [
        func.count().label("total"),
        func.coalesce(func.sum(flagged.c.is_invalid), 0).label("invalid"),
    ]
    counters.extend(
        func.coalesce(func.sum(flagged.c[f"rule_{name}"]), 0).label(name)
        for name in rules
    )
    counters.extend(
        func.coalesce(func.sum(flagged.c[name]), 0).label(name)
        for name in relationship_rules
    )
    counts = (
        connection.execute(select(*counters).select_from(flagged), params)
        .mappings()
        .one()
    )
    # Reuse the exact metadata-quoted product and relationship tables.
    product = next(
        t
        for t in visitors.iterate(statement)
        if isinstance(t, Table)
        and t.name
        == next(
            o.name
            for o in adapter.discovery.objects
            if o.object_id == adapter.mapping.object_id
        )
        and t.schema
        == next(
            o.schema_name
            for o in adapter.discovery.objects
            if o.object_id == adapter.mapping.object_id
        )
    )
    metadata = product.metadata
    mapped_tables = {
        o.object_id: metadata.tables[f"{o.schema_name}.{o.name}"]
        for o in adapter.discovery.objects
    }
    # Reference an approved column explicitly: SQL Server column-only readers
    # need not have table-wide SELECT, even for COUNT(*). Count null keys too.
    base = select(
        func.count(
            case((product.c[adapter.mapping.key_columns[0]].is_(None), 1), else_=1)
        )
    ).select_from(product)
    for scope in adapter.mapping.row_scopes:
        base = base.where(product.c[scope.column] == scope.value)
    if connection.execute(base).scalar_one() != counts["total"]:
        raise SourceMappingError("mapping_category_row_multiplication")
    findings = []
    impact_status = "INSUFFICIENT_CAPABILITY_EVIDENCE"
    if counts["duplicate_identity"]:
        impact_status = "BLOCKED_BY_DATA_QUALITY"
        findings.append(
            "Duplicate product identities cannot be safely excluded "
            "while preserving authoritative identity."
        )
    check_deadline(deadline)
    invalid_rows = select(*projection.c).where(invalid).subquery("invalid_products")
    if counts["invalid"] and impact and not findings:
        # Bind scope values through the same projection for all impact subqueries.
        impact_status, notes = _relationship_impact(
            adapter, connection, mapped_tables, invalid_rows, deadline, params
        )
        findings.extend(notes)
    elif not counts["invalid"]:
        impact_status = "SAFE_FOR_CAPABILITY"
        findings.append(
            "No invalid catalogue records; partial approval is unnecessary."
        )
    if any(counts[name] for name in relationship_rules):
        findings.append(
            "Some optional category joins have no label. They remain "
            "unknown and are not required product-field failures."
        )
    records = []
    if diagnostics:
        flags = [
            case((predicate, 1), else_=0).label(f"rule_{name}")
            for name, predicate in rules.items()
        ]
        rows = (
            connection.execute(
                select(*projection.c, *flags).where(invalid).order_by(*keys).limit(50),
                params,
            )
            .mappings()
            .fetchmany(50)
        )
        for row in rows:
            failed = tuple(name for name in rules if row[f"rule_{name}"])
            parts = [row[f"key_{i}"] for i in range(len(keys))]
            identifier = (
                " / ".join(str(v) for v in parts)[:128]
                if all(v is not None and str(v).strip() for v in parts)
                else None
            )
            name = next(
                (
                    str(row[f"name_{i}"]).strip()[:255]
                    for i in range(len(adapter.mapping.name_columns))
                    if row[f"name_{i}"] is not None and str(row[f"name_{i}"]).strip()
                ),
                None,
            )
            safe = impact_status == "SAFE_WITH_EXCLUSIONS"
            records.append(
                InvalidCatalogueRecord(
                    identifier=identifier,
                    name=name,
                    rules=failed,
                    explanation=" ".join(RULE_EXPLANATIONS[r] for r in failed),
                    relationship_concerns=tuple(findings),
                    safe_exclusion=safe,
                    severity="warning" if safe else "blocked",
                    suggested_action=(
                        "Correct the approved fields in the source through its "
                        "normal owner controls, or review proven-safe exclusions."
                    ),
                )
            )
    digest = (
        source_data_digest(
            connection,
            adapter.mapping,
            catalogue_dependencies(adapter.mapping, adapter.discovery),
            deadline,
            column_scope=catalogue_columns(adapter.mapping),
        )
        if fingerprint
        else ""
    )
    adapter.assert_catalogue_schema(connection)
    check_deadline(deadline)
    return QualityEvaluation(
        relationship_counts={name: int(counts[name]) for name in relationship_rules},
        relationship_digest=relationship_digest(adapter, connection)
        if impact
        else None,
        total=int(counts["total"]),
        invalid=int(counts["invalid"]),
        reasons={k: int(counts[k]) for k in rules},
        impact=impact_status,
        findings=tuple(findings),
        data_digest=digest,
        diagnostics=tuple(records),
    )

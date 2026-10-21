"""Compile validated catalogue structures, never model-generated SQL."""

from __future__ import annotations

import base64
import hashlib
import json

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Unicode,
    and_,
    bindparam,
    cast,
    exists,
    func,
    or_,
    select,
)

from app.integrations.discovery import EngineConnector, SourceMappingError
from app.schemas.source_mapping import (
    CatalogueProduct,
    CatalogueRequest,
    CatalogueResult,
    ProductMapping,
    SchemaDiscovery,
)
from app.utils.product_reference import search_terms


def validate_mapping(mapping: ProductMapping, discovery: SchemaDiscovery) -> None:
    objects = {item.object_id: item for item in discovery.objects}
    product = objects.get(mapping.object_id)
    if product is None:
        raise SourceMappingError("mapping_object_unknown")
    columns = {item.name: item for item in product.columns}
    if len(set(mapping.key_columns)) != len(mapping.key_columns) or not any(
        set(mapping.key_columns) == set(key) for key in product.unique_keys
    ):
        raise SourceMappingError("mapping_product_key_not_unique")
    required = (*mapping.key_columns, *mapping.name_columns)
    if mapping.sku_column is not None:
        required = (*required, mapping.sku_column)
    if not set(required) <= columns.keys():
        raise SourceMappingError("mapping_column_unknown")
    if any(
        columns[name].kind not in {"text", "integer"} for name in mapping.key_columns
    ):
        raise SourceMappingError("mapping_product_key_type")
    if any(columns[name].kind != "text" for name in mapping.name_columns):
        raise SourceMappingError("mapping_name_type")
    if mapping.sku_column and columns[mapping.sku_column].kind != "text":
        raise SourceMappingError("mapping_sku_type")
    if len(set(mapping.name_columns)) != len(mapping.name_columns):
        raise SourceMappingError("mapping_duplicate_name")
    for scope in mapping.row_scopes:
        if scope.column not in columns or columns[scope.column].kind not in {
            "text",
            "integer",
        }:
            raise SourceMappingError("mapping_partition_type")
        kind = columns[scope.column].kind
        if type(scope.value) is not (int if kind == "integer" else str):
            raise SourceMappingError("mapping_partition_type")
    for relation in (*mapping.categories, *mapping.identifiers):
        related = objects.get(relation.object_id)
        if related is None or related.object_id == product.object_id:
            raise SourceMappingError("mapping_relationship_unknown")
        targets = {item.name: item for item in related.columns}
        pairs = [(pair.product_column, pair.related_column) for pair in relation.joins]
        if len(set(pairs)) != len(pairs) or len({p[1] for p in pairs}) != len(pairs):
            raise SourceMappingError("mapping_duplicate_join")
        for left, right in pairs:
            if left not in columns or right not in targets:
                raise SourceMappingError("mapping_join_column_unknown")
            if columns[left].kind != targets[right].kind or columns[left].kind not in {
                "text",
                "integer",
            }:
                raise SourceMappingError("mapping_join_type")
        if hasattr(relation, "label_column"):
            if not any(
                set(p[1] for p in pairs) == set(key) for key in related.unique_keys
            ):
                raise SourceMappingError("mapping_category_join_not_unique")
            label = relation.label_column
        else:
            # Identifier rows may be one-to-many; EXISTS avoids multiplying products.
            if not any(
                set(p[0] for p in pairs) == set(key) for key in product.unique_keys
            ):
                raise SourceMappingError("mapping_identifier_join_not_product_key")
            label = relation.value_column
        if label not in targets or targets[label].kind != "text":
            raise SourceMappingError("mapping_label_type")


def encode_product_key(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return values[0]
    return "key:" + base64.urlsafe_b64encode(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def decode_product_key(value: str, size: int) -> tuple[str, ...]:
    if size == 1:
        return (value,)
    try:
        if not value.startswith("key:"):
            raise ValueError
        encoded = value[4:]
        parts = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if (
            not isinstance(parts, list)
            or len(parts) != size
            or any(not isinstance(part, str) or len(part) > 128 for part in parts)
        ):
            raise ValueError
        return tuple(parts)
    except ValueError, TypeError, UnicodeError:
        raise SourceMappingError("invalid_product_identifier") from None


def _continuation_scope(request, mapping, discovery, coverage_scope=None):
    payload = [
        request.model_dump(exclude={"continuation", "limit"}, mode="json"),
        mapping.model_dump(mode="json"),
        discovery.model_dump(mode="json"),
    ]
    if coverage_scope is not None:
        payload.append(coverage_scope)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def encode_continuation(request, mapping, discovery, keys, coverage_scope=None):
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "scope": _continuation_scope(
                    request, mapping, discovery, coverage_scope
                ),
                "after": keys,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).decode()


def decode_continuation(request, mapping, discovery, coverage_scope=None):
    try:
        value = json.loads(base64.urlsafe_b64decode(request.continuation))
        keys = value["after"]
        if (
            value["scope"]
            != _continuation_scope(request, mapping, discovery, coverage_scope)
            or not isinstance(keys, list)
            or len(keys) != len(mapping.key_columns)
            or any(
                not isinstance(k, str) or not k.strip() or len(k) > 128 for k in keys
            )
        ):
            raise ValueError
        return keys
    except ValueError, KeyError, TypeError, UnicodeError:
        raise SourceMappingError("invalid_catalogue_continuation") from None


def compile_catalogue(
    mapping: ProductMapping,
    discovery: SchemaDiscovery,
    request: CatalogueRequest | None,
    *,
    exclude_invalid: bool = False,
    coverage_scope: str | None = None,
):
    validate_mapping(mapping, discovery)
    metadata = MetaData()
    tables = {
        item.object_id: Table(
            item.name,
            metadata,
            *(
                Column(
                    column.name,
                    Integer if column.kind == "integer" else Unicode,
                    quote=True,
                )
                for column in item.columns
            ),
            schema=item.schema_name,
            quote=True,
            quote_schema=True,
        )
        for item in discovery.objects
    }
    product = tables[mapping.object_id]
    selected = [
        product.c[name].label(f"key_{i}") for i, name in enumerate(mapping.key_columns)
    ]
    selected += [
        product.c[name].label(f"name_{i}")
        for i, name in enumerate(mapping.name_columns)
    ]
    if mapping.sku_column:
        selected.append(product.c[mapping.sku_column].label("sku"))
    joined = product
    category_columns = []
    for i, category in enumerate(mapping.categories):
        related = tables[category.object_id].alias(f"category_{i}")
        joined = joined.outerjoin(
            related,
            and_(
                *(
                    product.c[p.product_column] == related.c[p.related_column]
                    for p in category.joins
                )
            ),
        )
        selected.append(related.c[category.label_column].label(f"category_{i}"))
        selected.append(
            related.c[category.joins[0].related_column].label(f"category_key_{i}")
        )
        selected.extend(
            product.c[p.product_column].label(f"category_input_{i}_{j}")
            for j, p in enumerate(category.joins)
        )
        category_columns.append(related.c[category.label_column])
    query = select(*selected).select_from(joined)
    if exclude_invalid:
        from app.integrations.catalogue_quality import valid_values

        query = query.where(
            valid_values({c.name: c.element for c in selected}, mapping)
        )
    parameters = {}

    for i, scope in enumerate(mapping.row_scopes):
        parameters[f"product_scope_{i}"] = scope.value
        query = query.where(product.c[scope.column] == bindparam(f"product_scope_{i}"))
    if request is None:
        pass  # Internal validation projection; not an API operation.
    elif request.external_product_id is not None:
        parts = decode_product_key(
            request.external_product_id, len(mapping.key_columns)
        )
        predicates = []
        source = next(
            item for item in discovery.objects if item.object_id == mapping.object_id
        )
        kinds = {item.name: item.kind for item in source.columns}
        for i, (name, value) in enumerate(zip(mapping.key_columns, parts, strict=True)):
            if kinds[name] == "integer":
                try:
                    parameters[f"id_{i}"] = int(value)
                except ValueError:
                    raise SourceMappingError("invalid_product_identifier") from None
            else:
                parameters[f"id_{i}"] = value
            predicates.append(product.c[name] == bindparam(f"id_{i}"))
        query = query.where(and_(*predicates))
    elif not request.browse:
        value = (request.query or "").strip()
        if not value:
            raise SourceMappingError("empty_product_search")
        parameters["exact"] = value
        terms = search_terms(value)
        if not terms or len(terms) > 16:
            raise SourceMappingError("empty_product_search")
        names = []
        for i, term in enumerate(terms):
            escaped = (
                term.replace("~", "~~")
                .replace("%", "~%")
                .replace("_", "~_")
                .replace("[", "~[")
            )
            parameters[f"term_{i}"] = f"%{escaped}%"
            names.append(
                or_(
                    *(
                        func.replace(product.c[name], " ", "").ilike(
                            bindparam(f"term_{i}"), escape="~"
                        )
                        for name in mapping.name_columns
                    )
                )
            )
        predicates = [and_(*names)]
        predicates += [
            cast(product.c[name], String) == bindparam("exact")
            for name in mapping.key_columns
        ]
        if mapping.sku_column:
            predicates.append(product.c[mapping.sku_column] == bindparam("exact"))
        for identifier in mapping.identifiers:
            related = tables[identifier.object_id]
            predicates.append(
                exists(
                    select(1)
                    .select_from(related)
                    .where(
                        and_(
                            *(
                                product.c[p.product_column]
                                == related.c[p.related_column]
                                for p in identifier.joins
                            ),
                            related.c[identifier.value_column] == bindparam("exact"),
                        )
                    )
                )
            )
        query = query.where(or_(*predicates))
    if request is not None and request.category:
        if not category_columns:
            raise SourceMappingError("category_discovery_unsupported")
        parameters["category"] = request.category.casefold()
        query = query.where(
            or_(*(func.lower(c) == bindparam("category") for c in category_columns))
        )
    if request is not None and request.continuation:
        after = decode_continuation(request, mapping, discovery, coverage_scope)
        kinds = {
            c.name: c.kind
            for o in discovery.objects
            if o.object_id == mapping.object_id
            for c in o.columns
        }
        for i, (name, value) in enumerate(zip(mapping.key_columns, after, strict=True)):
            try:
                parameters[f"after_{i}"] = (
                    int(value) if kinds[name] == "integer" else value
                )
            except ValueError, TypeError:
                raise SourceMappingError("invalid_catalogue_continuation") from None
        query = query.where(
            or_(
                *(
                    and_(
                        *(
                            product.c[mapping.key_columns[j]] == bindparam(f"after_{j}")
                            for j in range(i)
                        ),
                        product.c[name] > bindparam(f"after_{i}"),
                    )
                    for i, name in enumerate(mapping.key_columns)
                )
            )
        )
    query = query.order_by(*(product.c[name] for name in mapping.key_columns)).limit(
        request.limit + 1 if request is not None else 6
    )
    return query, parameters


class MappedProductSource:
    def __init__(
        self,
        connector: EngineConnector,
        discovery: SchemaDiscovery,
        mapping: ProductMapping,
        revision_version: int = 1,
        *,
        quality_policy=None,
        coverage=None,
    ):
        validate_mapping(mapping, discovery)
        self.connector = connector
        self.discovery = discovery
        self.mapping = mapping
        self.revision_version = revision_version
        self.quality_policy = quality_policy
        self.coverage = coverage

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.connector.config.query_timeout_seconds

    def assert_catalogue_schema(self, connection):
        from app.integrations.catalogue_quality import catalogue_dependencies

        if isinstance(self.connector, EngineConnector):
            self.connector.assert_schema(
                catalogue_dependencies(self.mapping, self.discovery),
                connection,
                dependencies=True,
            )
        else:
            self.connector.assert_schema(self.discovery, connection)

    def search(self, request: CatalogueRequest) -> CatalogueResult:
        if (
            request.mapping_version is not None
            and request.mapping_version != self.revision_version
        ):
            raise SourceMappingError("mapping_selection_stale")
        request = request.model_copy(update={"mapping_version": self.revision_version})
        coverage_scope = (
            (
                str(self.coverage.approval_id)
                if self.coverage and self.coverage.approval_id
                else self.quality_policy.data_digest
            )
            if self.quality_policy
            else None
        )
        matched_records = None
        statement, parameters = compile_catalogue(
            self.mapping,
            self.discovery,
            request,
            exclude_invalid=self.quality_policy is not None,
            coverage_scope=coverage_scope,
        )
        with self.connector.connection(
            **({"consistent": True} if self.quality_policy is not None else {})
        ) as connection:
            self.assert_catalogue_schema(connection)
            if self.quality_policy is not None:
                self._assert_partial_snapshot(connection)
                count_query, count_params = compile_catalogue(
                    self.mapping,
                    self.discovery,
                    request.model_copy(update={"continuation": None}),
                    exclude_invalid=True,
                )
                matched_records = connection.scalar(
                    select(func.count()).select_from(
                        count_query.limit(None).order_by(None).subquery()
                    ),
                    count_params,
                )
            rows = (
                connection.execute(statement, parameters)
                .mappings()
                .fetchmany(request.limit + 1)
            )
            self.assert_catalogue_schema(connection)
        items = self._products(rows[: request.limit])
        truncated = len(rows) > request.limit
        return CatalogueResult(
            matched_records=matched_records,
            coverage=self.coverage,
            mapping_version=self.revision_version,
            pagination_scope=_continuation_scope(
                request, self.mapping, self.discovery, coverage_scope
            ),
            key_size=len(self.mapping.key_columns),
            status="not_found"
            if not items
            else "resolved"
            if len(items) == 1 and not truncated
            else "ambiguous",
            items=items,
            truncated=truncated,
            discovery_request=request.model_copy(update={"continuation": None}),
            search_semantics="approved_category"
            if request.category
            else "browse"
            if request.browse
            else "required_literal_terms",
            continuation=encode_continuation(
                request,
                self.mapping,
                self.discovery,
                tuple(
                    str(rows[request.limit - 1][f"key_{i}"])
                    for i in range(len(self.mapping.key_columns))
                ),
                coverage_scope,
            )
            if truncated
            else None,
        )

    def _products(self, rows) -> tuple[CatalogueProduct, ...]:
        items = []
        for row in rows:
            names = tuple(
                str(row[f"name_{i}"]).strip()
                for i in range(len(self.mapping.name_columns))
                if row[f"name_{i}"] is not None and str(row[f"name_{i}"]).strip()
            )
            if not names or any(
                row[f"key_{i}"] is None or not str(row[f"key_{i}"]).strip()
                for i in range(len(self.mapping.key_columns))
            ):
                raise SourceMappingError("catalogue_value_invalid")
            if any(len(name) > 255 for name in names):
                raise SourceMappingError("catalogue_value_out_of_bounds")
            keys = tuple(
                str(row[f"key_{i}"]) for i in range(len(self.mapping.key_columns))
            )
            if any(len(key) > 128 for key in keys):
                raise SourceMappingError("catalogue_value_out_of_bounds")
            categories = tuple(
                str(row[f"category_{i}"]).strip()
                for i in range(len(self.mapping.categories))
                if row[f"category_{i}"] is not None
                and str(row[f"category_{i}"]).strip()
            )
            if any(len(label) > 255 for label in categories):
                raise SourceMappingError("catalogue_value_out_of_bounds")
            if row.get("sku") is not None and len(str(row["sku"])) > 128:
                raise SourceMappingError("catalogue_value_out_of_bounds")
            items.append(
                CatalogueProduct(
                    external_product_id=encode_product_key(keys),
                    names=names,
                    sku=str(row["sku"]) if row.get("sku") is not None else None,
                    categories=categories,
                )
            )
        return tuple(items)

    def validate_results(self) -> tuple[str, ...]:
        from app.integrations.catalogue_quality import evaluate_quality

        with self.connector.connection(
            **(
                {"consistent": True}
                if isinstance(self.connector, EngineConnector)
                else {}
            )
        ) as connection:
            if self.quality_policy is not None:
                self._assert_partial_snapshot(connection)
                return (
                    (
                        "Approved partial catalogue snapshot and exclusion policy"
                        " verified."
                    ),
                )
            result = evaluate_quality(
                self, connection, diagnostics=False, impact=False, fingerprint=False
            )
            if result.invalid:
                raise SourceMappingError("catalogue_value_invalid")
        return (
            "Product keys and category join cardinality verified from metadata.",
            "Complete approved catalogue scope counted with restricted credentials.",
            "Join business meaning and missing labels require reviewer confirmation.",
        )

    def _assert_partial_snapshot(self, connection):
        import time
        from datetime import UTC, datetime

        from app.integrations.catalogue_quality import (
            catalogue_columns,
            catalogue_dependencies,
            mapping_digest,
            relationship_digest,
            source_data_digest,
        )

        if (
            self.coverage is None
            or self.coverage.expires_at is None
            or self.coverage.expires_at <= datetime.now(UTC)
        ):
            raise SourceMappingError("catalogue_partial_approval_expired")
        if self.quality_policy.mapping_digest != mapping_digest(self.mapping):
            raise SourceMappingError("catalogue_partial_mapping_changed")
        from app.integrations.catalogue_quality import RULE_EXPLANATIONS

        if set(self.quality_policy.rules) != set(RULE_EXPLANATIONS):
            raise SourceMappingError("catalogue_partial_policy_invalid")
        if (
            self.quality_policy.relationship_digest is None
            or relationship_digest(self, connection)
            != self.quality_policy.relationship_digest
        ):
            raise SourceMappingError("catalogue_partial_relationships_changed")
        current = source_data_digest(
            connection,
            self.mapping,
            catalogue_dependencies(self.mapping, self.discovery),
            time.monotonic() + 30,
            column_scope=catalogue_columns(self.mapping),
        )
        if current != self.quality_policy.data_digest:
            raise SourceMappingError("catalogue_partial_data_changed")

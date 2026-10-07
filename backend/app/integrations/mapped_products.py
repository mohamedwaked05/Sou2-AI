"""Compile validated catalogue structures, never model-generated SQL."""

from __future__ import annotations

import base64
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
    if any(columns[name].nullable for name in mapping.key_columns):
        raise SourceMappingError("mapping_product_key_nullable")
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


def compile_catalogue(
    mapping: ProductMapping,
    discovery: SchemaDiscovery,
    request: CatalogueRequest | None,
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
    query = select(*selected).select_from(joined)
    parameters = {}
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
    else:
        value = (request.query or "").strip()
        if not value:
            raise SourceMappingError("empty_product_search")
        escaped = (
            value.replace("~", "~~")
            .replace("%", "~%")
            .replace("_", "~_")
            .replace("[", "~[")
        )
        parameters.update(term=f"%{escaped}%", exact=value)
        predicates = [
            product.c[name].ilike(bindparam("term"), escape="~")
            for name in mapping.name_columns
        ]
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
    ):
        validate_mapping(mapping, discovery)
        self.connector = connector
        self.discovery = discovery
        self.mapping = mapping
        self.revision_version = revision_version

    def search(self, request: CatalogueRequest) -> CatalogueResult:
        if (
            request.mapping_version is not None
            and request.mapping_version != self.revision_version
        ):
            raise SourceMappingError("mapping_selection_stale")
        statement, parameters = compile_catalogue(self.mapping, self.discovery, request)
        with self.connector.connection() as connection:
            self.connector.assert_schema(self.discovery, connection)
            rows = (
                connection.execute(statement, parameters)
                .mappings()
                .fetchmany(request.limit + 1)
            )
            self.connector.assert_schema(self.discovery, connection)
        items = self._products(rows[: request.limit])
        truncated = len(rows) > request.limit
        return CatalogueResult(
            mapping_version=self.revision_version,
            status="not_found"
            if not items
            else "resolved"
            if len(items) == 1 and not truncated
            else "ambiguous",
            items=items,
            truncated=truncated,
        )

    def _products(self, rows) -> tuple[CatalogueProduct, ...]:
        items = []
        for row in rows:
            names = tuple(
                str(row[f"name_{i}"]).strip()
                for i in range(len(self.mapping.name_columns))
                if row[f"name_{i}"] is not None and str(row[f"name_{i}"]).strip()
            )
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
        # A bounded projection proves that approved fields/joins are executable.
        # Enforced unique keys (not a sample guess) establish catalogue cardinality.
        statement, parameters = compile_catalogue(self.mapping, self.discovery, None)
        with self.connector.connection() as connection:
            self.connector.assert_schema(self.discovery, connection)
            self._products(
                connection.execute(statement, parameters).mappings().fetchmany(6)
            )
            self.connector.assert_schema(self.discovery, connection)
        return (
            "Product keys and category join cardinality verified from metadata.",
            "Bounded SELECT executed with restricted credentials.",
            "Join business meaning and missing labels require reviewer confirmation.",
        )

"""Disposable sources: execute production SQL on SQLite, never deployment URLs.

SQLite validates query/result logic only. PostgreSQL/SQL Server catalogs, role
checks, cancellation, collations and numeric storage still need native tests.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from app.integrations.discovery import SourceMappingError
from app.integrations.mapped_products import MappedProductSource
from app.integrations.postgresql import PostgreSQLOperationalAdapter
from app.schemas.source_mapping import ProductMapping, SchemaDiscovery
from sqlalchemy.engine.default import DefaultDialect


class Rows:
    def __init__(self, values=()):
        self.values = list(values)

    def mappings(self):
        return self

    def fetchmany(self, count):
        return self.values[:count]

    def one(self):
        assert len(self.values) == 1
        return self.values[0]

    def scalar_one(self):
        return next(iter(self.one().values()))

    def __iter__(self):
        return iter(self.values)


class DisposableSource:
    def __init__(self, schema):
        # Literal memory-only connections; no environment/configuration lookup.
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f'ATTACH DATABASE ":memory:" AS "{schema}"')
        assert all(not row[2] for row in self.db.execute("PRAGMA database_list"))
        self.db.create_function("GREATEST", -1, max)
        self.db.create_function("btrim", 1, lambda value: value.strip())
        self.config = SimpleNamespace(query_timeout_seconds=2)
        self.source_fingerprint = hashlib.sha256(schema.encode()).hexdigest()
        self.calls = []
        self.schema_checks = 0

    def close(self):
        self.db.close()

    def readonly(self):
        self.db.commit()
        self.db.execute("PRAGMA query_only = ON")

    @contextmanager
    def connection(self):
        yield self

    def connect(self):
        return self.connection()

    def assert_schema(self, discovery, _connection):
        self.schema_checks += 1
        if discovery.source_fingerprint != self.source_fingerprint:
            raise SourceMappingError("mapping_schema_changed")
        for obj in discovery.objects:
            rows = self.db.execute(
                f'PRAGMA "{obj.schema_name}".table_info("{obj.name}")'
            ).fetchall()
            if {row[1] for row in rows} != {c.name for c in obj.columns}:
                raise SourceMappingError("mapping_schema_changed")

    def execute(self, statement, parameters=None):
        parameters = parameters or {}
        sql = str(statement)
        # These two PostgreSQL session controls have no SQLite equivalent.
        # Read-only protection is enforced separately by PRAGMA query_only.
        if sql == "SET TRANSACTION READ ONLY" or "SELECT set_config(" in sql:
            return Rows()
        compiled = statement.compile(dialect=DefaultDialect(paramstyle="named"))
        # Public API in SQLAlchemy 2.0/2.1: retain named binds after IN expansion.
        # render_postcompile + compiled.params is not supported consistently.
        expanded = compiled.construct_expanded_state(parameters)
        bound = expanded.parameters
        values = {
            key: value.isoformat(" ") if isinstance(value, datetime) else value
            for key, value in bound.items()
        }
        self.calls.append((expanded.statement, tuple(values.values())))
        records = [dict(row) for row in self.db.execute(expanded.statement, values)]
        for row in records:
            for key, value in row.items():
                if key == "data_updated_at":
                    row[key] = datetime.fromisoformat(value)
                elif isinstance(value, float):
                    row[key] = Decimal(str(value))
        return Rows(records)


def mapped_catalogue(seed, layout="text", engine="postgresql"):
    schema, table = f"schema_{seed}", f"records_{seed}"
    source = DisposableSource(schema)
    composite = layout == "composite"
    kind = "integer" if layout == "integer" else "text"
    key_sql = "INTEGER" if kind == "integer" else "TEXT"
    source.db.execute(
        f'CREATE TABLE "{schema}"."{table}" '
        f"(identity {key_sql} NOT NULL, segment TEXT NOT NULL, "
        "label TEXT, local_label TEXT, code TEXT, category_key TEXT, "
        f"PRIMARY KEY (identity{', segment' if composite else ''}))"
    )
    source.db.execute(
        f'CREATE TABLE "{schema}".groups (group_key TEXT PRIMARY KEY, label TEXT)'
    )
    source.db.execute(
        f'CREATE TABLE "{schema}".identifiers '
        "(identity_key TEXT, segment_key TEXT, alternate TEXT)"
    )
    family = f"P{hashlib.sha256(str(seed).encode()).hexdigest()[:12]}"
    identities = [101, 102, 103] if kind == "integer" else ["0001", "0002", "0003"]
    source.db.executemany(
        f'INSERT INTO "{schema}"."{table}" VALUES (?,?,?,?,?,?)',
        [
            (identities[0], "west", family, f"صنف {seed}", f"SKU-{seed}", "g"),
            (identities[1], "east", family, f"صنف {seed}", f"SKU-{seed}", "g"),
            (identities[2], "east", f"Other-{family}", None, None, None),
        ],
    )
    source.db.execute(f'INSERT INTO "{schema}".groups VALUES (?,?)', ("g", "Group"))
    source.db.executemany(
        f'INSERT INTO "{schema}".identifiers VALUES (?,?,?)',
        [(str(identities[0]), "west", "shared-code")] * 2
        + [(str(identities[1]), "east", "shared-code")],
    )
    columns = lambda fields: [  # noqa: E731
        {"name": name, "kind": column_kind, "nullable": nullable}
        for name, column_kind, nullable in fields
    ]
    objects = [
        dict(
            object_id="o0",
            schema_name=schema,
            name=table,
            columns=columns(
                [
                    ("identity", kind, False),
                    ("segment", "text", False),
                    ("label", "text", True),
                    ("local_label", "text", True),
                    ("code", "text", True),
                    ("category_key", "text", True),
                ]
            ),
            unique_keys=[("identity", "segment") if composite else ("identity",)],
        ),
        dict(
            object_id="o1",
            schema_name=schema,
            name="groups",
            columns=columns([("group_key", "text", False), ("label", "text", True)]),
            unique_keys=[("group_key",)],
        ),
    ]
    identifiers = []
    # Textual alternate IDs require matching join types; do not coerce metadata.
    if kind == "text":
        objects.append(
            dict(
                object_id="o2",
                schema_name=schema,
                name="identifiers",
                columns=columns(
                    [
                        ("identity_key", "text", True),
                        ("segment_key", "text", True),
                        ("alternate", "text", True),
                    ]
                ),
                unique_keys=[],
            )
        )
        joins = [dict(product_column="identity", related_column="identity_key")]
        if composite:
            joins.append(dict(product_column="segment", related_column="segment_key"))
        identifiers = [
            dict(
                object_id="o2",
                joins=joins,
                value_column="alternate",
                kind="alternate_id",
            )
        ]
    discovery = SchemaDiscovery(
        engine=engine, source_fingerprint=source.source_fingerprint, objects=objects
    )
    mapping = ProductMapping(
        capability="products",
        object_id="o0",
        key_columns=("identity", "segment") if composite else ("identity",),
        name_columns=("label", "local_label"),
        sku_column="code",
        categories=[
            dict(
                object_id="o1",
                joins=[dict(product_column="category_key", related_column="group_key")],
                label_column="label",
            )
        ],
        identifiers=identifiers,
    )
    source.readonly()
    return SimpleNamespace(
        source=source,
        adapter=MappedProductSource(source, discovery, mapping),
        discovery=discovery,
        mapping=mapping,
        family=family,
        identities=identities,
    )


def legacy_source(seed, now, currency="EUR", timezone="Europe/Paris"):
    source = DisposableSource("minimarket")
    source.db.executescript("""
        CREATE TABLE minimarket.store_configuration
            (singleton BOOLEAN, business_timezone TEXT,
             currency_code TEXT, data_updated_at TEXT);
        CREATE TABLE minimarket.categories
            (category_id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE minimarket.catalog_items
            (item_id INTEGER PRIMARY KEY, item_code TEXT, merchant_sku TEXT,
             ean_barcode TEXT, display_label TEXT, category_id INTEGER, active BOOLEAN);
        CREATE TABLE minimarket.stock_locations
            (location_id INTEGER PRIMARY KEY, location_code TEXT UNIQUE,
             location_label TEXT, location_type TEXT);
        CREATE TABLE minimarket.stock_levels
            (location_id INTEGER, item_id INTEGER, physical_quantity NUMERIC,
             reorder_threshold NUMERIC, desired_quantity NUMERIC);
        CREATE TABLE minimarket.stock_reservations
            (location_id INTEGER, item_id INTEGER, held_quantity NUMERIC,
             reservation_state TEXT, expires_at TEXT);
        CREATE TABLE minimarket.receipts
            (receipt_id INTEGER PRIMARY KEY, receipt_state TEXT, completed_at TEXT,
             branch_location_id INTEGER);
        CREATE TABLE minimarket.receipt_lines
            (receipt_line_id INTEGER PRIMARY KEY, receipt_id INTEGER, item_id INTEGER,
             sold_quantity NUMERIC, line_total NUMERIC);
        CREATE TABLE minimarket.refunds
            (refund_id INTEGER PRIMARY KEY, receipt_id INTEGER,
             refund_state TEXT, refunded_at TEXT);
        CREATE TABLE minimarket.refund_lines
            (refund_id INTEGER, receipt_line_id INTEGER,
             returned_quantity NUMERIC, refund_total NUMERIC);
    """)
    source.db.execute(
        "INSERT INTO minimarket.store_configuration VALUES (1,?,?,?)",
        (timezone, currency, now.isoformat()),
    )
    name = f"Generated-{seed}"
    source.db.execute(
        "INSERT INTO minimarket.catalog_items VALUES (1,?,?,NULL,?,NULL,1)",
        (f"ID-{seed}", f"SKU-{seed}", name),
    )
    source.db.executemany(
        "INSERT INTO minimarket.stock_locations VALUES (?,?,?,?)",
        [(1, "L1", "Counter", "SHOP"), (2, "L2", "Depot", "DEPOT")],
    )
    source.db.executemany(
        "INSERT INTO minimarket.stock_levels VALUES (?,1,?,?,?)",
        [(1, 10.5, 8, 20), (2, 4, 2, 12)],
    )
    source.db.executemany(
        "INSERT INTO minimarket.stock_reservations VALUES (1,1,?,?,?)",
        [
            (2.5, "ACTIVE", None),
            (9, "CANCELLED", None),
            (8, "ACTIVE", "2000-01-01"),
            (0.5, "ACTIVE", "2099-01-01"),
        ],
    )
    adapter = PostgreSQLOperationalAdapter(
        engine=source,
        location_type_mapping={"branch": ("SHOP",), "warehouse": ("DEPOT",)},
    )
    return SimpleNamespace(source=source, adapter=adapter, name=name)

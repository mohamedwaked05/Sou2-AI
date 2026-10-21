"""Read-only engine connections and bounded, operator-approved discovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.schemas.source_mapping import (
    DiscoveredColumn,
    DiscoveredObject,
    DiscoveredRelationship,
    SchemaDiscovery,
)


class SourceMappingError(ValueError):
    """Safe errors never include connection strings, SQL or source records."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DiscoveryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_name: str = Field(min_length=1, max_length=128)
    object_name: str = Field(min_length=1, max_length=128)
    columns: tuple[str, ...] = Field(min_length=1, max_length=64)


class SourceConnection(BaseModel):
    """Deployment-managed secrets and exposure policy, never onboarding SQL."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    display_name: str = Field(min_length=2, max_length=120)
    engine: Literal["postgresql", "sqlserver"]
    url: SecretStr
    business_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=100)
    discovery_scope: tuple[DiscoveryScope, ...] = Field(min_length=1, max_length=16)
    query_timeout_seconds: int = Field(default=2, ge=1, le=30)
    connect_timeout_seconds: int = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def validate_connection(self) -> SourceConnection:
        try:
            parsed = make_url(self.url.get_secret_value())
        except SQLAlchemyError:
            raise ValueError("Invalid source connection configuration.") from None
        if "odbc_connect" in parsed.query:
            raise ValueError("Opaque ODBC connection overrides are not accepted.")
        expected = (
            "postgresql+psycopg" if self.engine == "postgresql" else "mssql+pyodbc"
        )
        if parsed.drivername != expected:
            raise ValueError("Connection driver does not match the selected engine.")
        if self.engine == "sqlserver" and (
            parsed.query.get("driver") != "ODBC Driver 18 for SQL Server"
            or parsed.query.get("Encrypt", "").lower() != "yes"
        ):
            raise ValueError("SQL Server requires ODBC 18 and encrypted transport.")
        if parsed.host not in {"127.0.0.1", "localhost"}:
            if (
                self.engine == "postgresql"
                and parsed.query.get("sslmode") != "verify-full"
            ):
                raise ValueError("Remote PostgreSQL requires verified TLS.")
            if (
                self.engine == "sqlserver"
                and parsed.query.get("TrustServerCertificate", "no").lower() != "no"
            ):
                raise ValueError("Remote SQL Server requires certificate verification.")
        names = [(item.schema_name, item.object_name) for item in self.discovery_scope]
        if len(set(names)) != len(names) or any(
            len(set(item.columns)) != len(item.columns) for item in self.discovery_scope
        ):
            raise ValueError("Duplicate discovery scope.")
        if sum(len(item.columns) for item in self.discovery_scope) > 128:
            raise ValueError("Discovery scope exceeds the column bound.")
        return self

    def permits(self, business_id: uuid.UUID) -> bool:
        return business_id in self.business_ids


_POSTGRES_COLUMNS = """
SELECT a.attname AS name, t.typname AS type_name, NOT a.attnotnull AS nullable,
       pg_catalog.format_type(a.atttypid, a.atttypmod) || ':'
         || a.attcollation::text AS type_signature,
       pg_catalog.has_column_privilege(c.oid, a.attnum, 'SELECT') AS readable
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
WHERE n.nspname = :schema AND c.relname = :name AND c.relkind IN ('r','v','m')
  AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
"""
_POSTGRES_KEYS = """
SELECT i.indexrelid AS key_id, a.attname AS name, k.ordinality AS position
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_index i ON i.indrelid = c.oid
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = :schema AND c.relname = :name AND i.indisunique
  AND i.indisvalid AND i.indpred IS NULL AND i.indexprs IS NULL
  AND k.ordinality <= i.indnkeyatts ORDER BY i.indexrelid, k.ordinality
"""
_MSSQL_COLUMNS = """
SELECT c.name, t.name AS type_name, c.is_nullable AS nullable,
       t.name + ':' + CAST(c.max_length AS varchar(10)) + ':'
         + CAST(c.precision AS varchar(10)) + ':' + CAST(c.scale AS varchar(10))
         + ':' + COALESCE(c.collation_name, '') AS type_signature,
       HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name), 'OBJECT',
                         'SELECT', c.name, 'COLUMN') AS readable
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.columns c ON c.object_id = o.object_id
JOIN sys.types t ON t.user_type_id = c.user_type_id
WHERE s.name = :schema AND o.name = :name AND o.type IN ('U','V')
ORDER BY c.column_id
"""
_MSSQL_KEYS = """
SELECT i.index_id AS key_id, c.name, ic.key_ordinal AS position
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.indexes i ON i.object_id = o.object_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE s.name = :schema AND o.name = :name AND o.type IN ('U','V') AND i.is_unique = 1
  AND i.is_disabled = 0 AND i.has_filter = 0 AND ic.key_ordinal > 0
ORDER BY i.index_id, ic.key_ordinal
"""

_POSTGRES_RELATIONSHIPS = """
SELECT f.oid AS key_id, sa.attname AS source_column, ta.attname AS target_column,
       tn.nspname AS target_schema, tc.relname AS target_name
FROM pg_catalog.pg_constraint f
JOIN pg_catalog.pg_class sc ON sc.oid=f.conrelid
JOIN pg_catalog.pg_namespace sn ON sn.oid=sc.relnamespace
JOIN pg_catalog.pg_class tc ON tc.oid=f.confrelid
JOIN pg_catalog.pg_namespace tn ON tn.oid=tc.relnamespace
CROSS JOIN LATERAL unnest(f.conkey, f.confkey) WITH ORDINALITY k(s,t,position)
JOIN pg_catalog.pg_attribute sa ON sa.attrelid=sc.oid AND sa.attnum=k.s
JOIN pg_catalog.pg_attribute ta ON ta.attrelid=tc.oid AND ta.attnum=k.t
WHERE f.contype='f' AND sn.nspname=:schema AND sc.relname=:name
  AND f.convalidated ORDER BY f.oid, k.position
"""
_MSSQL_RELATIONSHIPS = """
SELECT f.object_id AS key_id, sc.name AS source_column, tc.name AS target_column,
       ts.name AS target_schema, tt.name AS target_name
FROM sys.foreign_keys f
JOIN sys.tables st ON st.object_id=f.parent_object_id
JOIN sys.schemas ss ON ss.schema_id=st.schema_id
JOIN sys.tables tt ON tt.object_id=f.referenced_object_id
JOIN sys.schemas ts ON ts.schema_id=tt.schema_id
JOIN sys.foreign_key_columns p ON p.constraint_object_id=f.object_id
JOIN sys.columns sc ON sc.object_id=st.object_id AND sc.column_id=p.parent_column_id
JOIN sys.columns tc ON tc.object_id=tt.object_id AND tc.column_id=p.referenced_column_id
WHERE ss.name=:schema AND st.name=:name AND f.is_disabled=0 AND f.is_not_trusted=0
ORDER BY f.object_id, p.constraint_column_id
"""

# Normal reads compare a catalogue stamp against saved provenance. They do not
# discover objects, assemble a new mapping, inspect samples or call a provider.
_POSTGRES_STAMP = """
SELECT c.oid::text || CASE WHEN c.relkind IN ('v','m')
  THEN ':' || md5(pg_catalog.pg_get_viewdef(c.oid)) ELSE '' END AS object_stamp,
  (SELECT string_agg(a.attname || ':' || a.atttypid::text || ':' || a.atttypmod::text
    || ':' || a.attnotnull::text || ':' || a.attcollation::text || ':'
    || pg_catalog.has_column_privilege(c.oid,a.attnum,'SELECT')::text,
       ',' ORDER BY a.attname)
   FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0
   AND NOT a.attisdropped AND a.attname IN :columns) AS column_stamp,
  (SELECT string_agg(pg_catalog.pg_get_indexdef(i.indexrelid) || ':'
    || i.indisvalid::text,
    ',' ORDER BY i.indexrelid) FROM pg_catalog.pg_index i
    WHERE i.indrelid=c.oid AND i.indisunique) AS key_stamp,
  (SELECT string_agg(pg_catalog.pg_get_constraintdef(f.oid) || ':'
    || f.convalidated::text,
    ',' ORDER BY f.oid) FROM pg_catalog.pg_constraint f
    WHERE f.conrelid=c.oid AND f.contype='f') AS relationship_stamp
FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=:schema AND c.relname=:name AND c.relkind IN ('r','v','m')
"""
_MSSQL_STAMP = """
SELECT CAST(o.object_id AS varchar(12)) + ':'
  + CONVERT(varchar(33),o.modify_date,126) AS object_stamp,
  (SELECT c.name,c.user_type_id,c.max_length,c.precision,c.scale,c.is_nullable,
          c.collation_name,
          HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name),'OBJECT',
                           'SELECT',c.name,'COLUMN') AS readable
   FROM sys.columns c WHERE c.object_id=o.object_id AND c.name IN :columns
   ORDER BY c.name FOR XML PATH('c')) AS column_stamp,
  (SELECT i.index_id,i.is_unique,i.is_disabled,i.has_filter,
          p.column_id,p.key_ordinal
   FROM sys.indexes i JOIN sys.index_columns p
     ON p.object_id=i.object_id AND p.index_id=i.index_id
   WHERE i.object_id=o.object_id AND i.is_unique=1
   ORDER BY i.index_id,p.key_ordinal FOR XML PATH('k')) AS key_stamp
FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
WHERE s.name=:schema AND o.name=:name AND o.type IN ('U','V')
"""


def _column_kind(type_name: str) -> str:
    name = type_name.lower()
    if name in {
        "varchar",
        "nvarchar",
        "char",
        "nchar",
        "text",
        "ntext",
        "bpchar",
        "uuid",
    }:
        return "text"
    if name in {"int", "int2", "int4", "int8", "bigint", "smallint", "tinyint"}:
        return "integer"
    if name in {"numeric", "decimal", "money", "float", "real", "float4", "float8"}:
        return "decimal"
    if name in {"bit", "bool", "boolean"}:
        return "boolean"
    if name in {
        "date",
        "datetime",
        "datetime2",
        "datetimeoffset",
        "timestamp",
        "timestamptz",
    }:
        return "date"
    return "other"


class EngineConnector:
    """Metadata and execution share enforced database/driver query deadlines."""

    def __init__(self, config: SourceConnection) -> None:
        self.config = config
        parsed = make_url(config.url.get_secret_value())
        arguments = (
            {"connect_timeout": config.connect_timeout_seconds}
            if config.engine == "postgresql"
            else {"timeout": config.connect_timeout_seconds}
        )
        self.engine = create_engine(
            parsed,
            connect_args=arguments,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_timeout=config.connect_timeout_seconds,
        )
        provenance = {
            "connection": parsed.render_as_string(hide_password=True),
            "scope": [item.model_dump() for item in config.discovery_scope],
        }
        self.source_fingerprint = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest()

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.config.query_timeout_seconds

    @contextmanager
    def connection(self, *, consistent: bool = False) -> Iterator[Connection]:
        try:
            with self.engine.connect() as connection:
                if consistent:
                    connection = connection.execution_options(
                        isolation_level="REPEATABLE READ"
                        if self.config.engine == "postgresql"
                        else "SERIALIZABLE"
                    )
                if self.config.engine == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": str(self.config.query_timeout_seconds * 1000)},
                    )
                else:
                    # pyodbc applies this SQL_ATTR_QUERY_TIMEOUT to every cursor;
                    # the driver cancels the statement on timeout, no orphan thread.
                    connection.connection.driver_connection.timeout = (
                        self.config.query_timeout_seconds
                    )
                    if consistent:
                        connection.execute(text("SET LOCK_TIMEOUT 2000"))
                yield connection
        except SQLAlchemyError as exc:
            original = getattr(exc, "orig", None)
            arguments = getattr(original, "args", ())
            state = str(arguments[0]) if arguments else ""
            if state in {"HYT00", "HYT01", "57014"}:
                code = "source_query_timeout"
            elif state == "42501" or (
                state == "42000"
                and any(f"({number})" in str(original) for number in (229, 230))
            ):
                code = "source_read_permission_denied"
            elif state == "42000" and "(195)" in str(original):
                code = "source_query_feature_unsupported"
            else:
                code = "source_query_rejected"
            raise SourceMappingError(code) from None

    def relationship_inventory(self, connection, product):
        """Complete inbound FK inventory, or None when metadata is not provable.

        SQL Server hides ungranted objects from metadata. Per-table visibility
        cannot establish the absence of incoming references.
        """
        if self.config.engine == "postgresql":
            sql = """
                SELECT f.oid AS relationship_id, sn.nspname AS schema_name,
                    sc.relname AS object_name, sa.attname AS source_column,
                    ta.attname AS target_column
                FROM pg_catalog.pg_constraint f
                JOIN pg_catalog.pg_class sc ON sc.oid=f.conrelid
                JOIN pg_catalog.pg_namespace sn ON sn.oid=sc.relnamespace
                JOIN pg_catalog.pg_class tc ON tc.oid=f.confrelid
                JOIN pg_catalog.pg_namespace tn ON tn.oid=tc.relnamespace
                CROSS JOIN LATERAL unnest(f.conkey,f.confkey) WITH ORDINALITY k(s,t,p)
                JOIN pg_catalog.pg_attribute sa ON sa.attrelid=sc.oid AND sa.attnum=k.s
                JOIN pg_catalog.pg_attribute ta ON ta.attrelid=tc.oid AND ta.attnum=k.t
                WHERE f.contype='f' AND tn.nspname=:schema AND tc.relname=:name
                ORDER BY f.oid,k.p
            """
            # RLS can hide referenced records from a SELECT-based proof.
            if connection.scalar(
                text("SELECT count(*) FROM pg_catalog.pg_class WHERE relrowsecurity")
            ):
                return None
        else:
            if (
                connection.scalar(
                    text(
                        "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW "
                        "DEFINITION')"
                    )
                )
                != 1
            ):
                return None
            sql = """
                SELECT f.object_id AS relationship_id, ss.name AS schema_name,
                    st.name AS object_name, sc.name AS source_column,
                    tc.name AS target_column
                FROM sys.foreign_keys f
                JOIN sys.tables st ON st.object_id=f.parent_object_id
                JOIN sys.schemas ss ON ss.schema_id=st.schema_id
                JOIN sys.tables tt ON tt.object_id=f.referenced_object_id
                JOIN sys.schemas ts ON ts.schema_id=tt.schema_id
                JOIN sys.foreign_key_columns p ON p.constraint_object_id=f.object_id
                JOIN sys.columns sc ON sc.object_id=st.object_id
                    AND sc.column_id=p.parent_column_id
                JOIN sys.columns tc ON tc.object_id=tt.object_id
                    AND tc.column_id=p.referenced_column_id
                WHERE ts.name=:schema AND tt.name=:name
                ORDER BY f.object_id,p.constraint_column_id
            """
            if connection.scalar(
                text("SELECT count(*) FROM sys.security_policies WHERE is_enabled=1")
            ):
                return None
        rows = (
            connection.execute(
                text(sql), {"schema": product.schema_name, "name": product.name}
            )
            .mappings()
            .fetchmany(65)
        )
        if len(rows) > 64:
            return None
        groups = {}
        for row in rows:
            groups.setdefault(
                (row["relationship_id"], row["schema_name"], row["object_name"]), []
            ).append((row["source_column"], row["target_column"]))
        return tuple(
            (schema, name, tuple(pairs)) for (_, schema, name), pairs in groups.items()
        )

    def discover(
        self,
        connection: Connection | None = None,
        *,
        object_ids=None,
        column_scope=None,
        available_only=False,
    ) -> SchemaDiscovery:
        if connection is None:
            with self.connection() as opened:
                return self.discover(
                    opened,
                    object_ids=object_ids,
                    column_scope=column_scope,
                    available_only=available_only,
                )
        column_sql, key_sql = (
            (_POSTGRES_COLUMNS, _POSTGRES_KEYS)
            if self.config.engine == "postgresql"
            else (_MSSQL_COLUMNS, _MSSQL_KEYS)
        )
        objects = []
        relationship_rows = []
        for index, scope in enumerate(self.config.discovery_scope, 1):
            object_id = f"o{index}"
            if object_ids is not None and object_id not in object_ids:
                continue
            if column_scope is not None:
                selected = column_scope.get(object_id, set())
                if not selected:
                    continue
                if not set(selected) <= set(scope.columns):
                    raise SourceMappingError("mapping_scope_changed")
                scope = scope.model_copy(update={"columns": tuple(sorted(selected))})
            params = {"schema": scope.schema_name, "name": scope.object_name}
            rows = connection.execute(text(column_sql), params).mappings().all()
            by_name = {row["name"]: row for row in rows}
            if any(
                name not in by_name or not by_name[name]["readable"]
                for name in scope.columns
            ):
                if not available_only:
                    raise SourceMappingError("discovery_permission_or_schema_changed")
                readable = tuple(
                    name
                    for name in scope.columns
                    if name in by_name and by_name[name]["readable"]
                )
                if not readable:
                    continue
                scope = scope.model_copy(update={"columns": readable})
            columns = tuple(
                DiscoveredColumn(
                    name=name,
                    kind=_column_kind(by_name[name]["type_name"]),
                    nullable=bool(by_name[name]["nullable"]),
                    type_signature=by_name[name]["type_signature"],
                )
                for name in sorted(scope.columns)
            )
            keys: dict[int, list[str]] = {}
            for row in connection.execute(text(key_sql), params).mappings():
                keys.setdefault(row["key_id"], []).append(row["name"])
            approved_keys = tuple(
                sorted(
                    {
                        tuple(value)
                        for value in keys.values()
                        if set(value) <= set(scope.columns)
                    }
                )
            )
            objects.append(
                DiscoveredObject(
                    object_id=f"o{index}",
                    schema_name=scope.schema_name,
                    name=scope.object_name,
                    columns=columns,
                    unique_keys=approved_keys,
                    validation_stamp=self._stamp(connection, scope),
                )
            )
            relation_sql = (
                _POSTGRES_RELATIONSHIPS
                if self.config.engine == "postgresql"
                else _MSSQL_RELATIONSHIPS
            )
            relation_keys = {}
            for row in connection.execute(text(relation_sql), params).mappings():
                relation_keys.setdefault(row["key_id"], []).append(dict(row))
            relationship_rows.append((f"o{index}", relation_keys))
        approved_objects = {(obj.schema_name, obj.name): obj for obj in objects}
        by_id = {obj.object_id: obj for obj in objects}
        relationships = []
        for source_id, keys in relationship_rows:
            source_columns = {column.name for column in by_id[source_id].columns}
            for rows in keys.values():
                target = approved_objects.get(
                    (rows[0]["target_schema"], rows[0]["target_name"])
                )
                if target is None:
                    continue
                allowed = {column.name for column in target.columns}
                if all(
                    row["source_column"] in source_columns
                    and row["target_column"] in allowed
                    for row in rows
                ):
                    relationships.append(
                        DiscoveredRelationship(
                            source_object_id=source_id,
                            source_columns=tuple(row["source_column"] for row in rows),
                            target_object_id=target.object_id,
                            target_columns=tuple(row["target_column"] for row in rows),
                        )
                    )
        if not objects:
            raise SourceMappingError("discovery_no_readable_scope")
        return SchemaDiscovery(
            engine=self.config.engine,
            source_fingerprint=self.source_fingerprint,
            objects=tuple(objects),
            relationships=tuple(relationships),
        )

    def accessible_metadata(self) -> dict:
        """Bounded metadata only. This does not expand the execution allowlist."""
        if self.config.engine == "sqlserver":
            sql = """
                SELECT TOP (513) s.name AS schema_name,o.name AS object_name,
                    c.name AS column_name,t.name AS type_name,
                    HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name),
                        'OBJECT','SELECT',c.name,'COLUMN') AS readable
                FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
                JOIN sys.columns c ON c.object_id=o.object_id
                JOIN sys.types t ON t.user_type_id=c.user_type_id
                WHERE o.type IN ('U','V') AND o.is_ms_shipped=0
                ORDER BY s.name,o.name,c.column_id
            """
        else:
            sql = """
                SELECT n.nspname AS schema_name,c.relname AS object_name,
                    a.attname AS column_name,
                    pg_catalog.format_type(a.atttypid,a.atttypmod) AS type_name,
                    pg_catalog.has_column_privilege(c.oid,a.attnum,'SELECT') AS readable
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
                WHERE c.relkind IN ('r','v','m') AND a.attnum>0 AND NOT a.attisdropped
                    AND n.nspname NOT IN ('pg_catalog','information_schema')
                    AND n.nspname NOT LIKE 'pg_toast%'
                    AND pg_catalog.has_schema_privilege(n.oid,'USAGE')
                ORDER BY n.nspname,c.relname,a.attnum LIMIT 513
            """
        allowed = {
            (s.schema_name, s.object_name): set(s.columns)
            for s in self.config.discovery_scope
        }
        with self.connection() as connection:
            rows = connection.execute(text(sql)).mappings().fetchmany(513)
        objects = {}
        for row in rows[:512]:
            key = (row["schema_name"], row["object_name"])
            entry = objects.setdefault(
                key, dict(schema_name=key[0], object_name=key[1], columns=[])
            )
            entry["columns"].append(
                dict(
                    name=row["column_name"],
                    kind=_column_kind(row["type_name"]),
                    type_name=row["type_name"],
                    readable=bool(row["readable"]),
                    approved_scope=row["column_name"] in allowed.get(key, set()),
                )
            )
        return {
            "objects": list(objects.values()),
            "truncated": len(rows) > 512,
            "limitation": (
                "Visible metadata is bounded and may omit objects hidden "
                "from the reader. Only deployment-approved columns may be "
                "queried; absent access is not proof that business data "
                "does not exist."
            ),
        }

    def _stamp(self, connection: Connection, scope: DiscoveryScope) -> str:
        statement = text(
            _POSTGRES_STAMP if self.config.engine == "postgresql" else _MSSQL_STAMP
        ).bindparams(bindparam("columns", expanding=True))
        row = (
            connection.execute(
                statement,
                {
                    "schema": scope.schema_name,
                    "name": scope.object_name,
                    "columns": tuple(sorted(scope.columns)),
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise SourceMappingError("mapping_schema_changed")
        return hashlib.sha256(
            json.dumps(dict(row), sort_keys=True).encode()
        ).hexdigest()

    def assert_schema(
        self, expected: SchemaDiscovery, connection: Connection, *, dependencies=False
    ) -> None:
        if expected.source_fingerprint != self.source_fingerprint:
            raise SourceMappingError("mapping_source_changed")
        allowed = {
            (scope.schema_name, scope.object_name): set(scope.columns)
            for scope in self.config.discovery_scope
        }
        names = {(obj.schema_name, obj.name) for obj in expected.objects}
        if (not names <= allowed.keys()) or (
            not dependencies and names != allowed.keys()
        ):
            raise SourceMappingError("mapping_scope_changed")
        for obj in expected.objects:
            columns = {column.name for column in obj.columns}
            permitted = allowed[(obj.schema_name, obj.name)]
            if not columns <= permitted or (not dependencies and columns != permitted):
                raise SourceMappingError("mapping_scope_changed")
            scope = DiscoveryScope(
                schema_name=obj.schema_name,
                object_name=obj.name,
                columns=tuple(column.name for column in obj.columns),
            )
            if (
                obj.validation_stamp is None
                or self._stamp(connection, scope) != obj.validation_stamp
            ):
                raise SourceMappingError("mapping_schema_changed")

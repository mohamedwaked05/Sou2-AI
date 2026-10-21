"""Accounting compatibility tests without network, Alembic env or native DBs.

Migration operations are captured, never applied. Its summary SELECT runs verbatim
(with bound parameter names) on disposable SQLite. Lock/accounting semantics
are controlled doubles; PostgreSQL permissions and locks still need native tests.
"""

import ast
import importlib.util
import re
import socket
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.services import ai_usage
from sqlalchemy import Engine
from sqlalchemy.exc import ProgrammingError
from test_phase3_reliability import NOW, USAGE, summary_row

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions/20261013_17_reservation_runtime_guards.py"
)


@pytest.fixture(autouse=True)
def forbid_live_connections(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("Only offline accounting fixtures are allowed")

    monkeypatch.setattr(Engine, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location("reservation_guards", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emitted_sql(migration, direction):
    statements = []
    migration.op = SimpleNamespace(execute=statements.append)
    getattr(migration, direction)()
    return "\n\n".join(s.strip().removesuffix(";") + ";" for s in statements)


def test_additive_migration_head_and_security_boundary(migration):
    revisions = {}
    for path in MIGRATION_PATH.parent.glob("*.py"):
        fields = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign):
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id in {
                "revision",
                "down_revision",
            }:
                fields[target.id] = ast.literal_eval(node.value)
        if fields:
            revisions[fields["revision"]] = fields["down_revision"]
    assert set(revisions) - set(revisions.values()) == {"20261018_19"}
    assert revisions["20261018_19"] == "20261015_18"
    assert revisions["20261015_18"] == migration.revision
    visited, current = set(), "20261018_19"
    while current is not None:
        assert current in revisions and current not in visited
        visited.add(current)
        current = revisions[current]
    assert visited == set(revisions)
    assert migration.down_revision == "20261007_16"
    sql = emitted_sql(migration, "upgrade")
    assert sql.count("CREATE FUNCTION") == 2
    assert sql.count("SECURITY DEFINER") == 2
    assert sql.count("SET search_path = pg_catalog") == 2
    for function in (migration.LOCK_FUNCTION, migration.SUMMARY_FUNCTION):
        assert f"ALTER FUNCTION {function} OWNER TO sou2ai_migrator" in sql
        assert f"REVOKE ALL ON FUNCTION {function}" in sql
        assert "FROM PUBLIC, sou2ai_runtime, sou2ai_lifecycle_operator" in sql
        assert f"GRANT EXECUTE ON FUNCTION {function} TO sou2ai_runtime" in sql
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\s+(INTO|public\.)", sql)
    assert not re.search(r"\b(GRANT|REVOKE)\b.*\b(TABLE|SCHEMA)\b", sql)
    assert "CREATE OR REPLACE" not in sql and "EXECUTE format" not in sql


def test_lock_checks_expiry_after_contested_lock_and_returns_only_deadlines(migration):
    sql = emitted_sql(migration, "upgrade")
    lock_body = sql.split("$function$")[1]
    assert "WHERE r.id = target_reservation_id" in lock_body
    assert lock_body.index("FOR UPDATE OF r") < lock_body.index("clock_timestamp()")
    assert "IF NOT FOUND OR reservation_record.status <> 'reserved'" in lock_body
    assert "reservation_record.lease_expires_at <= checked_at" in lock_body
    assert "reservation_record.window_end <= checked_at" in lock_body
    assert "target_finalization_deadline <= checked_at" in lock_body
    assert "RETURNS TABLE(lease_expires_at timestamptz, window_end timestamptz)" in sql
    assert "r.*" not in lock_body and "content" not in lock_body


def test_downgrade_removes_only_new_functions(migration):
    sql = emitted_sql(migration, "downgrade")
    assert sql.strip().splitlines() == [
        f"DROP FUNCTION {migration.SUMMARY_FUNCTION};",
        "",
        f"DROP FUNCTION {migration.LOCK_FUNCTION};",
    ]
    assert "CASCADE" not in sql


@pytest.mark.parametrize(
    "case,expected",
    [
        ("reserved", True),
        ("charged", True),
        ("completed", True),
        ("released", False),
        ("missing", False),
        ("wrong_summary", False),
        ("wrong_business", False),
        ("wrong_conversation", False),
        ("wrong_attempt", False),
        ("stale_attempt", False),
        ("reservation_business_mismatch", False),
        ("conversation_business_mismatch", False),
    ],
)
def test_summary_attempt_executes_real_scoped_predicate(migration, case, expected):
    sql = emitted_sql(migration, "upgrade")
    body = re.search(
        r"CREATE FUNCTION public\.sou2ai_summary_attempt_has_usage\(.*?"
        r"AS \$function\$(.*?)\$function\$",
        sql,
        re.S,
    )[1]
    body = re.sub(r"\b(target_\w+)\b", r":\1", body)
    summary = summary_row(generation_attempts=2)
    other = uuid.uuid4()
    with sqlite3.connect(":memory:") as db:
        db.execute("ATTACH DATABASE ':memory:' AS public")
        db.execute("CREATE TABLE public.owner_conversations(id TEXT, business_id TEXT)")
        db.execute(
            "CREATE TABLE public.owner_conversation_summaries("
            "id TEXT, business_id TEXT, conversation_id TEXT, generation_attempts INT)"
        )
        db.execute(
            "CREATE TABLE public.ai_usage_reservations(conversation_summary_id TEXT,"
            "business_id TEXT, generation_attempt INT, status TEXT)"
        )
        db.execute(
            "INSERT INTO public.owner_conversations VALUES(?,?)",
            (
                str(summary.conversation_id),
                str(
                    other
                    if case == "conversation_business_mismatch"
                    else summary.business_id
                ),
            ),
        )
        db.execute(
            "INSERT INTO public.owner_conversation_summaries VALUES(?,?,?,?)",
            (
                str(summary.id),
                str(summary.business_id),
                str(summary.conversation_id),
                3 if case == "stale_attempt" else 2,
            ),
        )
        if case != "missing":
            db.execute(
                "INSERT INTO public.ai_usage_reservations VALUES(?,?,?,?)",
                (
                    str(summary.id),
                    str(
                        other
                        if case == "reservation_business_mismatch"
                        else summary.business_id
                    ),
                    2,
                    case
                    if case in {"reserved", "charged", "completed", "released"}
                    else "reserved",
                ),
            )
        before = db.total_changes

        class SummaryFunctionSession:
            def execute(self, statement, parameters):
                assert "sou2ai_summary_attempt_has_usage" in str(statement)
                assert set(parameters) == {
                    "summary_id",
                    "business_id",
                    "conversation_id",
                    "attempt",
                }
                bound = {
                    "target_summary_id": str(parameters["summary_id"]),
                    "target_business_id": str(parameters["business_id"]),
                    "target_conversation_id": str(parameters["conversation_id"]),
                    "target_generation_attempt": parameters["attempt"],
                }
                value = bool(db.execute(body, bound).fetchone()[0])
                return SimpleNamespace(scalar_one=lambda: value)

        for field, mismatch in (
            ("id", "wrong_summary"),
            ("business_id", "wrong_business"),
            ("conversation_id", "wrong_conversation"),
        ):
            if case == mismatch:
                setattr(summary, field, other)
        if case == "wrong_attempt":
            summary.generation_attempts = 1
        assert (
            ai_usage.summary_attempt_has_usage(SummaryFunctionSession(), summary)
            is expected
        )
        assert db.total_changes == before


class RestrictedAccountingSession:
    """Reject table reads; expose only the narrow function contracts and DB clock."""

    def __init__(self, *, status="reserved", lease=60, window=3600):
        self.id = uuid.uuid4()
        self.now = NOW
        self.reservation = SimpleNamespace(
            status=status,
            lease_expires_at=NOW + timedelta(seconds=lease),
            window_end=NOW + timedelta(seconds=window),
        )
        self.calls = []
        self.charged = 0
        self.commits = 0
        self.wait_seconds = 0

    def scalar(self, statement):
        assert "clock_timestamp" in str(statement) and "FROM" not in str(statement)
        return self.now

    def execute(self, statement, parameters):
        sql = str(statement)
        assert "FROM ai_usage_reservations" not in sql
        self.calls.append((sql, parameters))
        if "sou2ai_lock_active_ai_usage_reservation" in sql:
            deadline = parameters["finalization_deadline"]
            r = self.reservation
            active = (
                parameters["reservation_id"] == self.id
                and r.status == "reserved"
                and r.lease_expires_at > self.now
                and r.window_end > self.now
                and (deadline is None or deadline > self.now)
            )
            row = (
                SimpleNamespace(
                    lease_expires_at=r.lease_expires_at,
                    window_end=r.window_end,
                )
                if active
                else None
            )
            return SimpleNamespace(one_or_none=lambda: row)
        assert "sou2ai_reconcile_ai_usage(" in sql
        assert parameters["reservation_id"] == self.id
        changed = self.reservation.status == "reserved"
        if changed:
            self.charged += (
                100
                if parameters["outcome"] == "uncertain"
                else 0
                if parameters["outcome"] == "release"
                else parameters["input_tokens"] + parameters["output_tokens"]
            )
            self.reservation.status = "completed"
        self.now += timedelta(seconds=self.wait_seconds)
        return SimpleNamespace(one=lambda: SimpleNamespace(reconciled=changed))

    def commit(self):
        self.commits += 1


def test_strict_finalization_and_replay_need_no_table_permissions():
    session = RestrictedAccountingSession()
    assert ai_usage.reconcile_ai_usage(
        session,
        session.id,
        usage=USAGE,
        outcome="completed",
        require_new=True,
        finalization_deadline=NOW + timedelta(seconds=30),
    )
    assert session.charged == USAGE.total_tokens and session.commits == 1
    with pytest.raises(RuntimeError, match="ai_usage_reservation_not_active"):
        ai_usage.reconcile_ai_usage(
            session, session.id, usage=USAGE, outcome="completed", require_new=True
        )
    assert not ai_usage.reconcile_ai_usage(
        session, session.id, usage=None, outcome="uncertain"
    )
    assert session.charged == USAGE.total_tokens


@pytest.mark.parametrize(
    "status,lease,window,deadline",
    [
        ("charged", 60, 3600, 30),
        ("released", 60, 3600, 30),
        ("reserved", 0, 3600, 30),
        ("reserved", 60, 0, 30),
        ("reserved", 60, 3600, 0),
    ],
)
def test_inactive_guard_never_calls_accounting(status, lease, window, deadline):
    session = RestrictedAccountingSession(status=status, lease=lease, window=window)
    with pytest.raises(RuntimeError, match="ai_usage_reservation_not_active"):
        ai_usage.reconcile_ai_usage(
            session,
            session.id,
            usage=USAGE,
            outcome="completed",
            require_new=True,
            finalization_deadline=NOW + timedelta(seconds=deadline),
        )
    assert len(session.calls) == 1 and session.charged == session.commits == 0


def test_accounting_wait_expiry_still_prevents_response_commit():
    session = RestrictedAccountingSession()
    session.wait_seconds = 5
    with pytest.raises(RuntimeError, match="ai_usage_finalization_deadline_expired"):
        ai_usage.reconcile_ai_usage(
            session,
            session.id,
            usage=USAGE,
            outcome="completed",
            require_new=True,
            finalization_deadline=NOW + timedelta(seconds=4),
        )
    assert session.commits == 0  # The caller owns response/accounting rollback.


@pytest.mark.parametrize("outcome", ["uncertain", "release"])
def test_expired_failure_accounting_uses_existing_interface(outcome):
    session = RestrictedAccountingSession(lease=-1)
    assert ai_usage.reconcile_ai_usage(session, session.id, usage=None, outcome=outcome)
    assert len(session.calls) == 1
    assert session.charged == (100 if outcome == "uncertain" else 0)


def test_missing_migration_fails_closed_without_table_access_or_fallback():
    class MissingMigrationSession(RestrictedAccountingSession):
        def execute(self, statement, parameters):
            assert "sou2ai_lock_active_ai_usage_reservation" in str(statement)
            raise ProgrammingError("controlled function unavailable", {}, Exception())

    session = MissingMigrationSession()
    with pytest.raises(ProgrammingError):
        ai_usage.reconcile_ai_usage(
            session, session.id, usage=USAGE, outcome="completed", require_new=True
        )
    assert session.charged == session.commits == 0

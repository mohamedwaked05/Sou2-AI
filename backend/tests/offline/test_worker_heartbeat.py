"""Worker-owned identity survives an expired Redis lease without altering jobs."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.worker.runtime import PrivacySafeWorker
from rq import Worker


class MemoryRedis:
    def __init__(self):
        self.hashes = {"rq:job:retained": {"data": "unchanged"}}
        self.ttls = {}
        self.pending = []
        self.executions = 0

    def pipeline(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def hset(self, key, field=None, value=None, mapping=None):
        self.pending.append(("hash", key, mapping or {field: value}))

    def expire(self, key, ttl):
        self.pending.append(("ttl", key, ttl))

    def execute(self):
        for kind, key, value in self.pending:
            if kind == "hash":
                self.hashes.setdefault(key, {}).update(value)
            else:
                self.ttls[key] = value
        self.pending.clear()
        self.executions += 1


def worker(connection, state="idle"):
    instance = object.__new__(PrivacySafeWorker)
    instance.connection = connection
    instance.name = "actual-running-worker"
    instance.worker_ttl = 420
    instance.birth_date = datetime(2026, 10, 21, tzinfo=UTC)
    instance.last_heartbeat = instance.birth_date
    instance.queues = [
        SimpleNamespace(name=q) for q in ("knowledge", "customer-messages")
    ]
    instance.pid = 123
    instance.hostname = "actual-worker-host"
    instance.ip_address = "127.0.0.1"
    instance.version = "fixture-rq"
    instance.python_version = "fixture-python"
    instance._state = state
    instance.log = SimpleNamespace(debug=lambda *args: None)
    return instance


def test_rq_heartbeat_alone_reproduces_partial_registration_after_expiry():
    connection = MemoryRedis()
    instance = worker(connection)
    Worker.heartbeat(instance, pipeline=connection)
    connection.execute()
    assert set(connection.hashes[instance.key]) == {"last_heartbeat"}


@pytest.mark.parametrize("state", ["idle", "busy"])
def test_heartbeat_restores_only_the_running_workers_own_rq_identity(state):
    connection = MemoryRedis()
    instance = worker(connection, state)
    instance.heartbeat()
    row = connection.hashes[instance.key]
    assert row == {**instance.serialize(), "state": state}
    assert row["hostname"] == instance.hostname and row["pid"] == instance.pid
    assert row["queues"] == "knowledge,customer-messages"
    assert connection.ttls[instance.key] == 480
    assert connection.executions == 1
    assert connection.hashes["rq:job:retained"] == {"data": "unchanged"}


def test_caller_pipeline_keeps_transaction_ownership_and_existing_job_fields():
    connection = MemoryRedis()
    instance = worker(connection, "busy")
    connection.hashes[instance.key] = {
        "current_job": "real-running-job",
        "successful_job_count": 7,
    }
    instance.heartbeat(timeout=900, pipeline=connection)
    assert connection.executions == 0
    assert "birth" not in connection.hashes[instance.key]
    connection.execute()
    assert connection.hashes[instance.key]["current_job"] == "real-running-job"
    assert connection.hashes[instance.key]["successful_job_count"] == 7
    assert connection.hashes[instance.key]["state"] == "busy"
    assert connection.ttls[instance.key] == 900

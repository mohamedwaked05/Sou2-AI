"""RQ failure boundary: sanitize logs and Redis failure records alike."""

import re
import sys
from collections.abc import Sequence

from rq import Queue, Worker
from rq.job import Job

from app.core.admission import require_open_admission
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.runtime import (
    WORKER_CONTRACT_VERSION,
    source_version,
    validate_worker_release,
)


def _check_release(queues: Sequence[Queue | str]) -> None:
    try:
        settings = get_settings()
    except Exception:
        # Pydantic settings errors may embed the entire environment, including keys.
        raise RuntimeError("worker_configuration_invalid") from None
    configure_logging(settings.log_level, settings.environment)
    validate_worker_release(
        settings, [queue if isinstance(queue, str) else queue.name for queue in queues]
    )


class PrivacySafeWorker(Worker):
    def __init__(self, queues, *args, **kwargs):
        _check_release(queues)
        super().__init__(queues, *args, **kwargs)

    def work(self, *args, **kwargs):
        _check_release(self.queues)
        require_open_admission(get_settings())
        self.log.info(
            "worker_start source_version=%s contract_version=%s",
            source_version(),
            WORKER_CONTRACT_VERSION,
        )
        return super().work(*args, **kwargs)

    def heartbeat(self, timeout=None, pipeline=None):
        # Redis TTLs keep advancing while a development host is asleep. RQ's
        # heartbeat alone recreates an expired hash with only last_heartbeat.
        # Refresh the running worker's own RQ identity and in-memory state in the
        # same transaction as its heartbeat; retain job fields and all history.
        if pipeline is None:
            with self.connection.pipeline() as transaction:
                self.heartbeat(timeout, pipeline=transaction)
                transaction.execute()
            return
        super().heartbeat(timeout, pipeline=pipeline)
        pipeline.hset(self.key, mapping={**self.serialize(), "state": self.get_state()})

    def run_maintenance_tasks(self):
        result = super().run_maintenance_tasks()
        from app.worker.knowledge import recover_stale_documents

        try:
            recover_stale_documents(get_settings())
        except Exception:
            self.log.warning("knowledge_recovery_failed code=processing_unavailable")
        return result

    def handle_exception(self, job, *exc_info):
        self.log.error(
            "worker_job_failed job_id=%s exception_type=%s",
            job.id,
            exc_info[0].__name__ if exc_info else "unknown",
        )

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):
        exception = sys.exc_info()[0]
        classification = exception.__name__ if exception is not None else "unknown"
        metadata = getattr(job, "meta", {})
        if isinstance(job, Job):
            try:
                metadata = job.get_meta(refresh=True)
            except Exception:
                pass
        metadata = metadata if isinstance(metadata, dict) else {}
        code = metadata.get("embedding_failure_code")
        detail = (
            f" code={code}"
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code)
            else ""
        )
        if metadata.get("embedding_failure_retryable") is False:
            job.retries_left = 0
        if getattr(job, "func_name", None) in {
            "app.worker.knowledge.process_document",
            "app.rag.reembed.process_reembed",
        }:
            job.retry_intervals = [2, 8]
        http_status = metadata.get("embedding_http_status")
        if isinstance(http_status, int) and 400 <= http_status <= 599:
            detail += f" http_status={http_status}"
        delay = metadata.get("embedding_retry_after_seconds")
        if (
            isinstance(delay, int)
            and not isinstance(delay, bool)
            and 0 < delay <= 86400
        ):
            job.retry_intervals = [delay]
            detail += f" retry_after_seconds={delay}"
        return super().handle_job_failure(
            job,
            queue,
            started_job_registry=started_job_registry,
            exc_string=f"{classification}:{detail} [REDACTED_INTERNAL_EXCEPTION]",
        )

"""Non-secret running-source identity and queued responsibility contracts."""

import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from rq import get_current_job
from rq.job import Job, JobStatus

from app.core.admission import require_open_admission
from app.core.config import Settings
from app.core.logging import configure_logging, request_id_context

WORKER_CONTRACT_VERSION = 3
logger = logging.getLogger(__name__)


def validate_release_settings(settings: Settings) -> None:
    """Deployment boundaries require deliberate selections, including local mocks."""
    if "environment" not in settings.model_fields_set:
        raise RuntimeError("release_configuration_missing_environment")
    providers = (
        "owner_chat_provider",
        "source_mapping_provider",
        "conversation_summary_provider",
        "customer_chat_provider",
    )
    for field in providers:
        if field not in settings.model_fields_set or getattr(settings, field) is None:
            raise RuntimeError(f"release_configuration_missing_{field}")
    selected = {getattr(settings, field) for field in providers}
    if "mock" in selected and settings.environment.casefold() not in {
        "development",
        "testing",
    }:
        raise RuntimeError("release_mock_provider_forbidden")
    for provider, field in (
        ("gemini", "gemini_chat_model"),
        ("ollama", "ollama_chat_model"),
    ):
        if provider in selected and (
            field not in settings.model_fields_set
            or not getattr(settings, field).strip()
        ):
            raise RuntimeError(f"release_configuration_missing_{field}")


def validate_worker_release(settings: Settings, queues: list[str]) -> None:
    validate_release_settings(settings)
    expected = settings.worker_expected_source_version
    if (
        expected is None
        or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        or expected != source_version()
    ):
        raise RuntimeError("worker_release_source_mismatch")
    required = [settings.knowledge_queue_name, settings.customer_message_queue_name]
    if len(set(required)) != 2 or sorted(queues) != sorted(required):
        raise RuntimeError("worker_release_queue_mismatch")


def preserve_incompatible_failed_job(
    job: Job, settings: Settings, responsibility: str
) -> None:
    """Do not erase legacy failure evidence or stamp a fresh contract onto it."""
    if job.get_status(refresh=True) == JobStatus.FAILED and (
        job.meta.get("runtime_contract") != job_contract(settings, responsibility)
    ):
        logger.warning(
            "worker_enqueue_rejected reason=failed_job_contract_mismatch "
            "responsibility=%s",
            responsibility,
        )
        raise RuntimeError("worker_failed_job_review_required")


@lru_cache
def source_version() -> str:
    """Fingerprint shipped application sources, including uncommitted changes."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def worker_provider_settings(settings: Settings, responsibility: str) -> Settings:
    if responsibility not in {"summary", "customer"}:
        raise ValueError("unknown_generation_responsibility")
    provider = (
        settings.conversation_summary_provider
        if responsibility == "summary"
        else settings.customer_chat_provider
    )
    if provider is None:
        raise RuntimeError("worker_provider_not_configured")
    return settings.model_copy(update={"owner_chat_provider": provider})


def job_contract(settings: Settings, responsibility: str) -> dict[str, object]:
    if responsibility not in {"summary", "customer", "knowledge", "outbound"}:
        raise ValueError("unknown_worker_responsibility")
    contract: dict[str, object] = {
        "contract_version": WORKER_CONTRACT_VERSION,
        "source_version": source_version(),
        "responsibility": responsibility,
        "environment": settings.environment.casefold(),
    }
    if responsibility in {"summary", "customer"}:
        resolved = worker_provider_settings(settings, responsibility)
        provider = resolved.owner_chat_provider
        contract["provider"] = provider
        contract["model"] = (
            resolved.gemini_chat_model
            if provider == "gemini"
            else resolved.ollama_chat_model
            if provider == "ollama"
            else "deterministic-v2"
            if responsibility == "summary"
            else "deterministic"
        )
        contract["generation_lease_seconds"] = (
            settings.owner_chat_generation_lease_seconds
            if responsibility == "summary"
            else settings.customer_generation_lease_seconds
        )
        contract["provider_timeout_seconds"] = (
            resolved.gemini_request_timeout_seconds
            if provider == "gemini"
            else resolved.ollama_request_timeout_seconds
            if provider == "ollama"
            else None
        )
        if responsibility == "customer":
            contract["max_output_tokens"] = settings.customer_chat_max_output_tokens
    if responsibility in {"knowledge", "customer"}:
        contract["embedding_provider"] = settings.embedding_provider
        contract["embedding_model"] = settings.embedding_model
        contract["ollama_embedding_model"] = settings.ollama_embedding_model
        contract["embedding_dimensions"] = 1024
        contract["embedding_timeout_seconds"] = (
            settings.embedding_request_timeout_seconds
            if settings.embedding_provider == "gemini"
            else settings.ollama_request_timeout_seconds
        )
    if responsibility == "outbound":
        contract["provider"] = "meta_whatsapp"
        contract["request_timeout_seconds"] = settings.whatsapp_request_timeout_seconds
        contract["max_attempts"] = settings.whatsapp_outbound_max_attempts
    return contract


def worker_configuration_identity(settings: Settings) -> str:
    """Compare loaded API/worker contracts without credentials or source URLs."""
    contract = {
        responsibility: job_contract(settings, responsibility)
        for responsibility in ("summary", "customer", "knowledge", "outbound")
    }
    contract["generation"] = {
        "admission_mode": settings.admission_mode,
        "owner_provider": settings.owner_chat_provider,
        "mapping_provider": settings.source_mapping_provider,
        "gemini_model": settings.gemini_chat_model,
        "ollama_model": settings.ollama_chat_model,
        "queues": [settings.knowledge_queue_name, settings.customer_message_queue_name],
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_worker_job(settings: Settings, responsibility: str) -> None:
    """Reject missing or mismatched contracts before claiming database work."""
    require_open_admission(settings)
    configure_logging(settings.log_level, settings.environment)
    job = get_current_job()
    request_id_context.set(str(job.id) if job is not None else None)
    expected = job_contract(settings, responsibility)
    if job is not None and job.meta.get("runtime_contract") != expected:
        logger.error("worker_job_rejected reason=runtime_contract_mismatch")
        raise RuntimeError("worker_runtime_contract_mismatch")
    logger.info(
        "worker_runtime source_version=%s contract_version=%s "
        "responsibility=%s provider=%s model=%s",
        source_version(),
        WORKER_CONTRACT_VERSION,
        responsibility,
        expected.get("provider", "none"),
        expected.get("model", "none"),
    )

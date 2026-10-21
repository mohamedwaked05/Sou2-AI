"""RQ reads its endpoint from the private stdin-injected process environment."""

from app.core.config import get_settings

try:
    REDIS_URL = get_settings().redis_url
except Exception:
    raise RuntimeError("worker_configuration_invalid") from None

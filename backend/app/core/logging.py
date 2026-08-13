"""Standard-library logging setup shared by application modules."""

import logging


def configure_logging(log_level: str) -> None:
    """Configure timestamped console logging without adding duplicate handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())
    # HTTP client request logs include local provider URLs; provider services emit
    # their own privacy-safe operational reasons instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    handler = next(
        (
            existing_handler
            for existing_handler in root_logger.handlers
            if getattr(existing_handler, "_sou2ai_handler", False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._sou2ai_handler = True
        root_logger.addHandler(handler)

    handler.setLevel(log_level.upper())
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )

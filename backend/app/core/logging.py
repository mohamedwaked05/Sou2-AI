"""Standard-library logging setup shared by application modules."""

import logging


def configure_logging(log_level: str) -> None:
    """Configure timestamped console logging without adding duplicate handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

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

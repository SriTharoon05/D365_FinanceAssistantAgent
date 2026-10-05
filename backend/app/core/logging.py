import logging

import structlog


SENSITIVE_KEYS = {"authorization", "client_secret", "api_key", "access_token", "password", "cookie"}


def redact(_logger, _method, event_dict):
    def clean(value):
        if isinstance(value, dict):
            return {
                k: "[REDACTED]" if any(s in k.lower() for s in SENSITIVE_KEYS) else clean(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(event_dict)


def configure_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    # Third-party HTTP loggers can include URLs. Keep provider payloads out of logs.
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            redact,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

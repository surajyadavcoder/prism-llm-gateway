"""
Structured (JSON-lines) application logging, separate from the request_logs
DB table. The DB table is for business data (usage/cost/cache stats queried
by the API and console); this logger is for operational visibility --
grep/parse-friendly logs for tailing in production (e.g. piped into
CloudWatch/Datadog/ELK). Every log line carries request_id so a single
request can be traced across auth, routing, retries, and provider calls.
"""
import json
import logging
import sys
import time
import uuid


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            payload["request_id"] = record.request_id
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level=logging.INFO):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("prism")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    return root


logger = configure_logging()


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def log_event(level: str, message: str, request_id: str = None, **fields):
    extra = {"extra_fields": fields}
    if request_id:
        extra["request_id"] = request_id
    getattr(logger, level)(message, extra=extra)

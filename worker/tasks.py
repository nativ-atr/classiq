import json
import logging
import os
import time
from datetime import datetime, timezone

import redis
from celery.exceptions import SoftTimeLimitExceeded
from qiskit import qasm3
from qiskit.exceptions import QiskitError
from qiskit_aer import AerSimulator

from celery_app import app

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
NUM_SHOTS = int(os.environ.get("NUM_SHOTS", "1024"))
TASK_MAX_RETRIES = int(os.environ.get("TASK_MAX_RETRIES", "3"))
RESULT_TTL = os.environ.get("RESULT_TTL")

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _bounded(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(task_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    key = f"task:{task_id}"
    _client.hset(key, mapping=fields)
    if RESULT_TTL and fields.get("status") in ("COMPLETED", "FAILED"):
        _client.expire(key, int(RESULT_TTL))


def _safe_write_status(task_id: str, **fields) -> None:
    try:
        _write_status(task_id, **fields)
    except Exception as exc:
        logger.error("state write failed", extra={"task_id": task_id, "reason": _bounded(exc)})


def _safe_push_dlq(task_id: str, qc: str, reason: str) -> None:
    try:
        _client.lpush("dlq:tasks", json.dumps({"task_id": task_id, "qc": qc, "error": reason}))
    except Exception as exc:
        logger.error("dlq push failed", extra={"task_id": task_id, "reason": _bounded(exc)})


@app.task(name="worker.tasks.execute_circuit", bind=True)
def execute_circuit(self, task_id: str, qc: str):
    logger.info("task claimed", extra={"task_id": task_id})
    _safe_write_status(task_id, status="PROCESSING")

    start = time.monotonic()
    try:
        circuit = qasm3.loads(qc)
        counts = AerSimulator().run(circuit, shots=NUM_SHOTS).result().get_counts()
    except (QiskitError, ValueError, SyntaxError) as exc:
        # Deterministic: bad QASM3 semantics or an unsupported gate, whether it
        # surfaces at parse time or during simulation. No retry, no DLQ (ADR-005).
        reason = _bounded(exc)
        logger.warning("deterministic failure", extra={"task_id": task_id, "reason": reason})
        _safe_write_status(task_id, status="FAILED", error=reason)
        return
    except SoftTimeLimitExceeded:
        # Deterministic: a circuit this slow will always be this slow. Retrying
        # just burns compute before it dies anyway. No retry, no DLQ (ADR-005).
        reason = "Execution exceeded soft time limit"
        logger.warning("deterministic failure", extra={"task_id": task_id, "reason": reason})
        _safe_write_status(task_id, status="FAILED", error=reason)
        return
    except Exception as exc:
        # Transient: anything else (resource exhaustion, host hiccups).
        # Bounded retry, then app-level DLQ on exhaustion (ADR-005).
        reason = _bounded(exc)
        if self.request.retries < TASK_MAX_RETRIES:
            logger.warning(
                "transient failure, retrying",
                extra={"task_id": task_id, "attempt": self.request.retries + 1, "reason": reason},
            )
            raise self.retry(exc=exc, countdown=2**self.request.retries, max_retries=TASK_MAX_RETRIES)
        logger.error("retry exhausted, pushed to dlq", extra={"task_id": task_id, "reason": reason})
        _safe_push_dlq(task_id, qc, reason)
        _safe_write_status(task_id, status="FAILED", error=f"Exhausted retries: {reason}")
        return

    duration_ms = round((time.monotonic() - start) * 1000, 2)
    logger.info("task completed", extra={"task_id": task_id, "duration_ms": duration_ms})
    _safe_write_status(task_id, status="COMPLETED", result=json.dumps(dict(counts)))

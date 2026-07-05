import json
import logging
import os
import uuid

from celery import Celery
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import state
from logging_config import setup_logging
from validation import ValidationError, validate_circuit

setup_logging()
logger = logging.getLogger(__name__)

MAX_QUBITS = int(os.environ.get("MAX_QUBITS", "24"))
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")

# Must match the `name=` on worker/tasks.py's @app.task exactly, or the
# message is enqueued but never consumed.
EXECUTE_CIRCUIT_TASK = "worker.tasks.execute_circuit"

celery_client = Celery("api", broker=CELERY_BROKER_URL)

app = FastAPI()


class TaskSubmission(BaseModel):
    qc: str


@app.exception_handler(RequestValidationError)
async def malformed_request_handler(request: Request, exc: RequestValidationError):
    # FastAPI raises this same exception type for path/query param validation
    # too (e.g. a malformed task_id) — only remap the body case to 400 per
    # README §3; let other validation errors fall through to the default 422.
    if any(error["loc"][:1] == ("body",) for error in exc.errors()):
        logger.warning("ingestion rejected", extra={"task_id": None, "reason": "malformed request body"})
        return JSONResponse(status_code=400, content={"message": "Malformed request: missing or invalid 'qc'."})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.post("/tasks", status_code=202)
def submit_task(submission: TaskSubmission):
    try:
        qubit_count = validate_circuit(submission.qc, MAX_QUBITS)
    except ValidationError as exc:
        logger.warning("ingestion rejected", extra={"task_id": None, "reason": str(exc)[:500]})
        return JSONResponse(status_code=400, content={"message": str(exc)})

    task_id = str(uuid.uuid4())
    logger.info(
        "task accepted",
        extra={
            "task_id": task_id,
            "qubit_count": qubit_count,
            "payload_bytes": len(submission.qc.encode("utf-8")),
        },
    )

    state.create_pending(task_id)
    celery_client.send_task(EXECUTE_CIRCUIT_TASK, args=[task_id, submission.qc])
    logger.info("task enqueued", extra={"task_id": task_id})

    return {"task_id": task_id, "message": "Task submitted successfully."}


@app.get("/tasks/{task_id}")
def get_task(task_id: uuid.UUID):
    task = state.get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Task not found."})

    internal_status = task.get("status")
    if internal_status in ("PENDING", "PROCESSING"):
        return {"status": "pending", "message": "Task is still in progress."}
    if internal_status == "COMPLETED":
        return {"status": "completed", "result": json.loads(task.get("result", "{}"))}
    if internal_status == "FAILED":
        return {"status": "failed", "message": f"Execution failed: {task.get('error', 'unknown error')}"}

    return JSONResponse(status_code=404, content={"status": "error", "message": "Task not found."})

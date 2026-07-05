import os
import time
import uuid

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

MAX_QUBITS = int(os.environ.get("MAX_QUBITS", "24"))
NUM_SHOTS = int(os.environ.get("NUM_SHOTS", "1024"))

BELL_STATE_QASM = """OPENQASM 3.0;
include "stdgates.inc";
qubit[2] q;
bit[2] c;
h q[0];
cx q[0], q[1];
c = measure q;
"""

INVALID_GATE_QASM = """OPENQASM 3.0;
include "stdgates.inc";
qubit[1] q;
bit[1] c;
not_a_real_gate q[0];
c = measure q;
"""


def _wait_for_status(task_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/tasks/{task_id}").json()
        if body["status"] != "pending":
            return body
        time.sleep(0.5)
    raise TimeoutError(f"Task {task_id} did not leave 'pending' within {timeout}s")


def test_submit_returns_202_and_task_id():
    response = client.post("/tasks", json={"qc": BELL_STATE_QASM})
    assert response.status_code == 202
    body = response.json()
    uuid.UUID(body["task_id"])


def test_bell_state_lifecycle_completes_with_valid_counts():
    task_id = client.post("/tasks", json={"qc": BELL_STATE_QASM}).json()["task_id"]

    body = _wait_for_status(task_id)

    assert body["status"] == "completed"
    assert sum(body["result"].values()) == NUM_SHOTS


def test_unknown_task_id_returns_not_found():
    response = client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "Task not found."}


def test_oversized_circuit_rejected_with_400():
    qasm = f'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[{MAX_QUBITS + 1}] q;\n'
    response = client.post("/tasks", json={"qc": qasm})
    assert response.status_code == 400


def test_invalid_gate_lands_in_failed_without_retry():
    task_id = client.post("/tasks", json={"qc": INVALID_GATE_QASM}).json()["task_id"]

    body = _wait_for_status(task_id, timeout=20)

    assert body["status"] == "failed"
    assert "message" in body


def test_missing_qc_payload_returns_client_error():
    response = client.post("/tasks", json={"wrong_key": "irrelevant"})
    assert response.status_code in (400, 422)


def test_unparsable_qasm_rejected_with_400():
    response = client.post("/tasks", json={"qc": "this is definitely not quantum code"})
    assert response.status_code == 400


def test_malformed_task_id_returns_422():
    response = client.get("/tasks/not-a-valid-uuid")
    assert response.status_code == 422

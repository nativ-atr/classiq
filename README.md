# Quantum Circuit Execution API

An asynchronous service for executing QASM3 quantum circuits. Clients submit a
circuit and receive a task ID immediately; execution happens on a decoupled
worker pool; results are retrieved by polling. The system is designed around one
non-negotiable property: **no submitted task is silently lost**.

Full design rationale — with the trade-off each decision accepts — lives in
[`docs/DECISIONS.md`](docs/DECISIONS.md). A one-line summary of each is in
[§6 Design decisions](#6-design-decisions) below.

---

## 1. Architecture at a glance

```
                    ┌─────────────────────────────────────────────┐
   POST /tasks      │              API gateway (FastAPI)           │
   GET  /tasks/{id} │  - validates payload + qubit cap (openqasm3) │
 ───────────────►   │  - writes PENDING to state store             │
   client           │  - enqueues job, returns 202 + task_id       │
                    └───────────────┬──────────────────┬───────────┘
                                    │ ① enqueue         │ ② state read/write
                                    ▼                   ▼
                          ┌──────────────────┐   ┌──────────────────┐
                          │  Redis (broker)  │   │ Redis (state      │
                          │  durable queue   │   │ store: task:{id}) │
                          └────────┬─────────┘   └────────▲─────────┘
                                   │ ③ deliver            │ ④ write result
                                   ▼                      │
                          ┌────────────────────────────────────────┐
                          │        Worker pool (Celery + Aer)       │
                          │  PROCESSING → COMPLETED / FAILED / DLQ   │
                          └────────────────────────────────────────┘
```

Three decoupled containers — `api`, `worker`, `redis` — orchestrated by
`docker-compose`. The **queue** (transient work) and the **state store**
(durable source of truth for `GET`) are deliberately kept as separate concerns,
even though both currently live in Redis (ADR-001, ADR-004).

### Tech stack

| Concern              | Choice                    | Rationale        |
|----------------------|---------------------------|------------------|
| API framework        | FastAPI (ASGI)            | ADR-001          |
| Ingestion validation | `openqasm3` AST parser    | ADR-002          |
| Task queue / broker  | Celery on Redis           | ADR-003–005      |
| State store          | Redis hash (`task:{id}`)  | ADR-004          |
| Simulation           | `qiskit` + `qiskit-aer`   | assignment req.  |
| Orchestration        | Docker Compose            | assignment req.  |

---

## 2. Running locally

### Prerequisites
- Docker and Docker Compose v2 (the `docker compose` plugin, bundled with
  current Docker Desktop / Engine installs). If you only have the legacy
  standalone v1 binary, substitute `docker-compose` (hyphenated) for
  `docker compose` in the commands below.
- No local Python needed — everything runs in containers.

### Start

```bash
docker compose up --build
```

This builds and starts `redis`, `api` (on `http://localhost:8000`), and one or
more `worker` containers. The API is ready when you see the Uvicorn startup log.

### Configuration (environment variables)

| Variable             | Default | Meaning                                            |
|----------------------|---------|----------------------------------------------------|
| `MAX_QUBITS`         | `24`    | Circuits above this are rejected at ingestion (ADR-002) |
| `NUM_SHOTS`          | `1024`  | Shots per simulation                               |
| `CELERY_CONCURRENCY` | `2`     | Worker processes per container; override to core count (ADR-003) |
| `MAX_TASKS_PER_CHILD`| `10`    | Recycle a worker process after N tasks (memory hygiene) |
| `TASK_MAX_RETRIES`   | `3`     | Transient-failure retries before DLQ (ADR-005)     |
| `RESULT_TTL`         | *unset* | No TTL by default; results persist (ADR-006)       |
| `LOG_LEVEL`          | `INFO`  | Log verbosity: `DEBUG`/`INFO`/`WARNING`/`ERROR` (ADR-009) |
| `LOG_FORMAT`         | `json`  | `json` (structured, default) or `text` (human-readable, local dev) (ADR-009) |

> The Celery flags are **env-driven, not hardcoded**. The worker command uses
> `--concurrency=${CELERY_CONCURRENCY:-2} --max-tasks-per-child=${MAX_TASKS_PER_CHILD:-10}`,
> so the demo is deterministic (default `2`) but a reviewer on an 8-core host can
> set `CELERY_CONCURRENCY=8` without editing the compose file. A bare literal
> would contradict the "pin concurrency to core count" principle in ADR-003.

### Run the tests

```bash
docker compose run --rm api pytest -v
```

Coverage is summarized in [§7 Testing](#7-testing).

---

## 3. API contract

### `POST /tasks`

Submit a circuit for asynchronous execution.

**Request**
```json
{ "qc": "<serialized_quantum_circuit_in_qasm3>" }
```

**Response — `202 Accepted`**
```json
{ "task_id": "a1b2c3d4-...", "message": "Task submitted successfully." }
```

**Error responses**
- `400 Bad Request` — malformed JSON, missing `qc`, unparseable QASM3, or a
  circuit exceeding `MAX_QUBITS` (see ADR-002). Body includes a reason.

### `GET /tasks/{id}`

Retrieve task status/result. Reads the state store only — never the queue.
The `completed`/`pending`/`failed` outcomes below are all returned as
`200 OK`; the `status` field in the body, not the HTTP status code, is what
distinguishes them. The two exceptions are called out inline and in
**Error responses** below.

**Completed**
```json
{ "status": "completed", "result": { "0": 512, "1": 512 } }
```

**Still processing** (covers both internal `PENDING` and `PROCESSING`)
```json
{ "status": "pending", "message": "Task is still in progress." }
```

**Not found** — `404 Not Found` (ID never existed, or — with a production TTL — expired)
```json
{ "status": "error", "message": "Task not found." }
```

**Failed** *(extension to the base spec — see note)*
```json
{ "status": "failed", "message": "Execution failed: <reason>" }
```

> **Note on the `failed` status.** The assignment defines only `completed`,
> `pending`, and `error` (not found). It does not specify a response for a task
> that is *found* but failed during execution (e.g. invalid gates). Returning a
> generic "not found" for a task that demonstrably existed would be misleading,
> so this implementation adds an explicit `failed` status. This is a documented
> deviation; the exact envelope would be confirmed with the reviewer. See ADR-005.

**Error responses**
- `422 Unprocessable Entity` — `{id}` is not a well-formed UUID. FastAPI
  validates the path parameter before the handler runs, so the response body
  here is FastAPI's default validation-error shape, not the `{"status":
  "error", ...}` envelope used by every other response on this endpoint —
  the one intentional exception to that convention.

### Example

```bash
# Submit
TASK=$(curl -s -X POST localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"qc":"OPENQASM 3.0; include \"stdgates.inc\"; qubit[2] q; bit[2] c; h q[0]; cx q[0], q[1]; c = measure q;"}' \
  | jq -r .task_id)

# Poll
curl -s localhost:8000/tasks/$TASK | jq
```

### Postman collection

A ready-made collection is included at
[`postman_collection.json`](postman_collection.json) covering Submit Task, GET
Tasks, Unknown Task, OOM Task, and Invalid Task — import it into Postman as an
alternative to the curl examples above.

---

## 4. Task state machine

Internal states are richer than the wire format. `PROCESSING` exists for
observability and to make the claim atomic, but is reported publicly as
`pending`.

| Internal    | Set by            | Public `GET` response        |
|-------------|-------------------|------------------------------|
| `PENDING`   | gateway, at submit| `pending`                    |
| `PROCESSING`| worker, on claim  | `pending`                    |
| `COMPLETED` | worker, on success| `completed` + result         |
| `FAILED`    | worker, terminal  | `failed` + reason            |
| *(absent)*  | —                 | `error` / "Task not found."  |

The state store is a Redis hash per task: `status`, `result` (JSON),
`error`, `created_at`, `updated_at`. The **write ordering at submission is
`PENDING` → enqueue → return `202`** so that a task is always visible to `GET`
before the client learns its ID. The residual crash window this leaves is
documented in [§5 Known limitations](#5-known-limitations).

---

## 5. Known limitations (called out, not hidden)

- **Submit-time crash window.** State ordering is `PENDING` → enqueue → `202`. A
  crash *between* the `PENDING` write and a successful enqueue leaves an orphaned
  `PENDING` task that never runs. The production fix is a **transactional outbox**
  (or a reconciliation sweeper that re-enqueues `PENDING` tasks with no live
  broker message after a timeout). Acknowledged, not solved, within the 48-hour
  scope.
- **Redis visibility timeout is load-bearing** for redelivery (ADR-004). Mis-tuning
  it below max task runtime causes double execution of long tasks.
- **App-level DLQ** rather than broker-native dead-lettering (ADR-005).
- **Flat qubit cap** over-rejects Clifford circuits (ADR-002).

None of these compromise the core guarantee on the happy and crash paths; they
are the edges a production hardening pass would close, and are documented so the
reviewer can see the boundary of what was built versus what was designed.

---

## 6. Design decisions

Each row summarizes one decision and the trade-off it accepts. The full record —
context, rationale, and consequences — is in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

| ADR | Decision | Trade-off accepted |
|-----|----------|--------------------|
| [001](docs/DECISIONS.md#adr-001) | FastAPI as the ingestion layer, not a dedicated gateway (Kong); decoupled `api`/`worker`/`redis` | Forgoes native DDoS / circuit-breaking / auth — correct at two-endpoint scale, revisit at a real edge |
| [002](docs/DECISIONS.md#adr-002) | Reject circuits over `MAX_QUBITS` at ingestion via the lightweight `openqasm3` parser, before enqueue | Flat qubit cap over-rejects Clifford circuits; `qiskit` deliberately kept out of the API image |
| [003](docs/DECISIONS.md#adr-003) | Prefork (process) worker pool pinned to core count; sync `POST` handler so `.delay()` runs in the threadpool | Explicitly avoids `BackgroundTasks`, which would return an ID before the task is durably queued |
| [004](docs/DECISIONS.md#adr-004) | `acks_late` + side-effect-free execution + last-write-wins hand-managed state hash | On Redis, redelivery depends on a correctly-tuned `visibility_timeout`, not a true ack — RabbitMQ would make it native |
| [005](docs/DECISIONS.md#adr-005) | Split failures: deterministic → `FAILED` now; transient → bounded retries → app-level DLQ (`dlq:tasks`) | DLQ is app-level emulation and does not catch hard OOM kills — those are stopped at ingestion (ADR-002) |
| [006](docs/DECISIONS.md#adr-006) | Results persist without a TTL so evaluation timing is unconstrained | Unbounded growth in production; real fix is an SLA-sized TTL or migration off Redis to a durable store |
| [007](docs/DECISIONS.md#adr-007) | Queue is unbounded; admission control out of scope | Under sustained overload, memory/latency climb; production fix is a queue-depth check + `HTTP 429` shedding |
| [008](docs/DECISIONS.md#adr-008) | Routing kept inline in `main.py` | Extract into `APIRouter` modules if the API surface grows past two endpoints |
| [009](docs/DECISIONS.md#adr-009) | Structured JSON logs correlated by `task_id`; severity mapped to failure class | Logs only — no metrics/tracing/aggregation yet; raw QASM payload never logged |

---

## 7. Testing

Integration tests (`pytest`, run against the composed stack) cover:

1. **Submission** returns `202` and a well-formed `task_id`.
2. **Lifecycle** — a submitted Bell-state circuit transitions from `pending` to
   `completed`, and the result is a valid counts dict summing to `NUM_SHOTS`.
3. **Retrieval of unknown ID** returns the `error` / "Task not found." envelope.
4. **Oversized circuit** (> `MAX_QUBITS`) is rejected at ingestion with `400` and
   never enqueued.
5. **Deterministic failure** (invalid gate) lands in `FAILED` with a reason and
   is not retried.
6. **Missing `qc` field** in the request body is rejected with a client error
   (`400`/`422`) before validation or enqueue.
7. **Unparseable QASM3** (not valid QASM at all, not just semantically invalid)
   is rejected at ingestion with `400`, not a `500`.
8. **Malformed task ID** (not a well-formed UUID) on `GET /tasks/{id}` is
   rejected with `422` by FastAPI's own path-parameter validation.

```bash
docker compose run --rm api pytest -v
```

---

## 8. Project structure

```
.
├── docker-compose.yml
├── README.md
├── postman_collection.json  # ready-made Postman collection (see §3)
├── docs/
│   └── DECISIONS.md       # full architectural decision records (ADR-001..009)
├── api/
│   ├── Dockerfile
│   ├── main.py            # FastAPI app: routes, validation, enqueue
│   ├── validation.py      # openqasm3 parse + qubit-cap check (ADR-002)
│   ├── state.py           # Redis state-store read/write helpers
│   └── logging_config.py  # structured JSON/text logging setup (ADR-009)
├── worker/
│   ├── Dockerfile
│   ├── celery_app.py      # Celery config: acks_late, prefork, timeouts (ADR-003/004)
│   ├── tasks.py           # execute_circuit + failure classification (ADR-005)
│   └── logging_config.py  # structured JSON/text logging setup (ADR-009)
└── tests/
    └── test_integration.py
```

---

## 9. Summary of the core guarantees

- **No task lost on the happy or crash path** — durable broker + `acks_late` +
  correctly-tuned visibility timeout, with a documented submit-time edge.
- **Worker pool protected from OOM** — ingestion qubit cap before enqueue.
- **Failures are honest and bounded** — deterministic → `FAILED` immediately;
  transient → bounded retries → DLQ.
- **Decisions are documented with their trade-offs** in
  [`docs/DECISIONS.md`](docs/DECISIONS.md), including where the Redis broker
  choice weakens a guarantee and what would be swapped to strengthen it.
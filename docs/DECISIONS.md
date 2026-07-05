# Architectural Decision Records (ADRs)

The design decisions behind the [Quantum Circuit Execution API](../README.md). Every record names the trade-off accepted and the specific architectural rationale behind it.

---

<a id="adr-001"></a>
### ADR-001: FastAPI over Dedicated API Gateway
* **Context:** Dedicated gateways (e.g., Kong) provide out-of-the-box routing and throttling but introduce heavyweight infrastructure and configuration overhead.
* **Decision:** Use FastAPI as the standalone ingestion layer. Pydantic handles validation, and rate limiting uses `slowapi` backed by our existing Redis instance.
* **Consequences:** We forgo native DDoS protection and managed auth to keep the 48-hour scope tight and system topology simple. 

<a id="adr-002"></a>
### ADR-002: Gateway Validation & Qubit Cap
* **Context:** `AerSimulator` statevector memory usage scales exponentially ($2^n$). Malicious or oversized payloads will cause OOM crashes in the worker pool.
* **Decision:** Parse the QASM3 string at the API Gateway using the lightweight `openqasm3` AST parser, which keeps the massive `qiskit` dependency out of the API image entirely. Reject >24 qubits instantly with an `HTTP 400`.
* **Consequences:** Protects workers from memory exhaustion without image bloat. *Caveat:* This is a conservative heuristic, as Clifford circuits run efficiently in polynomial memory.

<a id="adr-003"></a>
### ADR-003: Threadpool Enqueueing (Avoiding `BackgroundTasks`)
* **Context:** `Celery.delay()` uses a synchronous Redis client. Running it inside an `async def` route blocks the ASGI event loop under load.
* **Decision:** Define `POST /tasks` as a standard `def` (sync) route, forcing FastAPI to safely execute the Redis enqueue within its internal threadpool. 
* **Consequences:** Prevents event loop blocking. Crucially, we avoid using FastAPI's `BackgroundTasks` for enqueueing, which executes *after* the HTTP response and violates the "zero task loss" guarantee if the pod dies mid-flight.

<a id="adr-004"></a>
### ADR-004: Delivery Semantics & Task Integrity
* **Context:** Message brokers require strict acknowledgement semantics to guarantee zero task loss in the event of a worker crash.
* **Decision:** Rely on Celery's `acks_late=True` alongside a last-write-wins Redis state store.
* **Consequences:** If a worker crashes, the task is safely redelivered. Because quantum execution is pure compute (side-effect-free) and the state write is last-write-wins, at-least-once redelivery is perfectly safe and converges to one valid result.

<a id="adr-005"></a>
### ADR-005: Process-Based Concurrency & Timeouts
* **Context:** Quantum simulation is heavily CPU-bound and does not release the Python GIL cleanly.
* **Decision:** Celery workers use process-based concurrency (`prefork`) pegged to core count. The broker `visibility_timeout` is explicitly set higher than the `task_time_limit`.
* **Consequences:** Prevents context-thrashing. Setting visibility higher than the time limit ensures a task is always killed by its own limit before the broker considers it lost and redelivers it, preventing duplicate execution loops.

<a id="adr-006"></a>
### ADR-006: Failure Classification & Dead Letter Queue (DLQ)
* **Context:** We must distinguish between transient infrastructure blips and deterministically fatal tasks (poison messages) to prevent infinite crash loops.
* **Decision:** *Transient Errors* (e.g., timeouts) are retried until the limit is reached, then pushed to a DLQ. *Deterministic Errors* (e.g., invalid QASM gates) are caught immediately, written as `FAILED`, and NOT retried.
* **Consequences:** Prevents poison payloads from burning through the worker pool. *Note: The DLQ only catches catchable exceptions — a hard OOM kill raises none, so that fatal case must be stopped at ingestion (ADR-002), not here.*

<a id="adr-007"></a>
### ADR-007: Separation of State Store and Task Queue
* **Context:** Message queues hold transient work; APIs need durable state to query. Beginners often conflate the two.
* **Decision:** Redis acts as the transient message broker *and* a separate, durable State Store (using Redis Hashes per `task_id`). Results are stored indefinitely for the scope of this assignment.
* **Consequences:** Satisfies the `GET /tasks/{id}` requirement reliably. *Trade-off:* In a real production environment, unbounded Redis storage is an anti-pattern. We would implement a TTL eviction policy or a background sweeper to migrate historical results to cold storage.

<a id="adr-008"></a>
### ADR-008: REST Hygiene & Inline Routing
* **Context:** Returning an `HTTP 200` for missing resources breaks monitoring tools, while over-separating routers creates cognitive overhead for tiny APIs.
* **Decision:** Keep routing inline in `main.py`. `GET /tasks/{id}` returns `HTTP 404` *only* if the task ID does not exist, preserving the exact spec envelope via `JSONResponse` to bypass FastAPI's default wrapper. Existing tasks (Pending, Completed, or Failed) all return `HTTP 200`. 
* **Consequences:** Clean separation of HTTP transport state and Business lifecycle state, while maintaining a highly readable single-file API scope.

<a id="adr-009"></a>
### ADR-009: Structured JSON Logging & Task Correlation
* **Context:** Debugging an asynchronous, multi-container system requires centralized, correlated observability.
* **Decision:** Implemented structured, single-line JSON logging in both containers. Every log line is injected with the `task_id` via the `extra` kwarg. We explicitly override Celery's root logger hijacking (`worker_hijack_root_logger=False`).
* **Consequences:** A developer can grep a single `task_id` and trace its complete lifecycle across both containers. Raw QASM3 payloads are excluded from logs to prevent bloat and data-privacy violations.
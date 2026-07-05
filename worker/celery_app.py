import logging
import os

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging_signal

import logging_config

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")

# task_time_limit is the backstop that kills a stuck task before Redis's
# visibility_timeout elapses and redelivers it — if that ordering were
# reversed, a slow task could be redelivered and run twice concurrently
# (ADR-004). Keep TASK_TIME_LIMIT strictly less than BROKER_VISIBILITY_TIMEOUT.
TASK_SOFT_TIME_LIMIT = 100
TASK_TIME_LIMIT = 120
BROKER_VISIBILITY_TIMEOUT = 300

app = Celery("worker", broker=CELERY_BROKER_URL, include=["tasks"])
app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT},
    task_time_limit=TASK_TIME_LIMIT,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT,
    worker_hijack_root_logger=False,
)


@celery_setup_logging_signal.connect
def _configure_logging(**_kwargs):
    # Connecting to this signal makes Celery skip its own logging setup
    # entirely, so our JSON/text formatter survives worker startup.
    logging_config.setup_logging()
    # Celery's own task-received/succeeded INFO logs embed the raw task args
    # (i.e. the QASM3 payload) via their default repr. Silence these two
    # loggers to WARNING so no log line ever contains the circuit text.
    logging.getLogger("celery.worker.strategy").setLevel(logging.WARNING)
    logging.getLogger("celery.app.trace").setLevel(logging.WARNING)

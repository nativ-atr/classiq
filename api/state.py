import os
from datetime import datetime, timezone
from typing import Optional

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_pending(task_id: str) -> None:
    now = _now()
    _client.hset(
        f"task:{task_id}",
        mapping={"status": "PENDING", "created_at": now, "updated_at": now},
    )


def get_task(task_id: str) -> Optional[dict]:
    data = _client.hgetall(f"task:{task_id}")
    return data or None

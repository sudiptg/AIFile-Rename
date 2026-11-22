"""Task model, global task registry and the concurrency limiter (queue)."""
import asyncio
import itertools
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import config


class TaskCancelled(Exception):
    """Raised when the user cancels a task."""


class UserError(Exception):
    """An expected error whose message is safe to show to the user."""


_sequence = itertools.count(1)
_batch_sequence = itertools.count(1)


@dataclass
class Task:
    user_id: int
    chat_id: int
    name: str
    source: str  # "telegram" | "url"
    client: object = None
    status_msg: object = None
    state: str = "queued"  # queued | downloading | processing | uploading
    seq: int = field(default_factory=lambda: next(_sequence))
    created: float = field(default_factory=time.time)
    started: float = 0.0
    last_edit: float = 0.0
    cancelled: bool = False
    proc: object = None  # running subprocess (aria2c / ffmpeg), if any
    notes: list = field(default_factory=list)  # warnings shown to the user after upload
    # ---- added in v3.1 ----
    settings_uid: int = 0          # whose settings are used (differs from user_id for group members)
    batch: object = None           # Batch this task belongs to (None = single file with its own panel)
    chips: str = ""                # e.g. "S01E05 - 1080p" shown on the progress panel
    progress: dict = field(default_factory=dict)  # {"action", "current", "total", "speed"}
    result: str = ""               # "", "done", "failed", "cancelled" (used by the batch summary)
    reason: str = ""               # failure reason (batch summary)

    @property
    def task_id(self) -> str:
        return str(self.seq)

    def cancel(self) -> None:
        self.cancelled = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled()


@dataclass
class Batch:
    """Several files sent together: ONE panel instead of one message per file."""
    chat_id: int
    user_id: int
    total: int
    client: object = None
    panel: object = None
    seq: int = field(default_factory=lambda: next(_batch_sequence))
    tasks: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    last_edit: float = 0.0
    finished: bool = False

    @property
    def batch_id(self) -> str:
        return str(self.seq)

    def count(self, result: str) -> int:
        return sum(1 for t in self.tasks if t.result == result)

    def running(self) -> list:
        return [t for t in self.tasks if t.state in ("downloading", "processing", "uploading") and not t.result]

    def waiting(self) -> int:
        return sum(1 for t in self.tasks if t.state == "queued" and not t.result)

    def cancel(self) -> None:
        for task in self.tasks:
            task.cancel()


BATCHES: dict = {}  # batch_id -> Batch (only while it has unfinished work)
TASKS: dict = {}  # task_id -> Task (queued + running)


def queue_position(task: Task) -> int:
    waiting = [t for t in TASKS.values() if t.state == "queued" and t.seq < task.seq]
    return len(waiting) + 1


class Limiter:
    """Runs at most ``limit`` tasks at once. Waiting tasks start in STRICT order of their sequence number
    (= the sorted episode order of a batch), not in whatever order their coroutines happened to wake up."""

    def __init__(self, limit: int = 1) -> None:
        self.limit = max(1, int(limit))
        self.active = 0
        self._cond = None
        self._waiting: list = []

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def set_limit(self, value: int) -> None:
        cond = self._condition()
        async with cond:
            self.limit = max(1, int(value))
            cond.notify_all()

    async def wake(self) -> None:
        cond = self._condition()
        async with cond:
            cond.notify_all()

    def _is_next(self, task: Task) -> bool:
        return all(task.seq <= other.seq for other in self._waiting)

    @asynccontextmanager
    async def slot(self, task: Task):
        cond = self._condition()
        async with cond:
            self._waiting.append(task)
            try:
                await cond.wait_for(lambda: task.cancelled or (self.active < self.limit and self._is_next(task)))
            finally:
                self._waiting.remove(task)
                cond.notify_all()  # the next task in line may now be the lowest one
            task.check()
            self.active += 1
        try:
            yield
        finally:
            async with cond:
                self.active -= 1
                cond.notify_all()


limiter = Limiter(config.MAX_CONCURRENT_TASKS)

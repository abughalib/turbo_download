"""
Chunk scheduling and resume system for TurboGet.

Chunk lifecycle
---------------
PENDING → IN_PROGRESS → DONE
                      ↘ FAILED (if retries exhausted) → put back as PENDING
                                                          (up to MAX_RETRIES)

Work-stealing
-------------
All chunks share a single asyncio.Queue. Workers pull chunks from it;
faster interfaces naturally pull more chunks. If a chunk fails, it's
put back in the queue for any other worker to retry — this is the
work-stealing mechanism.

Resume
------
A JSON sidecar (.filename.turboget) tracks the status of every chunk.
On restart, pending/in-progress chunks are re-queued while done chunks
are skipped — resuming from where the session left off.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


MAX_RETRIES = 3
BLOCK_SIZE = 256 * 1024  # 256 KB streaming block for progress granularity


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------

class ChunkStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Chunk:
    id: int
    start: int        # inclusive byte offset
    end: int          # inclusive byte offset
    status: ChunkStatus = ChunkStatus.PENDING
    retries: int = 0
    assigned_to: Optional[str] = None  # interface name
    bytes_done: int = 0               # bytes written so far (for partial resume)

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def resume_start(self) -> int:
        """Actual start byte for next request (after partial progress)."""
        return self.start + self.bytes_done


# ---------------------------------------------------------------------------
# Chunk creation
# ---------------------------------------------------------------------------

def calculate_chunk_count(file_size: int, n_interfaces: int,
                           chunks_per_iface: int) -> int:
    """
    Calculate a sensible total chunk count.

    Rules:
    - Target: chunks_per_iface × n_interfaces
    - Minimum chunk size: 2 MB  (avoid silly tiny chunks)
    - Maximum chunks: 128
    """
    desired = chunks_per_iface * max(1, n_interfaces)
    min_chunk_bytes = 2 * 1024 * 1024  # 2 MB
    max_by_size = max(1, file_size // min_chunk_bytes)
    return min(desired, max_by_size, 128)


def create_chunks(file_size: int, n_chunks: int) -> list[Chunk]:
    """Split file_size bytes into n_chunks roughly-equal byte ranges."""
    if n_chunks <= 0:
        n_chunks = 1

    chunk_size = file_size // n_chunks
    chunks: list[Chunk] = []

    for i in range(n_chunks):
        start = i * chunk_size
        end = (start + chunk_size - 1) if i < n_chunks - 1 else file_size - 1
        chunks.append(Chunk(id=i, start=start, end=end))

    return chunks


# ---------------------------------------------------------------------------
# Work-stealing queue
# ---------------------------------------------------------------------------

class WorkQueue:
    """
    Async work-stealing queue backed by asyncio.Queue.

    Semantics:
    - get()       → returns next Chunk or None if queue is currently empty
    - put_back()  → return a failed chunk for retry (increments retry counter)
    - mark_done() → mark a chunk as successfully downloaded
    - is_complete() → True when every chunk is either DONE or permanently FAILED
    """

    def __init__(self, chunks: list[Chunk]):
        self._chunks = {c.id: c for c in chunks}
        self._queue: asyncio.Queue[Chunk] = asyncio.Queue()
        self._done: set[int] = set()
        self._failed: set[int] = set()
        self._lock = asyncio.Lock()

        for chunk in chunks:
            if chunk.status == ChunkStatus.DONE:
                self._done.add(chunk.id)
            else:
                chunk.status = ChunkStatus.PENDING
                self._queue.put_nowait(chunk)

    async def get(self) -> Optional[Chunk]:
        """Pull the next pending chunk (non-blocking). Returns None if empty."""
        try:
            chunk = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        async with self._lock:
            chunk.status = ChunkStatus.IN_PROGRESS
        return chunk

    async def put_back(self, chunk: Chunk) -> None:
        """Return a failed chunk to the queue for retry, or mark as FAILED."""
        async with self._lock:
            chunk.retries += 1
            chunk.bytes_done = 0
            chunk.assigned_to = None
            if chunk.retries >= MAX_RETRIES:
                chunk.status = ChunkStatus.FAILED
                self._failed.add(chunk.id)
            else:
                chunk.status = ChunkStatus.PENDING
                await self._queue.put(chunk)

    async def mark_done(self, chunk: Chunk) -> None:
        """Mark a chunk as successfully downloaded."""
        async with self._lock:
            chunk.status = ChunkStatus.DONE
            self._done.add(chunk.id)

    def is_complete(self) -> bool:
        """All chunks are settled (done or permanently failed)."""
        return len(self._done) + len(self._failed) >= len(self._chunks)

    def all_succeeded(self) -> bool:
        return len(self._done) >= len(self._chunks)

    def has_pending(self) -> bool:
        return not self._queue.empty()

    @property
    def done_count(self) -> int:
        return len(self._done)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    @property
    def total_count(self) -> int:
        return len(self._chunks)

    def all_chunks(self) -> list[Chunk]:
        return list(self._chunks.values())


# ---------------------------------------------------------------------------
# Resume system
# ---------------------------------------------------------------------------

def get_resume_file(output: Path) -> Path:
    return output.parent / f".{output.name}.turboget"


def save_resume_state(chunks: list[Chunk], url: str,
                      file_size: int, output: Path) -> None:
    """Persist chunk state to a JSON sidecar file."""
    state = {
        "url": url,
        "file_size": file_size,
        "output": str(output.resolve()),
        "chunks": [
            {
                "id": c.id,
                "start": c.start,
                "end": c.end,
                "status": c.status.value,
                "bytes_done": c.bytes_done,
                "retries": c.retries,
            }
            for c in chunks
        ],
    }
    resume_file = get_resume_file(output)
    try:
        with open(resume_file, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass  # Non-fatal — resume just won't work next time


def load_resume_state(url: str, output: Path) -> Optional[list[Chunk]]:
    """
    Load chunk state from sidecar if URL matches.
    Returns None if no valid resume data exists.
    """
    resume_file = get_resume_file(output)
    if not resume_file.exists():
        return None

    try:
        with open(resume_file) as f:
            state = json.load(f)

        if state.get("url") != url:
            return None  # Different URL — start fresh

        chunks: list[Chunk] = []
        for c in state["chunks"]:
            status = ChunkStatus(c["status"])
            # Treat in-progress as pending (session was interrupted)
            if status == ChunkStatus.IN_PROGRESS:
                status = ChunkStatus.PENDING
                bytes_done = 0
            else:
                bytes_done = c.get("bytes_done", 0)

            chunks.append(Chunk(
                id=c["id"],
                start=c["start"],
                end=c["end"],
                status=status,
                bytes_done=bytes_done,
                retries=c.get("retries", 0),
            ))

        return chunks
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def cleanup_resume_file(output: Path) -> None:
    """Delete sidecar after a successful download."""
    rf = get_resume_file(output)
    if rf.exists():
        try:
            rf.unlink()
        except OSError:
            pass

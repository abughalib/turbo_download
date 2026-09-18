"""
Pre-allocated file writer for TurboGet.

Chunks arrive out of order, so we pre-allocate the full file on disk and
use seek+write to place each chunk at its correct byte offset.

Pre-allocation (via posix_fallocate) avoids disk fragmentation and ensures
the file is contiguous on disk — important for large video/ISO downloads.

Thread safety: asyncio.Lock serialises all writes since we share one fd.
The lock is async so it doesn't block the event loop during seek+write.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


class FileWriter:
    """
    Manages a single pre-allocated file for multi-offset writes.

    Usage
    -----
        writer = FileWriter(Path("output.iso"), 4_000_000_000)
        await writer.open()
        await writer.write(offset, data)
        await writer.close()
    """

    def __init__(self, path: Path, file_size: int) -> None:
        self._path = path
        self._file_size = file_size
        self._file = None
        self._lock = asyncio.Lock()
        self._bytes_written: int = 0

    async def open(self) -> None:
        """Pre-allocate file space and open for random writes."""
        await asyncio.to_thread(self._preallocate)
        self._file = await asyncio.to_thread(open, self._path, "r+b")

    def _preallocate(self) -> None:
        """
        Allocate disk space upfront.

        Tries posix_fallocate first (contiguous, no holes).
        Falls back to a sparse file (seek-to-end + write 1 byte).
        """
        with open(self._path, "wb") as f:
            if self._file_size <= 0:
                return
            try:
                os.posix_fallocate(f.fileno(), 0, self._file_size)
            except (AttributeError, OSError):
                # Sparse fallback — works on any POSIX filesystem
                f.seek(self._file_size - 1)
                f.write(b"\x00")

    async def write(self, offset: int, data: bytes) -> None:
        """Write *data* at *offset* bytes from the start of the file."""
        async with self._lock:
            await asyncio.to_thread(self._sync_write, offset, data)
            self._bytes_written += len(data)

    def _sync_write(self, offset: int, data: bytes) -> None:
        self._file.seek(offset)
        self._file.write(data)

    async def close(self) -> None:
        """Flush and close the file."""
        if self._file is not None:
            await asyncio.to_thread(self._file.flush)
            await asyncio.to_thread(self._file.close)
            self._file = None

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

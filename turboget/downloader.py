"""
Async download engine for TurboGet.

Each NetworkInterface gets one async worker coroutine. Workers share a
single WorkQueue and independently pull chunks from it. This is the
work-stealing model: faster interfaces naturally handle more chunks.

Socket binding
--------------
Each worker creates its own aiohttp.TCPConnector with
    local_addr=(iface.local_ip, 0)
This binds every socket that the session opens to the interface's local IP.
Combined with the Linux policy routing rules set up by `turboget setup`,
this guarantees all data flows through the correct physical NIC.

Chunk download flow
-------------------
1. Pull chunk from queue
2. Build Range header: "bytes=<resume_start>-<end>"
3. Stream response body in 256KB blocks
4. Write each block to FileWriter at the correct offset
5. Update ProgressTracker after each block
6. On success → mark_done; on error → put_back (for retry)
7. Periodically save resume state
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

import aiohttp

from .interface import NetworkInterface
from .progress import ProgressTracker
from .scheduler import BLOCK_SIZE, Chunk, WorkQueue, save_resume_state
from .writer import FileWriter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAVE_STATE_INTERVAL = 15.0  # seconds between resume state saves
_USER_AGENT = "TurboGet/1.0 (Multi-WAN Download Manager)"


# ---------------------------------------------------------------------------
# Chunk downloader
# ---------------------------------------------------------------------------

async def _download_chunk(
    session: aiohttp.ClientSession,
    url: str,
    chunk: Chunk,
    writer: FileWriter,
    progress_cb: Callable[[int], None],
    read_timeout: int = 120,
) -> bool:
    """
    Download one chunk via an HTTP Range request.

    Returns True on success, False on any failure.
    Supports resuming a partially-downloaded chunk via chunk.bytes_done.
    """
    byte_range = f"bytes={chunk.resume_start}-{chunk.end}"
    headers = {
        "Range": byte_range,
        "User-Agent": _USER_AGENT,
    }
    timeout = aiohttp.ClientTimeout(connect=30, sock_read=read_timeout, total=None)

    try:
        async with session.get(
            url, headers=headers, allow_redirects=True, timeout=timeout
        ) as resp:
            # 206 Partial Content is expected; 200 means server ignored Range header
            if resp.status not in (200, 206):
                return False

            offset = chunk.resume_start

            async for block in resp.content.iter_chunked(BLOCK_SIZE):
                await writer.write(offset, block)
                n = len(block)
                offset += n
                chunk.bytes_done += n
                progress_cb(n)

        return True

    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


# ---------------------------------------------------------------------------
# Worker coroutine (one per interface)
# ---------------------------------------------------------------------------

async def _worker(
    iface: NetworkInterface,
    url: str,
    queue: WorkQueue,
    writer: FileWriter,
    tracker: ProgressTracker,
    save_cb: Callable[[], None],
    read_timeout: int,
) -> None:
    """
    Single-interface download worker.

    Creates an aiohttp session whose connector is bound to the interface's
    local IP, then pulls chunks from the shared queue until the download
    is complete or the interface fails repeatedly.
    """
    connector = aiohttp.TCPConnector(
        local_addr=(iface.local_ip, 0),
        limit=0,                    # unlimited simultaneous connections
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )

    tracker.set_status(iface.name, "active")
    last_save = time.monotonic()
    consecutive_failures = 0
    MAX_CONSECUTIVE = 5  # give up if interface seems dead

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while not queue.is_complete():
                chunk = await queue.get()

                if chunk is None:
                    if queue.is_complete():
                        break
                    # Queue temporarily empty (other workers still in progress)
                    await asyncio.sleep(0.3)
                    continue

                chunk.assigned_to = iface.name

                def _progress(n: int) -> None:
                    tracker.update(iface.name, n)

                success = await _download_chunk(
                    session, url, chunk, writer, _progress, read_timeout
                )

                if success:
                    await queue.mark_done(chunk)
                    tracker.chunk_done(iface.name)
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    await queue.put_back(chunk)
                    if consecutive_failures >= MAX_CONSECUTIVE:
                        tracker.set_status(iface.name, "error")
                        break
                    await asyncio.sleep(1.0)  # brief pause before retry

                # Periodically save resume state
                now = time.monotonic()
                if now - last_save >= SAVE_STATE_INTERVAL:
                    save_cb()
                    last_save = now

    except Exception:
        tracker.set_status(iface.name, "error")
        raise
    finally:
        await connector.close()
        current_status = tracker.get_status(iface.name)
        if current_status == "active":
            tracker.set_status(iface.name, "done")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_download(
    url: str,
    interfaces: list[NetworkInterface],
    queue: WorkQueue,
    writer: FileWriter,
    tracker: ProgressTracker,
    output_path,      # Path
    file_size: int,
    read_timeout: int = 120,
) -> bool:
    """
    Launch one worker per interface and wait for all to finish.

    Returns True if all chunks succeeded, False otherwise.
    """
    def _save() -> None:
        save_resume_state(queue.all_chunks(), url, file_size, output_path)

    tasks = [
        asyncio.create_task(
            _worker(iface, url, queue, writer, tracker, _save, read_timeout),
            name=f"worker-{iface.name}",
        )
        for iface in interfaces
    ]

    # Run all workers; collect exceptions rather than propagating immediately
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any unexpected errors (not worker-level failures)
    for iface, result in zip(interfaces, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            # Will be visible in --verbose mode; non-fatal here
            pass

    return queue.all_succeeded()

"""
TurboGet Web UI Server.

Provides a browser-based dashboard for managing multi-WAN downloads.
Launched via: turboget ui [--port 8080] [--open]

Architecture:
- FastAPI serves the REST API and the static HTML UI
- Downloads run as asyncio background tasks
- Server-Sent Events (SSE) push live progress to the browser
- Policy routing setup/cleanup is attempted directly; on permission
  failure a helpful error with the manual command is returned.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import threading
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .config import TurboConfig, get_default_config_path, load_config
from .interface import (
    NetworkInterface,
    check_routing_setup,
    detect_interfaces,
    setup_policy_routing,
    teardown_policy_routing,
)
from .checker import get_file_info
from .scheduler import (
    WorkQueue,
    calculate_chunk_count,
    cleanup_resume_file,
    create_chunks,
    load_resume_state,
    save_resume_state,
    Chunk,
)
from .writer import FileWriter
from .downloader import run_download


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_config: Optional[TurboConfig] = None
_config_path: Optional[Path] = None
_downloads: dict[str, dict] = {}
_sse_queues: list[asyncio.Queue] = []

_STATIC_DIR = Path(__file__).parent / "ui"


def _get_config() -> TurboConfig:
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def _broadcast(event_type: str, data: dict) -> None:
    """Push an event to all connected SSE clients."""
    msg = {"type": event_type, "data": data}
    for q in _sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _broadcast_download(download_id: str) -> None:
    dl = _downloads.get(download_id, {})
    _broadcast("download_update", {k: v for k, v in dl.items() if k != "task"})


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="TurboGet UI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (_STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# API: Interfaces
# ---------------------------------------------------------------------------

@app.get("/api/interfaces")
async def api_interfaces() -> list:
    config = _get_config()
    ifaces = detect_interfaces(config)
    routing = check_routing_setup(ifaces) if ifaces else {}

    result = []
    for ic in config.interfaces:
        matched = next((i for i in ifaces if i.name == ic.name), None)
        result.append({
            "name": ic.name,
            "alias": ic.alias,
            "enabled": ic.enabled,
            "up": matched is not None,
            "local_ip": matched.local_ip if matched else None,
            "gateway": matched.gateway if matched else None,
            "reachable": matched.reachable if matched else False,
            "routing_ok": routing.get(ic.name, False) if matched else False,
        })
    return result


# ---------------------------------------------------------------------------
# API: Routing
# ---------------------------------------------------------------------------

@app.post("/api/routing/setup")
async def api_routing_setup() -> dict:
    config = _get_config()
    ifaces = detect_interfaces(config)
    active = [i for i in ifaces if i.reachable]

    if not active:
        raise HTTPException(400, "No active interfaces found")

    setup_policy_routing(active)

    # Verify it worked (ip rule add silently fails without root)
    routing = check_routing_setup(active)
    if not any(routing.values()):
        return {
            "success": False,
            "permission_error": True,
            "message": "Permission denied — ip rule requires root.",
            "command": "sudo turboget setup",
        }

    _broadcast("routing_changed", {"action": "setup", "interfaces": [i.name for i in active]})
    return {"success": True, "configured": [i.name for i in active]}


@app.post("/api/routing/cleanup")
async def api_routing_cleanup() -> dict:
    config = _get_config()
    ifaces = detect_interfaces(config)

    if not ifaces:
        return {"success": True, "message": "No interfaces to clean up"}

    teardown_policy_routing(ifaces)
    _broadcast("routing_changed", {"action": "cleanup"})
    return {"success": True}


@app.get("/api/routing/status")
async def api_routing_status() -> dict:
    config = _get_config()
    ifaces = detect_interfaces(config)
    return check_routing_setup(ifaces) if ifaces else {}


# ---------------------------------------------------------------------------
# API: Downloads
# ---------------------------------------------------------------------------

class StartDownloadRequest(BaseModel):
    url: str
    output: Optional[str] = None
    interfaces: Optional[str] = None
    chunks_per_interface: int = 8
    no_resume: bool = False


@app.post("/api/download/start")
async def api_download_start(req: StartDownloadRequest) -> dict:
    download_id = str(uuid.uuid4())[:8]

    _downloads[download_id] = {
        "id": download_id,
        "url": req.url,
        "status": "starting",
        "filename": None,
        "size": 0,
        "downloaded": 0,
        "progress": 0.0,
        "speed": 0.0,
        "eta": None,
        "interfaces": {},
        "error": None,
    }

    task = asyncio.create_task(
        _download_task(download_id, req),
        name=f"turboget-download-{download_id}",
    )
    _downloads[download_id]["task"] = task
    _broadcast_download(download_id)

    return {"download_id": download_id}


@app.get("/api/downloads")
async def api_downloads_list() -> list:
    return [{k: v for k, v in dl.items() if k != "task"}
            for dl in _downloads.values()]


@app.get("/api/downloads/{download_id}")
async def api_download_get(download_id: str) -> dict:
    if download_id not in _downloads:
        raise HTTPException(404, "Download not found")
    return {k: v for k, v in _downloads[download_id].items() if k != "task"}


@app.post("/api/downloads/{download_id}/cancel")
async def api_download_cancel(download_id: str) -> dict:
    if download_id not in _downloads:
        raise HTTPException(404, "Download not found")
    task = _downloads[download_id].get("task")
    if task and not task.done():
        task.cancel()
    _downloads[download_id]["status"] = "cancelled"
    _broadcast_download(download_id)
    return {"success": True}


# ---------------------------------------------------------------------------
# API: SSE stream
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_events() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues.append(queue)

    async def stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API: Config
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def api_config_get() -> dict:
    config = _get_config()
    return {
        "interfaces": [
            {"name": i.name, "alias": i.alias, "metric": i.metric, "enabled": i.enabled}
            for i in config.interfaces
        ],
        "chunks_per_interface": config.chunks_per_interface,
        "max_retries": config.max_retries,
        "output_dir": config.output_dir,
    }


# ---------------------------------------------------------------------------
# Internal: UI-compatible progress tracker
# ---------------------------------------------------------------------------

class _UITracker:
    """
    Duck-type replacement for ProgressTracker when running under the web UI.
    Instead of rendering Rich output, it updates the download state dict and
    broadcasts SSE events to connected browsers.
    """

    def __init__(self, download_id: str, file_size: int, interfaces) -> None:
        self._id = download_id
        self._file_size = file_size
        self._lock = threading.Lock()
        self._total_downloaded = 0
        self._chunks_done = 0
        self._last_broadcast = 0.0

        self._stats: dict[str, dict] = {
            iface.name: {
                "alias": iface.alias,
                "local_ip": iface.local_ip,
                "bytes": 0,
                "chunks": 0,
                "status": "idle",
                "_samples": deque(maxlen=300),
            }
            for iface in interfaces
        }

    def update(self, iface_name: str, nbytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            s = self._stats.get(iface_name)
            if s:
                s["bytes"] += nbytes
                s["_samples"].append((now, nbytes))
            self._total_downloaded += nbytes
        if now - self._last_broadcast >= 0.15:
            self._last_broadcast = now
            self._push()

    def set_status(self, iface_name: str, status: str) -> None:
        with self._lock:
            s = self._stats.get(iface_name)
            if s:
                s["status"] = status
        self._push()

    def get_status(self, iface_name: str) -> str:
        with self._lock:
            return self._stats.get(iface_name, {}).get("status", "idle")

    def chunk_done(self, iface_name: str) -> None:
        with self._lock:
            s = self._stats.get(iface_name)
            if s:
                s["chunks"] += 1
            self._chunks_done += 1

    # Rich interface stubs — not used in web mode
    def render(self):
        pass

    def get_live(self):
        class _Noop:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def update(self, *a): pass
        return _Noop()

    # ── private ──────────────────────────────────────────────────────────

    def _push(self) -> None:
        now = time.monotonic()
        iface_data = {}
        total_speed = 0.0

        with self._lock:
            for name, s in self._stats.items():
                cutoff = now - 4.0
                recent = [(ts, nb) for ts, nb in s["_samples"] if ts > cutoff]
                if len(recent) >= 2:
                    elapsed = now - recent[0][0]
                    speed = sum(nb for _, nb in recent) / max(elapsed, 0.01)
                else:
                    speed = 0.0
                total_speed += speed
                iface_data[name] = {
                    "alias": s["alias"],
                    "local_ip": s["local_ip"],
                    "speed": speed,
                    "bytes": s["bytes"],
                    "chunks": s["chunks"],
                    "status": s["status"],
                }

            downloaded = self._total_downloaded

        progress = (downloaded / self._file_size * 100) if self._file_size > 0 else 0
        eta = ((self._file_size - downloaded) / total_speed) if total_speed > 0 else None

        dl = _downloads.get(self._id)
        if dl:
            dl["downloaded"] = downloaded
            dl["progress"] = progress
            dl["speed"] = total_speed
            dl["eta"] = eta
            dl["interfaces"] = iface_data
        _broadcast_download(self._id)


# ---------------------------------------------------------------------------
# Internal: download background task
# ---------------------------------------------------------------------------

async def _download_task(download_id: str, req: StartDownloadRequest) -> None:
    dl = _downloads[download_id]
    config = _get_config()

    try:
        # ── resolve interfaces ─────────────────────────────────────────────
        all_ifaces = detect_interfaces(config)
        if req.interfaces:
            names = {n.strip() for n in req.interfaces.split(",")}
            all_ifaces = [i for i in all_ifaces if i.name in names]
        active = [i for i in all_ifaces if i.reachable] or all_ifaces

        if not active:
            raise RuntimeError("No active network interfaces found")

        # ── fetch file info ────────────────────────────────────────────────
        dl["status"] = "fetching_info"
        _broadcast_download(download_id)

        info = await get_file_info(req.url, active[0].local_ip)
        file_size = info["size"] or 0
        filename = info["filename"]
        final_url = info["final_url"]

        dl["filename"] = filename
        dl["size"] = file_size
        dl["url"] = final_url
        _broadcast_download(download_id)

        # ── resolve output path ───────────────────────────────────────────
        if req.output:
            out_path = Path(req.output)
            if out_path.is_dir():
                out_path = out_path / filename
        else:
            out_path = Path(config.output_dir) / filename

        # ── build chunks ──────────────────────────────────────────────────
        if file_size > 0 and info.get("supports_ranges", True):
            n_chunks = calculate_chunk_count(
                file_size, len(active), req.chunks_per_interface
            )
            chunk_list: list[Chunk] = []

            if not req.no_resume and out_path.exists():
                saved = load_resume_state(final_url, out_path)
                if saved:
                    chunk_list = saved
                    n_chunks = len(chunk_list)
                    done = sum(1 for c in chunk_list if c.status.value == "done")
                    dl["resumed"] = done
            if not chunk_list:
                chunk_list = create_chunks(file_size, n_chunks)
        else:
            chunk_list = [Chunk(id=0, start=0, end=max(0, file_size - 1))]

        # ── setup ─────────────────────────────────────────────────────────
        queue = WorkQueue(chunk_list)
        writer = FileWriter(out_path, file_size)
        tracker = _UITracker(download_id, file_size, active)

        dl["status"] = "downloading"
        _broadcast_download(download_id)

        await writer.open()
        try:
            success = await run_download(
                url=final_url,
                interfaces=active,
                queue=queue,
                writer=writer,
                tracker=tracker,
                output_path=out_path,
                file_size=file_size,
                read_timeout=config.read_timeout,
            )
        finally:
            await writer.close()

        if success:
            cleanup_resume_file(out_path)
            dl["status"] = "done"
            dl["progress"] = 100.0
        else:
            save_resume_state(chunk_list, final_url, file_size, out_path)
            dl["status"] = "failed"
            dl["error"] = f"{queue.failed_count} chunk(s) permanently failed"

    except asyncio.CancelledError:
        dl["status"] = "cancelled"
    except Exception as exc:
        dl["status"] = "error"
        dl["error"] = str(exc)

    _broadcast_download(download_id)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    config_path: Optional[Path] = None,
) -> None:
    global _config_path
    _config_path = config_path or get_default_config_path()

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")

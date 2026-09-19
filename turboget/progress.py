"""
Rich terminal progress display for TurboGet.

Layout
------
╭─ ⚡ TurboGet v1.0.0 ────────────────────────────────────────────╮
│  ubuntu-24.04.2-desktop-amd64.iso          6.14 GB              │
╰─────────────────────────────────────────────────────────────────╯
  Overall   ████████████████████░░░░░░░░░░░░  62%  3.8 GB  1m 44s

  Interface        Speed       Downloaded   Chunks   Status
  ────────────────────────────────────────────────────────────
  📶  WiFi Hotspot 12.3 MB/s  1.4 GB       18/24    ⚡ active
  📱  USB Tether    8.7 MB/s  1.1 GB       14/24    ⚡ active
  🔌  Home Fiber   11.1 MB/s  1.3 GB       16/24    ⚡ active
  ────────────────────────────────────────────────────────────
  Combined: 32.1 MB/s  •  3 interfaces  •  All healthy
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rich import box

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Speed tracking (exponential moving average)
# ---------------------------------------------------------------------------

_SPEED_WINDOW = 4.0   # seconds to average speed over


@dataclass
class _SpeedSample:
    ts: float
    nbytes: int


@dataclass
class InterfaceStats:
    name: str
    alias: str
    local_ip: str
    bytes_downloaded: int = 0
    chunks_done: int = 0
    status: str = "idle"         # idle | active | done | error
    _samples: deque = field(default_factory=lambda: deque(maxlen=200))

    def add_bytes(self, n: int) -> None:
        self.bytes_downloaded += n
        self._samples.append(_SpeedSample(time.monotonic(), n))

    @property
    def speed_bps(self) -> float:
        now = time.monotonic()
        cutoff = now - _SPEED_WINDOW
        recent = [s for s in self._samples if s.ts > cutoff]
        if not recent:
            return 0.0
        elapsed = now - recent[0].ts
        if elapsed < 0.05:
            return 0.0
        return sum(s.nbytes for s in recent) / elapsed


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "[dim]—[/dim]"
    mbs = bps / 1_048_576
    if mbs >= 1:
        return f"[bold green]{mbs:.1f}[/bold green] [dim]MB/s[/dim]"
    kbs = bps / 1_024
    return f"[yellow]{kbs:.0f}[/yellow] [dim]KB/s[/dim]"


def _fmt_bytes(b: int) -> str:
    if b < 1_024:
        return f"{b} B"
    if b < 1_048_576:
        return f"{b/1_024:.1f} KB"
    if b < 1_073_741_824:
        return f"{b/1_048_576:.1f} MB"
    return f"{b/1_073_741_824:.2f} GB"


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "[dim]—[/dim]"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds//60)}m {int(seconds%60)}s"
    return f"{int(seconds//3600)}h {int((seconds%3600)//60)}m"


def _status_cell(status: str) -> Text:
    icons = {
        "idle":    ("💤", "dim"),
        "active":  ("⚡", "bright_cyan"),
        "done":    ("✅", "green"),
        "error":   ("❌", "red"),
        "waiting": ("⏳", "yellow"),
    }
    icon, style = icons.get(status, ("•", "dim"))
    return Text(f"{icon} {status}", style=style)


# ---------------------------------------------------------------------------
# Main tracker
# ---------------------------------------------------------------------------

class ProgressTracker:
    """
    Thread-safe tracker for all download progress.

    Workers call update() and chunk_done() from async tasks;
    the Rich Live display calls render() from the main thread.
    """

    def __init__(
        self,
        filename: str,
        file_size: int,
        total_chunks: int,
        interfaces,           # list[NetworkInterface]
    ) -> None:
        self._filename = filename
        self._file_size = file_size
        self._total_chunks = total_chunks
        self._start_time = time.monotonic()
        self._lock = threading.Lock()

        self._stats: dict[str, InterfaceStats] = {
            iface.name: InterfaceStats(
                name=iface.name,
                alias=iface.alias,
                local_ip=iface.local_ip,
            )
            for iface in interfaces
        }
        self._total_downloaded: int = 0
        self._chunks_done: int = 0

        # Rich Progress widget for the overall bar
        self._overall = Progress(
            TextColumn("[bold white]{task.description}"),
            BarColumn(
                bar_width=None,
                style="cyan",
                complete_style="bright_cyan",
                finished_style="bright_green",
            ),
            TextColumn("[bold]{task.percentage:>3.0f}%"),
            DownloadColumn(binary_units=True),
            TextColumn("•"),
            TransferSpeedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(compact=True),
            expand=True,
        )
        self._overall_task: TaskID = self._overall.add_task(
            "Overall", total=file_size
        )
        self._console = Console()

    # ── update API ─────────────────────────────────────────────────────────

    def update(self, iface_name: str, nbytes: int) -> None:
        """Called by download workers each time a block arrives."""
        with self._lock:
            if iface_name in self._stats:
                self._stats[iface_name].add_bytes(nbytes)
            self._total_downloaded += nbytes
        self._overall.advance(self._overall_task, nbytes)

    def set_status(self, iface_name: str, status: str) -> None:
        with self._lock:
            if iface_name in self._stats:
                self._stats[iface_name].status = status

    def get_status(self, iface_name: str) -> str:
        with self._lock:
            if iface_name in self._stats:
                return self._stats[iface_name].status
            return "idle"

    def chunk_done(self, iface_name: str) -> None:
        with self._lock:
            if iface_name in self._stats:
                self._stats[iface_name].chunks_done += 1
            self._chunks_done += 1

    # ── computed properties ─────────────────────────────────────────────────

    @property
    def total_speed_bps(self) -> float:
        with self._lock:
            return sum(s.speed_bps for s in self._stats.values())

    @property
    def bytes_downloaded(self) -> int:
        return self._total_downloaded

    @property
    def chunks_done(self) -> int:
        return self._chunks_done

    @property
    def eta_seconds(self) -> Optional[float]:
        speed = self.total_speed_bps
        if speed <= 0:
            return None
        remaining = self._file_size - self._total_downloaded
        return remaining / speed if remaining > 0 else 0.0

    # ── rendering ──────────────────────────────────────────────────────────

    def render(self):
        """Build and return a Rich renderable for the current state."""
        with self._lock:
            return self._build_panel()

    def _build_panel(self):
        # ── interface table ─────────────────────────────────────────────
        tbl = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold dim",
            padding=(0, 1),
            expand=True,
        )
        tbl.add_column("Interface",   style="cyan",  min_width=20)
        tbl.add_column("Speed",       justify="right", min_width=12)
        tbl.add_column("Downloaded",  justify="right", min_width=10)
        tbl.add_column("Chunks",      justify="center", min_width=6)
        tbl.add_column("Status",      min_width=10)

        iface_emojis = {"wlan": "📶", "wifi": "📶", "usb": "📱",
                         "eth": "🔌", "enp": "🔌", "eno": "🔌",
                         "enx": "📱", "ww": "📡"}

        for stats in self._stats.values():
            emoji = next(
                (v for k, v in iface_emojis.items() if stats.name.lower().startswith(k)),
                "🌐",
            )
            tbl.add_row(
                f"{emoji}  {stats.alias}",
                _fmt_speed(stats.speed_bps),
                _fmt_bytes(stats.bytes_downloaded),
                f"[dim]{stats.chunks_done}[/dim]",
                _status_cell(stats.status),
            )

        # ── footer ──────────────────────────────────────────────────────
        combined = sum(s.speed_bps for s in self._stats.values())
        active_count = sum(1 for s in self._stats.values() if s.status == "active")
        health = (
            "[green]All healthy[/green]"
            if active_count == len(self._stats)
            else f"[yellow]{active_count}/{len(self._stats)} active[/yellow]"
        )

        speed_str = f"{combined/1_048_576:.1f} MB/s" if combined >= 1024 else f"{combined/1024:.0f} KB/s"
        footer = Text.assemble(
            ("Combined: ", "dim"),
            (speed_str, "bold green"),
            ("  •  ", "dim"),
            (f"{len(self._stats)} interfaces", "cyan"),
            ("  •  ", "dim"),
        )
        footer.append_text(Text.from_markup(health))

        # ── assemble ────────────────────────────────────────────────────
        return Panel(
            Group(
                self._overall,
                Rule(style="dim"),
                tbl,
                Rule(style="dim"),
                footer,
            ),
            title=Text.assemble(
                ("⚡ TurboGet  ", "bold cyan"),
                (self._filename, "bold white"),
                (f"  {_fmt_bytes(self._file_size)}", "dim"),
            ),
            subtitle=Text.assemble(
                ("Chunks: ", "dim"),
                (f"{self._chunks_done}/{self._total_chunks}", "white"),
                ("  •  ETA: ", "dim"),
                (_fmt_eta(self.eta_seconds).replace("[dim]", "").replace("[/dim]", ""), "dim"),
            ),
            border_style="cyan",
            padding=(0, 1),
        )

    def get_live(self) -> Live:
        return Live(
            self.render,          # callable — Rich calls this each refresh
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )

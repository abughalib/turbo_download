"""
TurboGet CLI — entry point for all user-facing commands.

Commands
--------
turboget download <URL>   Download a file using all active interfaces
turboget verify           Check interfaces, connectivity, and routing
turboget setup            Configure Linux policy routing (requires sudo)
turboget cleanup          Remove policy routing rules (requires sudo)
turboget config show      Print current configuration
turboget config detect    Auto-detect interfaces and write config
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from . import __version__
from .checker import get_file_info, verify_range_support
from .config import InterfaceConfig, TurboConfig, get_default_config_path, load_config, save_config
from .downloader import run_download
from .interface import (
    NetworkInterface,
    auto_detect_interfaces,
    check_connectivity,
    check_routing_setup,
    detect_interfaces,
    setup_policy_routing,
    teardown_policy_routing,
)
from .progress import ProgressTracker
from .scheduler import (
    WorkQueue,
    calculate_chunk_count,
    cleanup_resume_file,
    create_chunks,
    load_resume_state,
    save_resume_state,
)
from .writer import FileWriter


console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="turboget")
@click.option(
    "--config", "-C",
    default=None,
    metavar="FILE",
    help="Path to config.json (default: auto-detect)",
)
@click.pass_context
def cli(ctx: click.Context, config: Optional[str]) -> None:
    """⚡ TurboGet — Multi-WAN parallel download manager."""
    ctx.ensure_object(dict)
    config_path = Path(config) if config else get_default_config_path()
    ctx.obj["config"] = load_config(config_path)
    ctx.obj["config_path"] = config_path


# ---------------------------------------------------------------------------
# turboget download
# ---------------------------------------------------------------------------

@cli.command("download")
@click.argument("url")
@click.option("-o", "--output",    default=None,  help="Output file path")
@click.option("-i", "--interfaces",default=None,  help="Comma-separated interface names to use")
@click.option("-c", "--chunks",    default=None,  type=int, help="Chunks per interface (default: 8)")
@click.option("--no-resume",       is_flag=True,  default=False, help="Ignore existing resume state")
@click.option("--fallback",        is_flag=True,  default=False,
              help="Download even if server does not support Range requests")
@click.option("--single",          is_flag=True,  default=False,
              help="Force single-stream (no splitting); still uses all interfaces in round-robin")
@click.pass_context
def download(
    ctx: click.Context,
    url: str,
    output: Optional[str],
    interfaces: Optional[str],
    chunks: Optional[int],
    no_resume: bool,
    fallback: bool,
    single: bool,
) -> None:
    """Download a file using all configured network interfaces."""
    config: TurboConfig = ctx.obj["config"]

    asyncio.run(
        _download_async(
            url=url,
            config=config,
            output_override=output,
            iface_filter=interfaces,
            chunks_override=chunks,
            no_resume=no_resume,
            fallback=fallback,
            single=single,
        )
    )


async def _download_async(
    url: str,
    config: TurboConfig,
    output_override: Optional[str],
    iface_filter: Optional[str],
    chunks_override: Optional[int],
    no_resume: bool,
    fallback: bool,
    single: bool,
) -> None:
    # ── Step 1: resolve interfaces ─────────────────────────────────────────
    all_ifaces = detect_interfaces(config)

    if not all_ifaces:
        err_console.print(
            "[red][!] No active interfaces found.[/red] "
            "Check config.json or run [bold]turboget config detect[/bold]."
        )
        sys.exit(1)

    if iface_filter:
        names = {n.strip() for n in iface_filter.split(",")}
        all_ifaces = [i for i in all_ifaces if i.name in names]
        if not all_ifaces:
            err_console.print(f"[red][!] None of the specified interfaces are active: {iface_filter}[/red]")
            sys.exit(1)

    reachable = [i for i in all_ifaces if i.reachable]
    if not reachable:
        err_console.print("[yellow][!] No interface has confirmed internet connectivity.[/yellow]")
        err_console.print("    Trying anyway with all detected interfaces...")
        reachable = all_ifaces

    if len(reachable) < 2:
        console.print(
            f"[yellow]⚠  Only 1 interface active ({reachable[0].alias}). "
            "No bandwidth aggregation.[/yellow]"
        )
    
    active_ifaces = reachable

    # ── Step 2: fetch file info ────────────────────────────────────────────
    console.print(f"\n[dim]Fetching file info…[/dim]")
    try:
        info = await get_file_info(url, local_ip=active_ifaces[0].local_ip)
    except Exception as exc:
        err_console.print(f"[red][!] Failed to contact server: {exc}[/red]")
        sys.exit(1)

    final_url = info["final_url"]
    file_size: Optional[int] = info["size"]
    filename = info["filename"]
    supports_ranges = info["supports_ranges"]

    # Show what we found
    _print_file_info(info, active_ifaces)

    # ── Step 3: validate range support ────────────────────────────────────
    if not supports_ranges and not single:
        # Double-check: some servers lie in headers
        supports_ranges = await verify_range_support(final_url, active_ifaces[0].local_ip)

    if not supports_ranges and not fallback and not single:
        err_console.print(
            "\n[yellow][!] Server does not support Range requests.[/yellow]\n"
            "    Parallel download is not possible for this URL.\n"
            "    Use [bold]--fallback[/bold] to download normally, "
            "or [bold]--single[/bold] to force single-stream.\n"
        )
        sys.exit(1)

    if file_size is None:
        err_console.print(
            "\n[yellow][!] Server did not return Content-Length.[/yellow]\n"
            "    Cannot pre-split chunks. Falling back to single-stream download.\n"
        )
        single = True

    # ── Step 4: resolve output path ───────────────────────────────────────
    out_path = _resolve_output(output_override, filename, config.output_dir)
    console.print(f"[dim]Output:[/dim] [bold]{out_path}[/bold]")

    # ── Step 5: build chunk list ──────────────────────────────────────────
    if single or not supports_ranges:
        # Wrap whole file as one chunk
        from .scheduler import Chunk, ChunkStatus
        chunk_list = [Chunk(id=0, start=0, end=(file_size or 0) - 1)]
        n_chunks = 1
    else:
        cpi = chunks_override or config.chunks_per_interface
        n_chunks = calculate_chunk_count(file_size, len(active_ifaces), cpi)

        # Try to resume
        resumed = False
        if not no_resume and out_path.exists():
            saved = load_resume_state(final_url, out_path)
            if saved:
                done_count = sum(1 for c in saved if c.status.value == "done")
                console.print(
                    f"\n[cyan]↩  Resuming:[/cyan] {done_count}/{len(saved)} chunks already done."
                )
                chunk_list = saved
                n_chunks = len(chunk_list)
                resumed = True

        if not resumed:
            chunk_list = create_chunks(file_size, n_chunks)

    console.print(
        f"[dim]Splitting into[/dim] [bold]{n_chunks}[/bold] [dim]chunks across[/dim] "
        f"[bold]{len(active_ifaces)}[/bold] [dim]interfaces…[/dim]\n"
    )

    # ── Step 6: check routing ─────────────────────────────────────────────
    routing_status = check_routing_setup(active_ifaces)
    missing_routing = [name for name, ok in routing_status.items() if not ok]
    if missing_routing and len(active_ifaces) > 1:
        console.print(
            "[yellow]⚠  Policy routing not configured for: "
            + ", ".join(missing_routing)
            + "[/yellow]\n"
            "   Run [bold]sudo turboget setup[/bold] for guaranteed multi-NIC routing.\n"
            "   Continuing anyway (may not use all interfaces).\n"
        )

    # ── Step 7: setup writer, queue, tracker ──────────────────────────────
    queue = WorkQueue(chunk_list)
    writer = FileWriter(out_path, file_size or 0)
    tracker = ProgressTracker(filename, file_size or 0, n_chunks, active_ifaces)

    t_start = time.monotonic()

    await writer.open()

    try:
        with tracker.get_live() as live:
            success = await run_download(
                url=final_url,
                interfaces=active_ifaces,
                queue=queue,
                writer=writer,
                tracker=tracker,
                output_path=out_path,
                file_size=file_size or 0,
                read_timeout=config.read_timeout,
            )
            # Final render update
            live.update(tracker.render())
    finally:
        await writer.close()

    # ── Step 8: report ────────────────────────────────────────────────────
    elapsed = time.monotonic() - t_start
    if success:
        cleanup_resume_file(out_path)
        avg_speed = (file_size or writer.bytes_written) / max(elapsed, 0.001)
        console.print(
            f"\n[bold green]✅ Download complete![/bold green]  "
            f"{_fmt_bytes(file_size or writer.bytes_written)} in "
            f"{_fmt_elapsed(elapsed)}  •  avg {_fmt_speed(avg_speed)}\n"
            f"   Saved to: [bold]{out_path}[/bold]\n"
        )
    else:
        failed = queue.failed_count
        console.print(
            f"\n[red]✗ Download incomplete.[/red] "
            f"{failed} chunk(s) failed after retries.\n"
            f"  Resume state saved — run the same command to continue.\n"
        )
        # Save final resume state
        save_resume_state(chunk_list, final_url, file_size or 0, out_path)
        sys.exit(1)


# ---------------------------------------------------------------------------
# turboget verify
# ---------------------------------------------------------------------------

@cli.command("verify")
@click.pass_context
def verify(ctx: click.Context) -> None:
    """Check interfaces, internet connectivity, and policy routing status."""
    config: TurboConfig = ctx.obj["config"]

    console.print(f"\n[bold cyan]⚡ TurboGet {__version__} — System Verify[/bold cyan]\n")

    # Detect interfaces
    ifaces = detect_interfaces(config)
    routing = check_routing_setup(ifaces) if ifaces else {}

    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold dim",
                title="Network Interfaces", title_style="bold")
    tbl.add_column("Interface",   style="cyan")
    tbl.add_column("Alias",       style="white")
    tbl.add_column("Local IP",    style="yellow")
    tbl.add_column("Gateway",     style="dim")
    tbl.add_column("Internet",    justify="center")
    tbl.add_column("Routing",     justify="center")

    for cfg_iface in config.interfaces:
        matched = next((i for i in ifaces if i.name == cfg_iface.name), None)
        if matched:
            internet = "[green]✓[/green]" if matched.reachable else "[red]✗[/red]"
            routing_ok = routing.get(matched.name, False)
            routing_cell = "[green]✓[/green]" if routing_ok else "[yellow]not set[/yellow]"
            tbl.add_row(
                matched.name, matched.alias, matched.local_ip,
                matched.gateway or "—", internet, routing_cell
            )
        else:
            tbl.add_row(
                cfg_iface.name, cfg_iface.alias, "[dim]DOWN[/dim]",
                "—", "[red]✗[/red]", "[dim]—[/dim]"
            )

    console.print(tbl)

    missing = [name for name, ok in routing.items() if not ok]
    if missing:
        console.print(
            f"\n[yellow]⚠  Policy routing not configured for: {', '.join(missing)}[/yellow]\n"
            "   Run: [bold]sudo turboget setup[/bold]\n"
        )
    elif ifaces:
        console.print("\n[green]✓ All routing rules in place.[/green]\n")


# ---------------------------------------------------------------------------
# turboget setup / cleanup
# ---------------------------------------------------------------------------

@cli.command("setup")
@click.pass_context
def setup(ctx: click.Context) -> None:
    """
    Configure Linux policy routing for multi-NIC downloads.

    Must be run with sudo. Rules persist until reboot (or 'turboget cleanup').
    """
    if os.geteuid() != 0:
        err_console.print(
            "[red][!] setup requires root.[/red] Run: [bold]sudo turboget setup[/bold]"
        )
        sys.exit(1)

    config: TurboConfig = ctx.obj["config"]
    ifaces = detect_interfaces(config)

    if not ifaces:
        err_console.print("[red][!] No active interfaces found.[/red]")
        sys.exit(1)

    console.print(f"\n[bold cyan]Setting up policy routing…[/bold cyan]\n")
    setup_policy_routing(ifaces)

    for iface in ifaces:
        console.print(
            f"  [green]✓[/green]  {iface.alias} ([dim]{iface.name}[/dim]) "
            f"→ table [bold]{iface.table_id}[/bold] via [yellow]{iface.gateway}[/yellow]"
        )

    console.print(
        f"\n[green]✓ Routing configured for {len(ifaces)} interface(s).[/green]\n"
        "  Rules are active until reboot. Run [bold]sudo turboget cleanup[/bold] to remove.\n"
    )


@cli.command("cleanup")
@click.pass_context
def cleanup(ctx: click.Context) -> None:
    """Remove TurboGet policy routing rules (requires sudo)."""
    if os.geteuid() != 0:
        err_console.print(
            "[red][!] cleanup requires root.[/red] Run: [bold]sudo turboget cleanup[/bold]"
        )
        sys.exit(1)

    config: TurboConfig = ctx.obj["config"]
    ifaces = detect_interfaces(config)

    if not ifaces:
        console.print("[dim]No active interfaces to clean up.[/dim]")
        return

    console.print(f"\n[bold cyan]Removing policy routing rules…[/bold cyan]\n")
    teardown_policy_routing(ifaces)

    for iface in ifaces:
        console.print(f"  [dim]removed[/dim]  {iface.alias} table {iface.table_id}")

    console.print(f"\n[green]✓ Routing rules removed.[/green]\n")


# ---------------------------------------------------------------------------
# turboget ui
# ---------------------------------------------------------------------------

@cli.command("ui")
@click.option("--host",   default="127.0.0.1", show_default=True,  help="Host to bind to")
@click.option("--port",   "-p", default=8080, show_default=True, type=int, help="Port to listen on")
@click.option("--open",   "open_browser", is_flag=True, default=False, help="Open browser automatically")
@click.pass_context
def ui_cmd(ctx: click.Context, host: str, port: int, open_browser: bool) -> None:
    """Launch the TurboGet web UI dashboard."""
    try:
        from .server import run_server
    except ImportError:
        err_console.print(
            "[red][!] Web UI dependencies not installed.[/red]\n"
            "    Run: [bold]uv add fastapi uvicorn[/bold]"
        )
        raise SystemExit(1)

    config_path: Path = ctx.obj.get("config_path", get_default_config_path())

    url = f"http://{host}:{port}"
    console.print(Panel(
        f"[bold cyan]⚡ TurboGet UI[/bold cyan]\n\n"
        f"  [dim]Dashboard →[/dim] [bold][link={url}]{url}[/link][/bold]\n\n"
        f"  [dim]Press Ctrl+C to stop[/dim]",
        border_style="cyan",
        expand=False,
    ))

    if open_browser:
        import threading, webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    run_server(host=host, port=port, config_path=config_path)


# ---------------------------------------------------------------------------
# turboget config
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group() -> None:
    """View and manage TurboGet configuration."""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Display the current configuration."""
    config: TurboConfig = ctx.obj["config"]
    config_path: Path = ctx.obj["config_path"]

    console.print(f"\n[bold]Config file:[/bold] [dim]{config_path}[/dim]\n")

    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold dim")
    tbl.add_column("Interface Name", style="cyan")
    tbl.add_column("Alias",          style="white")
    tbl.add_column("Metric",         justify="center")
    tbl.add_column("Enabled",        justify="center")

    for iface in config.interfaces:
        tbl.add_row(
            iface.name, iface.alias,
            str(iface.metric),
            "[green]✓[/green]" if iface.enabled else "[dim]✗[/dim]"
        )

    console.print(tbl)
    console.print(
        f"\n  [dim]chunks_per_interface:[/dim] {config.chunks_per_interface}\n"
        f"  [dim]max_retries:         [/dim] {config.max_retries}\n"
        f"  [dim]output_dir:          [/dim] {config.output_dir}\n"
    )


@config_group.command("detect")
@click.option("--write", "-w", is_flag=True, default=False,
              help="Write detected interfaces to config.json")
@click.pass_context
def config_detect(ctx: click.Context, write: bool) -> None:
    """Auto-detect network interfaces with internet connectivity."""
    config_path: Path = ctx.obj["config_path"]

    console.print("\n[bold cyan]Scanning network interfaces…[/bold cyan]\n")
    found = auto_detect_interfaces()

    if not found:
        err_console.print("[red][!] No interfaces with internet connectivity found.[/red]")
        sys.exit(1)

    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold dim")
    tbl.add_column("#",          justify="center", width=3)
    tbl.add_column("Interface",  style="cyan")
    tbl.add_column("Local IP",   style="yellow")
    tbl.add_column("Gateway",    style="dim")

    for i, iface in enumerate(found, 1):
        tbl.add_row(str(i), iface["name"], iface["ip"], iface["gateway"])

    console.print(tbl)

    if write:
        existing = load_config(config_path) if config_path.exists() else TurboConfig()
        existing_names = {i.name for i in existing.interfaces}

        aliases = {
            "wlan": "WiFi",
            "wl":   "WiFi",
            "enx":  "USB Ethernet",
            "usb":  "USB Tethering",
            "enp":  "Ethernet",
            "eth":  "Ethernet",
            "eno":  "Ethernet",
            "ww":   "LTE/5G",
        }

        for iface in found:
            if iface["name"] not in existing_names:
                prefix = next(
                    (v for k, v in aliases.items() if iface["name"].startswith(k)),
                    "Interface",
                )
                existing.interfaces.append(InterfaceConfig(
                    name=iface["name"],
                    alias=prefix,
                    metric=200,
                    enabled=True,
                ))

        save_config(existing, config_path)
        console.print(f"\n[green]✓ Config written to {config_path}[/green]")
        console.print("  Edit [bold]alias[/bold] fields to give interfaces friendly names.\n")
    else:
        console.print(
            "\n  Run with [bold]--write[/bold] to save these to config.json.\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_output(output_override: Optional[str], filename: str,
                    output_dir: str) -> Path:
    if output_override:
        p = Path(output_override)
        if p.is_dir():
            return p / filename
        return p
    return Path(output_dir) / filename


def _print_file_info(info: dict, ifaces: list[NetworkInterface]) -> None:
    size_str = _fmt_bytes(info["size"]) if info["size"] else "unknown"
    range_str = "[green]✓ supported[/green]" if info["supports_ranges"] else "[red]✗ not supported[/red]"
    iface_str = "  ".join(
        f"[cyan]{i.alias}[/cyan] [dim]({i.local_ip})[/dim]" for i in ifaces
    )
    console.print(Panel(
        f"[bold]{info['filename']}[/bold]  [dim]{size_str}[/dim]\n"
        f"[dim]Range requests:[/dim] {range_str}\n"
        f"[dim]Interfaces:[/dim]    {iface_str}",
        title="[bold cyan]Download Info[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))


def _fmt_bytes(b: int) -> str:
    if b < 1_024:
        return f"{b} B"
    if b < 1_048_576:
        return f"{b/1_024:.1f} KB"
    if b < 1_073_741_824:
        return f"{b/1_048_576:.1f} MB"
    return f"{b/1_073_741_824:.2f} GB"


def _fmt_speed(bps: float) -> str:
    mbs = bps / 1_048_576
    if mbs >= 1:
        return f"{mbs:.1f} MB/s"
    return f"{bps/1_024:.0f} KB/s"


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds//60)}m {int(seconds%60)}s"
    return f"{int(seconds//3600)}h {int((seconds%3600)//60)}m"

# ⚡ TurboGet

**Multi-WAN parallel download manager for Linux.**

TurboGet splits a file into chunks and downloads each chunk through a **different internet connection** simultaneously — your home broadband, a USB tethered connection, and a Wi-Fi hotspot all working at once.

It features a command-line interface as well as a modern Web UI to manage downloads and monitor per-interface speeds in real time.

```
╭─ ⚡ TurboGet v1.0.0 ── ubuntu-24.04.2-desktop-amd64.iso  6.14 GB ─────╮
│                                                                          │
│  Overall   ████████████████████░░░░░░░░░░  62%  3.8 GiB  •  1m 44s     │
│  ────────────────────────────────────────────────────────────────────    │
│  Interface          Speed        Downloaded   Chunks   Status            │
│  📶  Jio Hotspot   12.3 MB/s   1.4 GB         18       ⚡ active        │
│  📱  Airtel USB     8.7 MB/s   1.1 GB         14       ⚡ active        │
│  🔌  Home Fiber    11.1 MB/s   1.3 GB         16       ⚡ active        │
│  ────────────────────────────────────────────────────────────────────    │
│  Combined: 32.1 MB/s  •  3 interfaces  •  All healthy                   │
╰──────────────────────────────────────────────────────────────────────────╯
```

---

## Why TurboGet?

Most download managers open multiple connections to the same server — but all through the **same network interface (NIC)**. TurboGet is different:

| Feature | TurboGet | aria2 / wget |
|---|---|---|
| Uses multiple internet connections | ✅ | ❌ |
| Linux policy routing (guaranteed NIC binding) | ✅ | ❌ |
| Distributes data cap across ISPs | ✅ | ❌ |
| Work-stealing chunk scheduler | ✅ | — |
| Resume interrupted downloads | ✅ | ✅ |
| Web UI Dashboard | ✅ | — |
| No external proxy required | ✅ | — |

**Primary use case**: You have multiple internet connections (e.g., Ethernet + Wi-Fi + USB Tethering) with limited bandwidth or data caps. TurboGet downloads portions of the file from each ISP simultaneously, bypassing individual caps and aggregating total bandwidth.

---

## How It Works

### 1. HTTP Range Requests
Most file servers support `Range: bytes=X-Y` headers. TurboGet splits the file into chunks and assigns each chunk to a different interface.

### 2. Linux Policy Routing (The Secret Sauce)
Simply binding a socket to a local IP is not enough — the Linux kernel's default route table is destination-based and may still route packets via the wrong NIC.

TurboGet creates a **private routing table per interface**:

```bash
# Example: Per-interface routing table (e.g., for wlan0 at 192.168.43.156)
ip route add 192.168.43.0/24 dev wlan0 src 192.168.43.156 table 102
ip route add default via 192.168.43.1 dev wlan0 table 102

# Rule: "packets from 192.168.43.156 → use table 102"
ip rule add from 192.168.43.156 table 102 priority 1020
```

Now any socket bound to `192.168.43.156` is **guaranteed** to exit via `wlan0`, regardless of the system default route.

### 3. Work-Stealing Scheduler
All chunks share a single queue. Faster interfaces naturally pull more chunks. If one interface fails mid-chunk, the chunk goes back to the queue for any other interface to retry.

---

## Installation

### Requirements
- Linux (kernel ≥ 4.x, `iproute2` installed)
- Python 3.11+
- `sudo` for the one-time routing setup

**Note on WSL / WSL2:** TurboGet relies on Linux policy routing and direct access to multiple network interfaces. By default, WSL2 uses a single virtual NAT adapter connected to the Windows host. Because the Windows host manages the physical routing, TurboGet's multi-WAN capabilities will **not** work inside a standard WSL setup. You must run it on a native Linux host (or a VM with physically bridged adapters).

### Install with pip

```bash
pip install turboget
```

### Install with uv (recommended)

```bash
uv tool install turboget
```

---

## Quick Start

### 1. Configure your interfaces

```bash
# Auto-detect interfaces and save to config.json
turboget config detect --write
```

Example `config.json`:
```json
{
    "interfaces": [
        {
            "name": "enp4s0",
            "alias": "Home Ethernet",
            "metric": 100,
            "enabled": true
        },
        {
            "name": "enx...",
            "alias": "USB Tether",
            "metric": 200,
            "enabled": true
        },
        {
            "name": "wlan0",
            "alias": "WiFi Hotspot",
            "metric": 300,
            "enabled": true
        }
    ],
    "chunks_per_interface": 8
}
```

Find your interface names with: `ip link show` or `nmcli device`

### 2. Set up policy routing (once per boot)

```bash
sudo turboget setup
```

This creates the `ip rule` and `ip route` entries. They persist until reboot or until you run `sudo turboget cleanup`.

### 3. Verify everything looks good

```bash
turboget verify
```

### 4. Download!

**Via CLI:**
```bash
turboget download https://example.com/bigfile.iso
```

**Via Web UI:**
```bash
# Launch the dashboard locally
turboget ui --open
```

---

## Usage Reference

```
Usage: turboget [OPTIONS] COMMAND [ARGS]...

  ⚡ TurboGet — Multi-WAN parallel download manager.

Options:
  -C, --config FILE  Path to config.json
  --version          Show version and exit.
  --help             Show this message and exit.

Commands:
  download  Download a file using all configured network interfaces.
  ui        Launch the TurboGet web UI dashboard.
  verify    Check interfaces, internet connectivity, and routing status.
  setup     Configure Linux policy routing (requires sudo).
  cleanup   Remove TurboGet policy routing rules (requires sudo).
  config    View and manage TurboGet configuration.
```

### CLI Download Options

```
turboget download [OPTIONS] URL

Options:
  -o, --output TEXT      Output file path or directory
  -i, --interfaces TEXT  Comma-separated interface names to use (e.g. wlan0,enp4s0)
  -c, --chunks INTEGER   Chunks per interface (default: 8)
  --no-resume            Ignore existing resume state
  --fallback             Download even without Range support (single stream)
  --single               Force single-stream (no chunk splitting)
```

---

## Resume Downloads

TurboGet automatically saves a `.filename.turboget` sidecar file next to the output.
If a download is interrupted (Ctrl+C, network failure, power loss), simply re-run the
same command — TurboGet will skip already-completed chunks and continue from where it left off.

```bash
# Interrupted:
turboget download https://example.com/huge.iso
# ↩  Resuming: 18/24 chunks already done.

# Force restart from scratch:
turboget download --no-resume https://example.com/huge.iso
```

---

## Troubleshooting

### "No active interfaces found"
- Run `turboget config detect` to scan available interfaces
- Check that interface names in `config.json` match `ip link show`

### Download doesn't use all interfaces
- Run `sudo turboget setup` to configure policy routing
- Run `turboget verify` to confirm routing rules are active

### "Server does not support Range requests"
- The server doesn't allow partial downloads — TurboGet can't split the file
- Use `--fallback` to download on the fastest interface without splitting
- Try a mirror that supports Range requests

### Policy routing survives reboot?
No — `ip rule` entries are not persistent. Add `sudo turboget setup` to your
startup script, or add the equivalent `ip rule`/`ip route` commands to `/etc/rc.local`.

### Only getting speed from one interface
Check that all interfaces have a default route: `ip route show`. If two interfaces
share the same gateway (e.g., both go through your home router), they are on the
same upstream link and cannot aggregate bandwidth.

---

## Architecture

```
turboget/
├── cli.py          — Click commands and download orchestration
├── config.py       — JSON config loading/saving
├── interface.py    — NIC detection and Linux policy routing
├── checker.py      — HTTP HEAD for file info and range support
├── scheduler.py    — Chunk creation, work-stealing queue, resume state
├── downloader.py   — Async aiohttp workers (one per NIC)
├── writer.py       — Pre-allocated file, seek+write at byte offsets
├── progress.py     — Rich live terminal display
├── server.py       — FastAPI backend for UI and live updates
└── ui/index.html   — Single-page Glassmorphism web dashboard
```

---

## License

MIT License — see [LICENSE](LICENSE).

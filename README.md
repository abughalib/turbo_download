# ⚡ TurboGet

**Multi-WAN parallel download manager for Linux.**

TurboGet splits a file into chunks and downloads each chunk through a **different internet connection** simultaneously — your home broadband, Airtel USB tethering, and Jio hotspot all working at once.

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

Most download managers open multiple connections to the same server — but all through the **same NIC**. TurboGet is different:

| Feature | TurboGet | aria2 / wget |
|---|---|---|
| Uses multiple internet connections | ✅ | ❌ |
| Linux policy routing (guaranteed NIC binding) | ✅ | ❌ |
| Distributes data cap across ISPs | ✅ | ❌ |
| Work-stealing chunk scheduler | ✅ | — |
| Resume interrupted downloads | ✅ | ✅ |
| No external proxy required | ✅ | — |

**Primary use case**: You have Jio + Airtel + home broadband. Each has a data cap. TurboGet downloads 1/3 of the file from each ISP, tripling your effective cap and potentially tripling your speed.

---

## How It Works

### 1. HTTP Range Requests
Most file servers support `Range: bytes=X-Y` headers. TurboGet splits the file into chunks and assigns each chunk to a different interface.

### 2. Linux Policy Routing (the key insight)
Simply binding a socket to a local IP is not enough — the kernel's default route table is destination-based and may still route packets via the wrong NIC.

TurboGet creates a **private routing table per interface**:

```bash
# Per-interface routing table (e.g., for wlan0 at 192.168.43.156)
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

### Install with pip

```bash
pip install turboget
```

### Install from source

```bash
git clone https://github.com/yourusername/turboget
cd turboget
pip install -e .
```

### Install with uv (recommended for development)

```bash
uv sync
uv run turboget --help
```

---

## Quick Start

### 1. Configure your interfaces

```bash
# Auto-detect and write config
turboget config detect --write

# Or edit config.json manually:
```

```json
{
    "interfaces": [
        {
            "name": "enp4s0",
            "alias": "Home Fiber",
            "metric": 100,
            "enabled": true
        },
        {
            "name": "enxc03eba3bda6f",
            "alias": "Jio USB",
            "metric": 200,
            "enabled": true
        },
        {
            "name": "wlan0",
            "alias": "Airtel Hotspot",
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

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Network Interfaces                              │
├──────────────────┬───────────────┬──────────────────┬───────┬───────┤
│ Interface        │ Alias         │ Local IP         │ Net   │ Route │
├──────────────────┼───────────────┼──────────────────┼───────┼───────┤
│ enp4s0           │ Home Fiber    │ 192.168.1.105    │ ✓     │ ✓     │
│ enxc03eba3bda6f  │ Jio USB       │ 192.168.42.87    │ ✓     │ ✓     │
│ wlan0            │ Airtel Hotspot│ 192.168.43.156   │ ✓     │ ✓     │
└──────────────────┴───────────────┴──────────────────┴───────┴───────┘
✓ All routing rules in place.
```

### 4. Download!

```bash
turboget download https://example.com/bigfile.iso
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
  verify    Check interfaces, internet connectivity, and routing status.
  setup     Configure Linux policy routing (requires sudo).
  cleanup   Remove TurboGet policy routing rules (requires sudo).
  config    View and manage TurboGet configuration.
```

### download

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

### config detect

```bash
# Show detected interfaces (no changes made)
turboget config detect

# Auto-write to config.json
turboget config detect --write
```

---

## Configuration Reference

| Field | Default | Description |
|---|---|---|
| `interfaces[].name` | — | Kernel interface name (e.g. `wlan0`, `enp4s0`) |
| `interfaces[].alias` | — | Human-readable label shown in the UI |
| `interfaces[].metric` | `200` | Lower = higher priority for browsing (via NetworkManager) |
| `interfaces[].enabled` | `true` | Set to `false` to skip an interface without removing it |
| `chunks_per_interface` | `8` | Chunks assigned per interface (more = better work-stealing granularity) |
| `max_retries` | `3` | Times a failed chunk is retried before giving up |
| `connect_timeout` | `30` | TCP connect timeout in seconds |
| `read_timeout` | `120` | Per-block read timeout in seconds |
| `output_dir` | `"."` | Default directory for downloaded files |

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
└── progress.py     — Rich live terminal display
```

---

## License

MIT License — see [LICENSE](LICENSE).

## Contributing

Pull requests welcome! Particularly interested in:
- macOS support (routing via `route add` / `pfctl`)
- Speed test before chunk distribution
- GUI frontend
- Per-interface data usage tracking

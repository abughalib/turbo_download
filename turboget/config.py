"""
Configuration management for TurboGet.

Loads and saves config.json. Supports auto-detection of interfaces.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InterfaceConfig:
    name: str
    alias: str
    metric: int = 200
    enabled: bool = True


@dataclass
class TurboConfig:
    interfaces: list[InterfaceConfig] = field(default_factory=list)
    chunks_per_interface: int = 8
    max_retries: int = 3
    connect_timeout: int = 30
    read_timeout: int = 120
    output_dir: str = "."


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_config(path: Path | str | None = None) -> TurboConfig:
    """Load config from JSON file. Falls back to defaults if not found."""
    if path is None:
        path = _find_config()

    if path is None or not Path(path).exists():
        return TurboConfig()

    path = Path(path)
    try:
        with open(path) as f:
            data = json.load(f)

        interfaces = [
            InterfaceConfig(
                name=i["name"],
                alias=i.get("alias", i["name"]),
                metric=i.get("metric", 200),
                enabled=i.get("enabled", True),
            )
            for i in data.get("interfaces", [])
        ]

        return TurboConfig(
            interfaces=interfaces,
            chunks_per_interface=data.get("chunks_per_interface", 8),
            max_retries=data.get("max_retries", 3),
            connect_timeout=data.get("connect_timeout", 30),
            read_timeout=data.get("read_timeout", 120),
            output_dir=data.get("output_dir", "."),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"[!] Error reading config at {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def save_config(config: TurboConfig, path: Path | str) -> None:
    """Save config to JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "interfaces": [
            {
                "name": i.name,
                "alias": i.alias,
                "metric": i.metric,
                "enabled": i.enabled,
            }
            for i in config.interfaces
        ],
        "chunks_per_interface": config.chunks_per_interface,
        "max_retries": config.max_retries,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
        "output_dir": config.output_dir,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_config() -> Optional[Path]:
    """Look for config.json in standard locations."""
    candidates = [
        Path.cwd() / "config.json",
        Path.home() / ".config" / "turboget" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def get_default_config_path() -> Path:
    """Return the preferred config path (may not exist yet)."""
    found = _find_config()
    if found:
        return found
    return Path.cwd() / "config.json"

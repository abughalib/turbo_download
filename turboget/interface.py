"""
Network interface detection and Linux policy routing management.

Policy routing ensures each download socket is physically forced out
through the correct NIC, regardless of the kernel's default route.

How it works
------------
1. Each active interface gets a private routing table (ID 101-199).
2. That table has: subnet route + default via the interface's gateway.
3. An `ip rule` entry says: "packets whose source IP is <iface_ip>
   must use routing table <N>."
4. aiohttp's TCPConnector binds sockets to the interface's local IP,
   so the kernel applies the correct routing table automatically.

This requires `sudo turboget setup` to be run once per boot.
"""
from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Routing table IDs 101-199 (100 is local, 200 is main, 253 is default)
ROUTING_TABLE_BASE = 100
# ip rule priority range (won't conflict with NetworkManager's range 0-99)
ROUTING_PRIORITY_BASE = 2000


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class NetworkInterface:
    name: str            # kernel name, e.g. "wlan0"
    alias: str           # human name, e.g. "WiFi Hotspot"
    local_ip: str        # "192.168.43.156"
    gateway: str         # "192.168.43.1"
    table_id: int        # policy routing table id
    enabled: bool = True
    reachable: bool = False

    @property
    def priority(self) -> int:
        """ip rule priority for this interface (unique per table)."""
        return ROUTING_PRIORITY_BASE + (self.table_id - ROUTING_TABLE_BASE) * 10


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def get_ip_info(iface_name: str) -> tuple[Optional[str], Optional[str]]:
    """Return (local_ip, gateway) for the given interface name."""
    local_ip: Optional[str] = None
    gateway: Optional[str] = None

    # Local IP
    r = _run(["ip", "-4", "addr", "show", iface_name])
    if r.returncode == 0:
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
        if m:
            local_ip = m.group(1)

    if not local_ip:
        return None, None

    # Gateway — search the global route table for this device
    r = _run(["ip", "-4", "route", "show"])
    if r.returncode == 0:
        m = re.search(
            rf"default via (\d+\.\d+\.\d+\.\d+)\s+dev\s+{re.escape(iface_name)}",
            r.stdout,
        )
        if m:
            gateway = m.group(1)

    return local_ip, gateway


def get_subnet_route(iface_name: str) -> Optional[str]:
    """Return the kernel subnet route for an interface, e.g. '192.168.43.0/24'."""
    r = _run(["ip", "-4", "route", "show", "dev", iface_name])
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "proto kernel" in line or "scope link" in line:
            m = re.match(r"(\d+\.\d+\.\d+\.\d+/\d+)", line)
            if m:
                return m.group(1)
    return None


def check_connectivity(local_ip: str, host: str = "8.8.8.8",
                       port: int = 53, timeout: float = 5.0) -> bool:
    """TCP-connect from a specific local IP to verify real internet access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.bind((local_ip, 0))
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# High-level interface detection
# ---------------------------------------------------------------------------

def detect_interfaces(config) -> list[NetworkInterface]:
    """
    Resolve active interfaces from config.json.
    Assigns routing table IDs in order of appearance.
    """
    interfaces: list[NetworkInterface] = []

    for idx, iface_conf in enumerate(config.interfaces):
        if not iface_conf.enabled:
            continue

        local_ip, gateway = get_ip_info(iface_conf.name)
        if not local_ip:
            continue  # Interface is down

        iface = NetworkInterface(
            name=iface_conf.name,
            alias=iface_conf.alias,
            local_ip=local_ip,
            gateway=gateway or "",
            table_id=ROUTING_TABLE_BASE + idx + 1,
        )
        iface.reachable = check_connectivity(local_ip)
        interfaces.append(iface)

    return interfaces


def auto_detect_interfaces() -> list[dict]:
    """
    Scan all network interfaces and return those with internet connectivity.
    Used by `turboget config detect`.
    """
    r = _run(["ip", "-4", "addr"])
    if r.returncode != 0:
        return []

    found: list[dict] = []
    current: Optional[str] = None

    for line in r.stdout.splitlines():
        m = re.match(r"^\d+:\s+([\w@]+):", line)
        if m:
            name = m.group(1).split("@")[0]  # strip vlan suffix
            current = None if name == "lo" else name
            continue

        if current:
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/\d+", line)
            if m:
                ip = m.group(1)
                _, gw = get_ip_info(current)
                if gw and check_connectivity(ip):
                    found.append({"name": current, "ip": ip, "gateway": gw})
                current = None  # one IP per interface is enough

    return found


# ---------------------------------------------------------------------------
# Policy routing
# ---------------------------------------------------------------------------

def setup_policy_routing(interfaces: list[NetworkInterface]) -> None:
    """
    Create per-interface routing tables and ip rules.
    REQUIRES ROOT — run via `sudo turboget setup`.
    """
    for iface in interfaces:
        if not iface.gateway:
            continue

        subnet = get_subnet_route(iface.name)

        # Subnet route inside the interface's private table
        if subnet:
            _run([
                "ip", "route", "add", subnet,
                "dev", iface.name, "src", iface.local_ip,
                "table", str(iface.table_id),
            ])

        # Default gateway inside the interface's private table
        _run([
            "ip", "route", "add", "default",
            "via", iface.gateway, "dev", iface.name,
            "table", str(iface.table_id),
        ])

        # Rule: from <this IP> → look up <this table>
        _run([
            "ip", "rule", "add",
            "from", iface.local_ip,
            "table", str(iface.table_id),
            "priority", str(iface.priority),
        ])


def teardown_policy_routing(interfaces: list[NetworkInterface]) -> None:
    """
    Remove policy routing rules and flush routing tables.
    REQUIRES ROOT — run via `sudo turboget cleanup`.
    """
    for iface in interfaces:
        if not iface.gateway:
            continue

        # Delete rule
        _run([
            "ip", "rule", "del",
            "from", iface.local_ip,
            "table", str(iface.table_id),
        ])

        # Flush the private routing table
        _run(["ip", "route", "flush", "table", str(iface.table_id)])


def check_routing_setup(interfaces: list[NetworkInterface]) -> dict[str, bool]:
    """
    Return {interface_name: routing_is_configured} for each interface.
    Used by `turboget verify`.
    """
    r = _run(["ip", "rule", "show"])
    rules = r.stdout if r.returncode == 0 else ""

    result: dict[str, bool] = {}
    for iface in interfaces:
        pattern = rf"from {re.escape(iface.local_ip)}\s+lookup\s+{iface.table_id}"
        result[iface.name] = bool(re.search(pattern, rules))
    return result

#!/usr/bin/env python3
import os
import subprocess
import json
import re
import sys
import argparse

# --- UTILITIES ---
def load_config(path="config.json"):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[-] Error: '{path}' not found.")
        sys.exit(1)

def get_ip_info(interface):
    """Returns (Local_IP, Gateway_IP) for a given interface."""
    try:
        # Get Local IP
        ip_cmd = f"ip -4 addr show {interface}"
        output = subprocess.check_output(ip_cmd.split(), stderr=subprocess.DEVNULL).decode()
        local_ip = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", output).group(1)

        # Get Gateway
        route_cmd = "ip route"
        routes = subprocess.check_output(route_cmd.split(), stderr=subprocess.DEVNULL).decode()
        
        # FIXED: Added 'r' before the string to fix SyntaxWarning
        # Regex to find default gateway for specific device
        gateway = re.search(rf"default via (\d+\.\d+\.\d+\.\d+) dev {interface}", routes).group(1)
        return local_ip, gateway
    except (subprocess.CalledProcessError, AttributeError):
        return None, None

def check_proxy_port(host, port):
    """Checks if a TCP port is open."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect((host, int(port)))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError):
        return False
    except Exception as e:
        print(f"[-] Error checking port: {e}")
        return False

# --- COMMANDS ---
def apply_metrics(config):
    """Configures NetworkManager priorities based on config.json"""
    print("[*] Applying Network Metrics (Prioritizing Browsing)...")
    
    for iface in config['interfaces']:
        if not iface['enabled']:
            continue
            
        print(f"    -> Setting {iface['alias']} ({iface['name']}) to Metric {iface['metric']}")
        
        try:
            nm_out = subprocess.check_output(["nmcli", "-t", "-f", "NAME,DEVICE", "con", "show"]).decode()
            con_name = None
            for line in nm_out.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == iface['name']:
                    con_name = parts[0]
                    break
            
            if con_name:
                subprocess.run(["sudo", "nmcli", "connection", "modify", con_name, "ipv4.route-metric", str(iface['metric'])], check=True)
                # Bringing up connection to apply changes
                subprocess.run(["sudo", "nmcli", "connection", "up", con_name], stdout=subprocess.DEVNULL)
            else:
                print(f"    [-] Warning: No active connection profile found for {iface['name']}")

        except subprocess.CalledProcessError as e:
            print(f"    [-] Error configuring {iface['name']}: {e}")

    print("[+] Metrics applied successfully.")

def start_proxy(config):
    """Detects IPs and starts dispatch-proxy (Rust Version)"""
    
    # 1. Check if port is already in use
    if check_proxy_port("127.0.0.1", config['proxy_port']):
        print(f"[!] Port {config['proxy_port']} is already in use. Is TurboNet already running?")
        print("    If yes, you can proceed to download.")
        print("    If no, please kill the process using this port.")
        sys.exit(1)

    # 2. Basic command structure
    # Note: We use --ip to bind the server to localhost (standard for Rust version)
    dispatch_args = ["dispatch", "start", "--ip", "127.0.0.1", "--port", str(config['proxy_port'])]
    active_count = 0

    print("[*] Detecting Interfaces...")
    
    for iface in config['interfaces']:
        if not iface['enabled']:
            continue
            
        # We still check if it has an IP to ensure it's actually UP
        local_ip, gateway = get_ip_info(iface['name'])
        
        if local_ip:
            print(f"    [+] {iface['alias']}: {local_ip} (Active)")
            
            # FIXED: Rust version expects just the INTERFACE NAME or IP.
            # We pass the interface name (e.g., 'enp4s0') which is more robust.
            dispatch_args.append(iface['name'])
            
            active_count += 1
        else:
            print(f"    [-] {iface['alias']}: DOWN or Disconnected")

    if active_count < 2:
        print("\n[!] WARNING: Less than 2 interfaces are active. Speed boosting will not work effectively.")
    
    print(f"\n[*] Starting Turbo Proxy (Rust Engine) on 127.0.0.1:{config['proxy_port']}")
    print("[*] Press Ctrl+C to stop.")
    
    try:
        subprocess.run(dispatch_args)
    except KeyboardInterrupt:
        print("\n[*] Proxy stopped.")

def download_file(config, url):
    """Launches aria2c with proxychains using the correct local config"""
    
    # 0. Pre-flight Check
    if not check_proxy_port("127.0.0.1", config['proxy_port']):
        print(f"[-] Error: Turbo Proxy is NOT running on port {config['proxy_port']}.")
        print("    Please run: python3 turbo_net.py start")
        sys.exit(1)

    # FIXED: Removed 'proxy_dns'. This forces aria2 to resolve DNS locally.
    # This prevents IPv6 leakage which was causing your socket errors.
    pc_conf_content = f"""
strict_chain
tcp_read_time_out 15000
tcp_connect_time_out 10000 
[ProxyList]
socks5 127.0.0.1 {config['proxy_port']}
"""
    
    conf_path = "turbo_pc.conf"
    with open(conf_path, "w") as f:
        f.write(pc_conf_content)
        
    print(f"[*] Generated local proxy config: {conf_path}")
    print(f"[*] Starting Download via Turbo Tunnel (Port {config['proxy_port']})...")

    # 2. Prepare the environment
    env = os.environ.copy()
    env["PROXYCHAINS_CONF_FILE"] = conf_path

    # 3. The Command - FORCE IPV4
    cmd = [
        "proxychains4",
        "aria2c",
        "--disable-ipv6",       # CRITICAL: Forces IPv4
        "-x", "8",              # Reduced to 8 for stability
        "-s", "8", 
        "-k", "1M",             # 1MB chunks helps balance unstable 5G
        "--connect-timeout=30",
        "--max-tries=5",
        "--retry-wait=3",
        url
    ]

    try:
        subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        print("\n[*] Download Interrupted.")
    finally:
        if os.path.exists(conf_path):
            os.remove(conf_path)

def verify_system(config):
    """Checks system status and dependencies"""
    print("[*] Verifying System Status...")
    
    # 1. Check Dependencies
    dependencies = ["dispatch", "aria2c", "proxychains4", "nmcli"]
    for dep in dependencies:
        path = shutil.which(dep)
        status = f"OK ({path})" if path else "MISSING"
        print(f"    [{'OK' if path else '!!'}] {dep}: {status}")

    # 2. Check Interfaces
    print("\n[*] Checking Interfaces:")
    for iface in config['interfaces']:
        local_ip, _ = get_ip_info(iface['name'])
        status = f"Active ({local_ip})" if local_ip else "DOWN"
        print(f"    - {iface['alias']} ({iface['name']}): {status}")

    # 3. Check Proxy
    proxy_status = check_proxy_port("127.0.0.1", config['proxy_port'])
    print(f"\n[*] Proxy Status: {'RUNNING' if proxy_status else 'STOPPED'}")
    if proxy_status:
        print(f"    -> Listening on 127.0.0.1:{config['proxy_port']}")
    else:
        print(f"    -> Port {config['proxy_port']} is free.")

# --- MAIN ---
if __name__ == "__main__":
    import shutil # Imported here for verify_system
    parser = argparse.ArgumentParser(description="TurboNet: Multi-WAN Manager")
    subparsers = parser.add_subparsers(dest='mode', help="Mode of operation")
    
    # Setup mode
    subparsers.add_parser('setup', help="Configure OS priorities")
    
    # Start mode
    subparsers.add_parser('start', help="Start the Proxy Server")
    
    # Verify mode
    subparsers.add_parser('verify', help="Check system status")
    
    # Download mode
    dl_parser = subparsers.add_parser('download', help="Download a file using TurboNet")
    dl_parser.add_argument('url', help="URL of the file to download")

    args = parser.parse_args()
    conf = load_config()
    
    if args.mode == 'setup':
        apply_metrics(conf)
    elif args.mode == 'start':
        start_proxy(conf)
    elif args.mode == 'verify':
        verify_system(conf)
    elif args.mode == 'download':
        download_file(conf, args.url)
    else:
        parser.print_help()
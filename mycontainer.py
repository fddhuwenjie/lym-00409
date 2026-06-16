#!/usr/bin/env python3
import argparse
import fcntl
import io
import json
import os
import resource
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

MYCONTAINER_HOME = Path(os.environ.get("MYCONTAINER_HOME", Path.home() / ".mycontainer"))
CONTAINERS_DIR = MYCONTAINER_HOME / "containers"
IMAGES_DIR = MYCONTAINER_HOME / "images"
LAYERS_DIR = Path(os.environ.get("MYCONTAINER_LAYERS", Path(__file__).parent / "layers"))
NET_DIR = MYCONTAINER_HOME / "net"
BRIDGE_SOCK = NET_DIR / "bridge.sock"
IPAM_STATE = NET_DIR / "ipam.json"

SUBNET_BASE = "172.17.0"
SUBNET_MASK = 16
BRIDGE_IP = f"{SUBNET_BASE}.1"


def ensure_dirs():
    for d in [MYCONTAINER_HOME, CONTAINERS_DIR, IMAGES_DIR, LAYERS_DIR, NET_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ============================================================
# IP Address Management (IPAM)
# ============================================================

def _ip_to_int(ip_str):
    parts = ip_str.split(".")
    return (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(parts[3])


def _int_to_ip(ip_int):
    return f"{(ip_int >> 24) & 0xFF}.{(ip_int >> 16) & 0xFF}.{(ip_int >> 8) & 0xFF}.{ip_int & 0xFF}"


def load_ipam_state():
    if not IPAM_STATE.exists():
        return {"used": {BRIDGE_IP: "bridge"}, "last_allocated": _ip_to_int(BRIDGE_IP)}
    with open(IPAM_STATE, "r") as f:
        return json.load(f)


def save_ipam_state(state):
    with open(IPAM_STATE, "w") as f:
        json.dump(state, f, indent=2)


def allocate_ip(container_id):
    ensure_dirs()
    state = load_ipam_state()
    used = state["used"]
    if container_id in used.values():
        for ip, cid in used.items():
            if cid == container_id:
                return ip
    start = state.get("last_allocated", _ip_to_int(BRIDGE_IP))
    current = start + 1
    while True:
        if current > _ip_to_int(f"{SUBNET_BASE}.254"):
            current = _ip_to_int(BRIDGE_IP) + 1
        ip = _int_to_ip(current)
        if ip not in used:
            used[ip] = container_id
            state["last_allocated"] = current
            save_ipam_state(state)
            return ip
        if current == start:
            raise RuntimeError("No available IP addresses in subnet")
        current += 1


def release_ip(container_id):
    ensure_dirs()
    state = load_ipam_state()
    used = state["used"]
    to_remove = [ip for ip, cid in used.items() if cid == container_id]
    for ip in to_remove:
        del used[ip]
    save_ipam_state(state)


def get_container_ip(container_id):
    state = load_ipam_state()
    for ip, cid in state["used"].items():
        if cid == container_id:
            return ip
    return None


def find_container_by_ip(ip):
    state = load_ipam_state()
    return state["used"].get(ip)


# ============================================================
# Virtual Bridge (Unix Domain Socket based)
# ============================================================

BRIDGE_INSTANCE = None


class VirtualBridge:
    def __init__(self):
        self.clients = {}
        self.lock = threading.Lock()
        self.sock_path = str(BRIDGE_SOCK)
        self._running = False
        self._server_thread = None
        self.port_mappings = {}

    def start(self):
        if self._running:
            return
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._running = True
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        time.sleep(0.1)

    def stop(self):
        self._running = False
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def _serve(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.sock_path)
        server.listen(128)
        server.settimeout(0.5)
        while self._running:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()
        server.close()

    def _handle_client(self, conn):
        container_ip = None
        try:
            hello = conn.recv(4096).decode("utf-8").strip()
            if hello.startswith("HELLO "):
                container_ip = hello.split(" ", 1)[1]
                with self.lock:
                    if container_ip not in self.clients:
                        self.clients[container_ip] = []
                    self.clients[container_ip].append(conn)
            buf = b""
            while self._running:
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        pkt = json.loads(line.decode("utf-8"))
                        self._route_packet(pkt, container_ip)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
        finally:
            if container_ip:
                with self.lock:
                    if container_ip in self.clients:
                        try:
                            self.clients[container_ip].remove(conn)
                        except ValueError:
                            pass
                        if not self.clients[container_ip]:
                            del self.clients[container_ip]
            try:
                conn.close()
            except OSError:
                pass

    def _route_packet(self, pkt, from_ip):
        dst = pkt.get("dst_ip")
        if not dst:
            return
        pkt["src_ip"] = from_ip
        if dst == BRIDGE_IP:
            self._deliver_to_host(pkt)
            return
        self._send_to_ip(dst, pkt)

    def _send_to_ip(self, ip, pkt):
        with self.lock:
            conns = list(self.clients.get(ip, []))
        for conn in conns:
            try:
                conn.sendall(json.dumps(pkt).encode("utf-8") + b"\n")
            except OSError:
                pass

    def _deliver_to_host(self, pkt):
        self._send_to_ip(BRIDGE_IP, pkt)

    def send_to_container(self, container_ip, pkt):
        with self.lock:
            conns = list(self.clients.get(container_ip, []))
        if conns:
            for conn in conns:
                try:
                    conn.sendall(json.dumps(pkt).encode("utf-8") + b"\n")
                except OSError:
                    pass
            return True
        return False

    def register_port_mapping(self, host_port, container_ip, container_port):
        with self.lock:
            self.port_mappings[int(host_port)] = {
                "container_ip": container_ip,
                "container_port": int(container_port),
            }

    def unregister_port_mapping(self, host_port):
        with self.lock:
            self.port_mappings.pop(int(host_port), None)


def get_bridge():
    global BRIDGE_INSTANCE
    if BRIDGE_INSTANCE is None:
        BRIDGE_INSTANCE = VirtualBridge()
    return BRIDGE_INSTANCE


# ============================================================
# User-space TCP/IP Stack for Containers
# ============================================================

class VirtualNetworkStack:
    def __init__(self, container_ip, container_id):
        self.container_ip = container_ip
        self.container_id = container_id
        self.sock = None
        self._listeners = {}
        self._connections = {}
        self._next_conn_id = 1
        self._recv_buffers = {}
        self._lock = threading.Lock()
        self._running = False
        self._recv_thread = None

    def connect_bridge(self):
        ensure_dirs()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(BRIDGE_SOCK))
        sock.sendall(f"HELLO {self.container_ip}".encode("utf-8"))
        self.sock = sock
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def close(self):
        self._running = False
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _recv_loop(self):
        sock = self.sock
        sock.settimeout(0.5)
        buf = b""
        while self._running:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    pkt = json.loads(line.decode("utf-8"))
                    self._handle_packet(pkt)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    def _handle_packet(self, pkt):
        pkt_type = pkt.get("type")
        src_ip = pkt.get("src_ip")
        src_port = pkt.get("src_port")
        dst_port = pkt.get("dst_port")
        conn_id = pkt.get("conn_id")
        payload = pkt.get("payload", "")

        if pkt_type == "TCP_SYN":
            listener = self._listeners.get(dst_port)
            if listener is not None:
                new_conn_id = self._next_conn_id
                self._next_conn_id += 1
                key = (src_ip, src_port, dst_port, new_conn_id)
                self._connections[key] = {
                    "state": "ESTABLISHED",
                    "remote_ip": src_ip,
                    "remote_port": src_port,
                    "local_port": dst_port,
                    "peer_conn_id": conn_id,
                    "conn_id": new_conn_id,
                }
                self._recv_buffers[key] = b""
                listener["queue"].append(key)
                listener["event"].set()
                self._send_packet({
                    "type": "TCP_SYNACK",
                    "dst_ip": src_ip,
                    "dst_port": src_port,
                    "src_port": dst_port,
                    "conn_id": new_conn_id,
                    "peer_conn_id": conn_id,
                })

        elif pkt_type == "TCP_SYNACK":
            for key, conn in list(self._connections.items()):
                if (conn["state"] == "SYN_SENT" and
                        conn["peer_conn_id"] is None and
                        conn["remote_ip"] == src_ip and
                        conn["remote_port"] == src_port):
                    conn["state"] = "ESTABLISHED"
                    conn["peer_conn_id"] = conn_id
                    conn["conn_id_ack"] = pkt.get("peer_conn_id")
                    self._recv_buffers[key] = b""
                    conn["event"].set()
                    break

        elif pkt_type == "TCP_DATA":
            for key, conn in list(self._connections.items()):
                if (conn["state"] == "ESTABLISHED" and
                        conn["remote_ip"] == src_ip and
                        conn["conn_id"] == conn_id):
                    self._recv_buffers[key] += payload.encode("utf-8", errors="replace")
                    conn["event"].set()
                    break

        elif pkt_type == "TCP_FIN":
            for key, conn in list(self._connections.items()):
                if (conn["state"] == "ESTABLISHED" and
                        conn["remote_ip"] == src_ip and
                        conn["conn_id"] == conn_id):
                    conn["state"] = "CLOSE_WAIT"
                    conn["event"].set()
                    break

        elif pkt_type == "TCP_ACK":
            for key, conn in list(self._connections.items()):
                if (conn.get("remote_ip") == src_ip and
                        conn.get("conn_id") == conn_id):
                    conn["acked"] = True
                    conn["event"].set()
                    break

    def _send_packet(self, pkt):
        if self.sock:
            try:
                self.sock.sendall(json.dumps(pkt).encode("utf-8") + b"\n")
            except OSError:
                pass

    def vbind(self, port):
        with self._lock:
            if port in self._listeners:
                raise OSError(f"Port {port} already bound")
            self._listeners[port] = {
                "queue": [],
                "event": threading.Event(),
            }
            return port

    def vlisten(self, port):
        return

    def vaccept(self, port, timeout=None):
        listener = self._listeners.get(port)
        if listener is None:
            raise OSError(f"Port {port} not bound")
        deadline = None if timeout is None else time.time() + timeout
        while True:
            with self._lock:
                if listener["queue"]:
                    key = listener["queue"].pop(0)
                    conn = self._connections[key]
                    return VirtualConnection(self, key, conn)
            remaining = None
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
            listener["event"].wait(timeout=remaining)
            listener["event"].clear()

    def vconnect(self, dst_ip, dst_port, timeout=10):
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        local_port = 49152 + (conn_id % 16383)
        event = threading.Event()
        key = (dst_ip, dst_port, local_port, conn_id)
        self._connections[key] = {
            "state": "SYN_SENT",
            "remote_ip": dst_ip,
            "remote_port": dst_port,
            "local_port": local_port,
            "conn_id": conn_id,
            "peer_conn_id": None,
            "event": event,
        }
        self._send_packet({
            "type": "TCP_SYN",
            "dst_ip": dst_ip,
            "dst_port": dst_port,
            "src_port": local_port,
            "conn_id": conn_id,
        })
        if not event.wait(timeout):
            del self._connections[key]
            raise OSError("Connection timed out")
        conn = self._connections[key]
        if conn["state"] != "ESTABLISHED":
            del self._connections[key]
            raise OSError("Connection refused")
        self._recv_buffers[key] = b""
        return VirtualConnection(self, key, conn)

    def vrecv(self, key, maxlen=4096, timeout=None):
        conn = self._connections.get(key)
        if conn is None:
            raise OSError("Connection closed")
        deadline = None if timeout is None else time.time() + timeout
        while True:
            data = self._recv_buffers.get(key, b"")
            if data:
                take = data[:maxlen]
                self._recv_buffers[key] = data[maxlen:]
                return take
            if conn["state"] in ("CLOSE_WAIT", "CLOSED"):
                return b""
            remaining = None
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return b""
            ev = conn.get("event")
            if ev:
                ev.wait(timeout=remaining)
                ev.clear()
            else:
                time.sleep(0.05)

    def vsend(self, key, data):
        conn = self._connections.get(key)
        if conn is None or conn["state"] != "ESTABLISHED":
            raise OSError("Connection closed")
        encoded = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        self._send_packet({
            "type": "TCP_DATA",
            "dst_ip": conn["remote_ip"],
            "dst_port": conn["remote_port"],
            "src_port": conn["local_port"],
            "conn_id": conn.get("peer_conn_id"),
            "payload": encoded,
        })
        return len(data)

    def vclose(self, key):
        conn = self._connections.get(key)
        if conn is None:
            return
        if conn["state"] == "ESTABLISHED":
            self._send_packet({
                "type": "TCP_FIN",
                "dst_ip": conn["remote_ip"],
                "dst_port": conn["remote_port"],
                "src_port": conn["local_port"],
                "conn_id": conn.get("peer_conn_id"),
            })
            conn["state"] = "FIN_WAIT"
        self._connections.pop(key, None)
        self._recv_buffers.pop(key, None)


class VirtualConnection:
    def __init__(self, stack, key, info):
        self._stack = stack
        self._key = key
        self._info = info

    @property
    def remote_ip(self):
        return self._info["remote_ip"]

    @property
    def remote_port(self):
        return self._info["remote_port"]

    @property
    def local_port(self):
        return self._info["local_port"]

    def recv(self, maxlen=4096, timeout=None):
        return self._stack.vrecv(self._key, maxlen, timeout)

    def send(self, data):
        return self._stack.vsend(self._key, data)

    def close(self):
        self._stack.vclose(self._key)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


NET_STACK_INSTANCE = None


def get_net_stack():
    return NET_STACK_INSTANCE


# ============================================================
# Port Mapping Proxy
# ============================================================

class PortMappingProxy:
    def __init__(self, host_port, container_ip, container_port, bridge):
        self.host_port = int(host_port)
        self.container_ip = container_ip
        self.container_port = int(container_port)
        self.bridge = bridge
        self._sock = None
        self._running = False
        self._thread = None
        self._conns = {}
        self._next_id = 1

    def start(self):
        ensure_dirs()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind(("127.0.0.1", self.host_port))
        except OSError as e:
            print(f"Warning: Could not bind port {self.host_port}: {e}", file=sys.stderr)
            return False
        self._sock.listen(128)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self.bridge.register_port_mapping(self.host_port, self.container_ip, self.container_port)
        self._bridge_listener = threading.Thread(target=self._bridge_listener_loop, daemon=True)
        self._bridge_listener.start()
        return True

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        self.bridge.unregister_port_mapping(self.host_port)

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            cid = self._next_id
            self._next_id += 1
            self._conns[cid] = {"host_conn": conn, "state": "NEW", "buffered": b""}
            t = threading.Thread(target=self._handle_host_conn, args=(cid, conn), daemon=True)
            t.start()

    def _handle_host_conn(self, cid, conn):
        try:
            conn_id = 100000 + cid
            local_port = 40000 + cid
            self.bridge.send_to_container(self.container_ip, {
                "type": "TCP_SYN",
                "dst_ip": self.container_ip,
                "dst_port": self.container_port,
                "src_port": local_port,
                "conn_id": conn_id,
                "src_ip": BRIDGE_IP,
            })
            info = self._conns[cid]
            info["bridge_conn_id"] = None
            info["local_port"] = local_port
            info["conn_id"] = conn_id
            info["state"] = "SYN_SENT"
            start = time.time()
            while time.time() - start < 10:
                if info.get("bridge_conn_id") is not None:
                    break
                time.sleep(0.05)
            if info.get("bridge_conn_id") is None:
                try:
                    conn.close()
                except OSError:
                    pass
                return
            info["state"] = "ESTABLISHED"
            conn.settimeout(0.1)
            while self._running:
                try:
                    data = conn.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.bridge.send_to_container(self.container_ip, {
                    "type": "TCP_DATA",
                    "dst_ip": self.container_ip,
                    "dst_port": self.container_port,
                    "src_port": local_port,
                    "conn_id": info["bridge_conn_id"],
                    "src_ip": BRIDGE_IP,
                    "payload": data.decode("utf-8", errors="replace"),
                })
            self.bridge.send_to_container(self.container_ip, {
                "type": "TCP_FIN",
                "dst_ip": self.container_ip,
                "dst_port": self.container_port,
                "src_port": local_port,
                "conn_id": info["bridge_conn_id"],
                "src_ip": BRIDGE_IP,
            })
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._conns.pop(cid, None)

    def _bridge_listener_loop(self):
        check_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            check_sock.connect(str(BRIDGE_SOCK))
            check_sock.sendall(f"HELLO {BRIDGE_IP}".encode("utf-8"))
        except OSError:
            try:
                check_sock.close()
            except OSError:
                pass
            return
        check_sock.settimeout(0.5)
        buf = b""
        while self._running:
            try:
                data = check_sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    pkt = json.loads(line.decode("utf-8"))
                    self._handle_bridge_pkt(pkt)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        try:
            check_sock.close()
        except OSError:
            pass

    def _handle_bridge_pkt(self, pkt):
        src_ip = pkt.get("src_ip")
        src_port = pkt.get("src_port")
        dst_port = pkt.get("dst_port")
        conn_id = pkt.get("conn_id")
        peer_conn_id = pkt.get("peer_conn_id")
        ptype = pkt.get("type")
        if src_ip != self.container_ip or src_port != self.container_port:
            return
        info = None
        for c in self._conns.values():
            if c.get("state") in ("SYN_SENT", "ESTABLISHED"):
                if peer_conn_id is not None and c.get("conn_id") == peer_conn_id:
                    info = c
                    break
                if conn_id is not None and c.get("conn_id") == conn_id:
                    info = c
                    break
                if conn_id is not None and c.get("bridge_conn_id") == conn_id:
                    info = c
                    break
        if info is None:
            for c in self._conns.values():
                if peer_conn_id is not None and c.get("conn_id") == peer_conn_id:
                    info = c
                    break
                if conn_id is not None and c.get("conn_id") == conn_id:
                    info = c
                    break
                if conn_id is not None and c.get("bridge_conn_id") == conn_id:
                    info = c
                    break
        if info is None:
            return
        if ptype == "TCP_SYNACK":
            info["bridge_conn_id"] = conn_id
            info["state"] = "ESTABLISHED"
        elif ptype == "TCP_DATA":
            hc = info.get("host_conn")
            if hc:
                payload = pkt.get("payload", "")
                try:
                    hc.sendall(payload.encode("utf-8", errors="replace"))
                except OSError:
                    pass
        elif ptype == "TCP_FIN":
            hc = info.get("host_conn")
            if hc:
                try:
                    hc.shutdown(socket.SHUT_WR)
                except OSError:
                    pass


# ============================================================
# Layer / Rootfs Helpers
# ============================================================

def list_layers(image_name):
    layers = []
    if LAYERS_DIR.exists():
        for f in sorted(LAYERS_DIR.iterdir()):
            if f.is_file() and f.name.endswith(".tar.gz"):
                if f.name.startswith(f"{image_name}_") or f.name.startswith(image_name):
                    layers.append(f)
    return sorted(layers)


def unpack_image(image_name):
    ensure_dirs()
    layers = list_layers(image_name)
    if not layers:
        print(f"Error: no layers found for image '{image_name}' in {LAYERS_DIR}", file=sys.stderr)
        sys.exit(1)
    image_cache = IMAGES_DIR / image_name
    if image_cache.exists():
        return image_cache
    image_cache.mkdir(parents=True, exist_ok=True)
    for layer in layers:
        print(f"Unpacking layer: {layer.name}")
        with tarfile.open(layer, "r:gz") as tf:
            tf.extractall(image_cache)
    return image_cache


def create_container_rootfs(image_name, container_id):
    image_root = unpack_image(image_name)
    container_dir = CONTAINERS_DIR / container_id
    rootfs = container_dir / "rootfs"
    if rootfs.exists():
        shutil.rmtree(rootfs)
    rootfs.mkdir(parents=True, exist_ok=True)
    for item in image_root.iterdir():
        dest = rootfs / item.name
        if item.is_dir():
            if dest.exists():
                for sub in item.rglob("*"):
                    rel = sub.relative_to(item)
                    target = dest / rel
                    if sub.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if sub.is_symlink():
                            link_target = os.readlink(sub)
                            os.symlink(link_target, target)
                        else:
                            shutil.copy2(sub, target)
            else:
                shutil.copytree(item, dest, symlinks=True)
        else:
            if item.is_symlink():
                link_target = os.readlink(item)
                os.symlink(link_target, dest)
            else:
                shutil.copy2(item, dest)
    return rootfs


def save_container_metadata(container_id, metadata):
    container_dir = CONTAINERS_DIR / container_id
    container_dir.mkdir(parents=True, exist_ok=True)
    with open(container_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)


def load_container_metadata(container_id):
    meta_path = CONTAINERS_DIR / container_id / "metadata.json"
    if not meta_path.exists():
        return None
    with open(meta_path, "r") as f:
        return json.load(f)


def is_process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_running_containers():
    running = []
    if not CONTAINERS_DIR.exists():
        return running
    for container_id in sorted(os.listdir(CONTAINERS_DIR)):
        meta = load_container_metadata(container_id)
        if meta and meta.get("status") == "running":
            pid = meta.get("pid")
            if pid and is_process_alive(pid):
                running.append((container_id, meta))
            else:
                meta["status"] = "stopped"
                save_container_metadata(container_id, meta)
    return running


def resolve_container_id(prefix):
    if not CONTAINERS_DIR.exists():
        return None
    matches = []
    for d in sorted(CONTAINERS_DIR.iterdir()):
        if d.is_dir() and d.name.startswith(prefix):
            matches.append(d.name)
    if len(matches) == 1:
        return matches[0]
    if prefix in matches:
        return prefix
    return matches[0] if matches else None


# ============================================================
# Resource Limits
# ============================================================

def apply_resource_limits(meta):
    limits = meta.get("resource_limits", {})
    mem_bytes = limits.get("memory")
    nofile = limits.get("nofile")
    if mem_bytes:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (int(mem_bytes), int(mem_bytes)))
        except (ValueError, OSError) as e:
            print(f"Warning: Failed to set memory limit: {e}", file=sys.stderr)
    if nofile:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            new_val = int(nofile)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_val, max(new_val, hard)))
        except (ValueError, OSError) as e:
            print(f"Warning: Failed to set nofile limit: {e}", file=sys.stderr)


# ============================================================
# Pysh - Python-based shell for use inside chroot containers
# ============================================================

def _p(path):
    rootfs = os.environ.get("MYCONTAINER_ROOTFS")
    if not rootfs:
        return path
    p = str(path)
    if os.path.isabs(p):
        normalized = os.path.normpath(p)
        if normalized == "/":
            return rootfs
        return os.path.join(rootfs, normalized.lstrip("/"))
    return p


def _unmap(path):
    rootfs = os.environ.get("MYCONTAINER_ROOTFS")
    if not rootfs:
        return path
    p = os.path.abspath(str(path))
    if p.startswith(rootfs):
        rest = p[len(rootfs):]
        return rest if rest else "/"
    return path


def pysh_cmd_echo(args):
    print(" ".join(args))


def pysh_cmd_ls(args):
    show_hidden = "-a" in args
    path = "."
    for a in args:
        if not a.startswith("-"):
            path = a
            break
    try:
        entries = sorted(os.listdir(_p(path)))
        for e in entries:
            if show_hidden or not e.startswith("."):
                print(e)
    except FileNotFoundError:
        print(f"ls: cannot access '{path}': No such file or directory", file=sys.stderr)
        return 1
    return 0


def pysh_cmd_cat(args):
    if not args:
        try:
            data = sys.stdin.read()
            sys.stdout.write(data)
        except KeyboardInterrupt:
            pass
        return 0
    rc = 0
    for f in args:
        try:
            with open(_p(f), "r") as fh:
                sys.stdout.write(fh.read())
        except FileNotFoundError:
            print(f"cat: {f}: No such file or directory", file=sys.stderr)
            rc = 1
    return rc


def pysh_cmd_touch(args):
    for f in args:
        real = _p(f)
        os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
        open(real, "a").close()
        try:
            os.utime(real, None)
        except OSError:
            pass
    return 0


def pysh_cmd_mkdir(args):
    for d in args:
        os.makedirs(_p(d), exist_ok=True)
    return 0


def pysh_cmd_rm(args):
    rc = 0
    for f in args:
        real = _p(f)
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.unlink(real)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"rm: {f}: {e}", file=sys.stderr)
            rc = 1
    return rc


def pysh_cmd_pwd(args):
    print(_unmap(os.getcwd()))
    return 0


def pysh_cmd_cd(args):
    path = args[0] if args else os.environ.get("HOME", "/")
    try:
        os.chdir(_p(path))
    except FileNotFoundError:
        print(f"cd: {path}: No such file or directory", file=sys.stderr)
        return 1
    return 0


def pysh_cmd_sleep(args):
    try:
        secs = float(args[0]) if args else 1
        time.sleep(secs)
        return 0
    except ValueError:
        print(f"sleep: invalid time interval '{args[0]}'", file=sys.stderr)
        return 1


def pysh_cmd_ps(args):
    print(f"  PID TTY          TIME CMD")
    print(f"{os.getpid():>5} ?        00:00:00 pysh")
    return 0


def pysh_cmd_id(args):
    print(f"uid=0(root) gid=0(wheel) groups=0(wheel)")
    return 0


def pysh_cmd_whoami(args):
    print("root")
    return 0


def pysh_cmd_uname(args):
    if "-a" in args:
        print("mycontainer 1.0.0 testos x86_64 mycontainer")
    else:
        print("mycontainer")
    return 0


def pysh_cmd_env(args):
    for k, v in sorted(os.environ.items()):
        print(f"{k}={v}")
    return 0


def pysh_cmd_true(args):
    return 0


def pysh_cmd_false(args):
    return 1


def pysh_cmd_hostname(args):
    if args:
        os.environ["HOSTNAME"] = args[0]
    else:
        print(os.environ.get("HOSTNAME", "mycontainer"))
    return 0


def pysh_cmd_exit(args):
    code = int(args[0]) if args else 0
    sys.exit(code)


def pysh_cmd_kill(args):
    if not args:
        print("kill: usage: kill [-s signal] pid", file=sys.stderr)
        return 1
    try:
        sig = 15
        pid = int(args[-1])
        if len(args) >= 3 and args[0] == "-s":
            sig_map = {"TERM": 15, "KILL": 9, "HUP": 1, "INT": 2}
            sig = sig_map.get(args[1], int(args[1]))
        os.kill(pid, sig)
        return 0
    except (ValueError, ProcessLookupError, PermissionError) as e:
        print(f"kill: {e}", file=sys.stderr)
        return 1


def pysh_cmd_ip(args):
    if not args:
        print("Usage: ip [addr]")
        return 1
    sub = args[0]
    if sub == "addr":
        stack = get_net_stack()
        if stack:
            print(f"1: lo: <LOOPBACK,UP>")
            print(f"    inet 127.0.0.1/8 scope host lo")
            print(f"2: eth0: <UP>")
            print(f"    inet {stack.container_ip}/{SUBNET_MASK} scope global eth0")
        else:
            print("1: lo: <LOOPBACK,UP>")
            print("    inet 127.0.0.1/8 scope host lo")
        return 0
    return 1


def pysh_cmd_nc(args):
    if len(args) < 2:
        print("Usage: nc [-l] <host> <port>", file=sys.stderr)
        return 1
    listen = "-l" in args
    positional = [a for a in args if not a.startswith("-")]
    stack = get_net_stack()
    if stack is None:
        print("nc: network stack unavailable", file=sys.stderr)
        return 1
    try:
        if listen:
            port = int(positional[0])
            stack.vbind(port)
            stack.vlisten(port)
            print(f"Listening on 0.0.0.0:{port}")
            conn = stack.vaccept(port)
            if conn is None:
                return 1
            try:
                def _read_net():
                    while True:
                        data = conn.recv(4096, timeout=0.1)
                        if not data:
                            break
                        sys.stdout.write(data.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                t = threading.Thread(target=_read_net, daemon=True)
                t.start()
                for line in sys.stdin:
                    conn.send(line.encode("utf-8"))
            finally:
                conn.close()
            return 0
        else:
            host = positional[0]
            port = int(positional[1])
            conn = stack.vconnect(host, port)
            try:
                def _read_net():
                    while True:
                        data = conn.recv(4096, timeout=0.1)
                        if not data:
                            break
                        sys.stdout.write(data.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                t = threading.Thread(target=_read_net, daemon=True)
                t.start()
                for line in sys.stdin:
                    conn.send(line.encode("utf-8"))
            finally:
                conn.close()
            return 0
    except (OSError, ValueError, IndexError) as e:
        print(f"nc: {e}", file=sys.stderr)
        return 1


def pysh_cmd_curl(args):
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        print("Usage: curl <ip:port>", file=sys.stderr)
        return 1
    target = positional[0]
    if ":" in target:
        host, port_s = target.rsplit(":", 1)
        port = int(port_s)
    else:
        host = target
        port = 80
    stack = get_net_stack()
    if stack is None:
        print("curl: network stack unavailable", file=sys.stderr)
        return 1
    try:
        conn = stack.vconnect(host, port)
        with conn:
            req = f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            conn.send(req.encode("utf-8"))
            resp = b""
            while True:
                chunk = conn.recv(4096, timeout=2)
                if not chunk:
                    break
                resp += chunk
            sys.stdout.write(resp.decode("utf-8", errors="replace"))
        return 0
    except OSError as e:
        print(f"curl: {e}", file=sys.stderr)
        return 1


PYSH_BUILTINS = {
    "echo": pysh_cmd_echo,
    "ls": pysh_cmd_ls,
    "cat": pysh_cmd_cat,
    "touch": pysh_cmd_touch,
    "mkdir": pysh_cmd_mkdir,
    "rm": pysh_cmd_rm,
    "pwd": pysh_cmd_pwd,
    "cd": pysh_cmd_cd,
    "sleep": pysh_cmd_sleep,
    "ps": pysh_cmd_ps,
    "id": pysh_cmd_id,
    "whoami": pysh_cmd_whoami,
    "uname": pysh_cmd_uname,
    "env": pysh_cmd_env,
    "true": pysh_cmd_true,
    "false": pysh_cmd_false,
    "hostname": pysh_cmd_hostname,
    "exit": pysh_cmd_exit,
    "kill": pysh_cmd_kill,
    "ip": pysh_cmd_ip,
    "nc": pysh_cmd_nc,
    "curl": pysh_cmd_curl,
}


def pysh_run_command(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return 0
    parts = shlex.split(line)
    cmd = parts[0]
    args = parts[1:]
    if "|" in line:
        sub_cmds = [s.strip() for s in line.split("|")]
        return pysh_run_pipeline(sub_cmds)
    if cmd in PYSH_BUILTINS:
        try:
            return PYSH_BUILTINS[cmd](args)
        except SystemExit:
            raise
        except Exception as e:
            print(f"{cmd}: {e}", file=sys.stderr)
            return 1
    else:
        try:
            result = os.spawnvp(os.P_WAIT, cmd, [cmd] + args)
            return result
        except FileNotFoundError:
            print(f"{cmd}: command not found", file=sys.stderr)
            return 127
        except Exception as e:
            print(f"{cmd}: {e}", file=sys.stderr)
            return 1


def pysh_run_pipeline(cmds):
    prev_out = None
    last_rc = 0
    for cmd_line in cmds:
        parts = shlex.split(cmd_line)
        cmd = parts[0]
        args = parts[1:]
        if cmd in PYSH_BUILTINS:
            buf = io.StringIO()
            old_stdout = sys.stdout
            old_stdin = sys.stdin
            sys.stdout = buf
            if prev_out is not None:
                sys.stdin = io.StringIO(prev_out)
            try:
                last_rc = PYSH_BUILTINS[cmd](args)
            except SystemExit as e:
                last_rc = e.code
            finally:
                sys.stdout = old_stdout
                sys.stdin = old_stdin
            prev_out = buf.getvalue()
        else:
            try:
                proc = subprocess.Popen(
                    [cmd] + args,
                    stdin=subprocess.PIPE if prev_out else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, _ = proc.communicate(input=prev_out)
                prev_out = stdout
                last_rc = proc.returncode
            except FileNotFoundError:
                print(f"{cmd}: command not found", file=sys.stderr)
                prev_out = ""
                last_rc = 127
    if prev_out:
        sys.stdout.write(prev_out)
    return last_rc


def pysh_exec_script(script):
    rc = 0
    for raw_line in script.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for stmt in line.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    rc = pysh_run_command(stmt)
                except SystemExit as e:
                    return e.code
    return rc


def pysh_interactive():
    os.environ.setdefault("PATH", "/bin:/usr/bin")
    os.environ.setdefault("HOME", "/root")
    os.environ.setdefault("HOSTNAME", os.environ.get("HOSTNAME", "mycontainer"))
    ps1 = os.environ.get("PS1", "# ")
    while True:
        try:
            line = input(ps1)
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        try:
            pysh_run_command(line)
        except SystemExit as e:
            return e.code


def setup_network_for_container(container_id):
    global NET_STACK_INSTANCE
    ip = get_container_ip(container_id)
    if ip is None:
        return
    stack = VirtualNetworkStack(ip, container_id)
    try:
        stack.connect_bridge()
        NET_STACK_INSTANCE = stack
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Warning: network setup failed: {e}", file=sys.stderr)


def run_inside_container(cmd, container_id=None):
    os.environ["PATH"] = "/bin:/usr/bin"
    os.environ["HOME"] = "/root"
    os.environ["HOSTNAME"] = os.environ.get("HOSTNAME", "mycontainer")
    os.environ["TERM"] = os.environ.get("TERM", "xterm")
    if container_id:
        setup_network_for_container(container_id)
    if cmd:
        rc = pysh_exec_script(cmd)
        sys.exit(rc)
    else:
        rc = pysh_interactive()
        sys.exit(rc)


# ============================================================
# Stdout Tee to log file
# ============================================================

class StdoutLogger:
    def __init__(self, log_path):
        self.log_path = log_path
        self._log_file = None
        self._stdout = sys.stdout
        self._lock = threading.Lock()

    def start(self):
        self._log_file = open(self.log_path, "a", buffering=1)
        sys.stdout = self

    def stop(self):
        sys.stdout = self._stdout
        try:
            if self._log_file:
                self._log_file.close()
        except OSError:
            pass

    def write(self, data):
        with self._lock:
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except Exception:
                pass
            try:
                if self._log_file:
                    self._log_file.write(data)
                    self._log_file.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self._stdout.flush()
        except Exception:
            pass
        try:
            if self._log_file:
                self._log_file.flush()
        except Exception:
            pass


# ============================================================
# Port Mapping helpers
# ============================================================

def parse_port_mappings(raw_list):
    mappings = []
    if not raw_list:
        return mappings
    for raw in raw_list:
        if ":" not in raw:
            print(f"Invalid port mapping: {raw}", file=sys.stderr)
            continue
        host_s, container_s = raw.rsplit(":", 1)
        try:
            host_port = int(host_s)
            container_port = int(container_s)
        except ValueError:
            print(f"Invalid port mapping (non-numeric): {raw}", file=sys.stderr)
            continue
        mappings.append((host_port, container_port))
    return mappings


# ============================================================
# Restart policy helpers
# ============================================================

def parse_restart_policy(raw):
    if not raw:
        return {"policy": "no"}
    raw = raw.lower()
    if raw == "always":
        return {"policy": "always"}
    if raw == "no" or raw == "none":
        return {"policy": "no"}
    if raw.startswith("on-failure"):
        max_count = 0
        if ":" in raw:
            try:
                max_count = int(raw.split(":", 1)[1])
            except ValueError:
                max_count = 0
        return {"policy": "on-failure", "max_retries": max_count}
    return {"policy": "no"}


def should_restart(policy_info, exit_code, retry_count):
    p = policy_info.get("policy", "no")
    if p == "no":
        return False
    if p == "always":
        return True
    if p == "on-failure":
        if exit_code != 0:
            max_r = policy_info.get("max_retries", 0)
            if max_r == 0 or retry_count < max_r:
                return True
    return False


# ============================================================
# Command handlers
# ============================================================

ACTIVE_PORT_PROXIES = {}


def _spawn_container_process(container_id, rootfs, meta, logger):
    child_pid = os.fork()
    if child_pid == 0:
        try:
            def _handle_sigterm(signum, frame):
                sys.exit(128 + signum)
            signal.signal(signal.SIGTERM, _handle_sigterm)
            signal.signal(signal.SIGINT, _handle_sigterm)
            try:
                os.setpgrp()
            except OSError:
                pass
            apply_resource_limits(meta)
            try:
                os.chroot(str(rootfs))
                os.chdir("/")
            except PermissionError:
                print(f"Warning: chroot not permitted (running without root). "
                      f"Using namespace simulation via path remapping.", file=sys.stderr)
                os.environ["MYCONTAINER_ROOTFS"] = str(rootfs.resolve())
                os.chdir(str(rootfs.resolve()))
            run_inside_container(meta.get("cmd", ""), container_id)
        except SystemExit:
            raise
        except Exception as e:
            print(f"Error in container: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            os.setpgid(child_pid, child_pid)
        except OSError:
            pass
        return child_pid


def cmd_run(args):
    ensure_dirs()
    bridge = get_bridge()
    bridge.start()

    container_id = str(uuid.uuid4())[:12]
    print(f"Creating container {container_id} from image '{args.image}'...")

    rootfs = create_container_rootfs(args.image, container_id)
    container_ip = allocate_ip(container_id)
    print(f"Assigned IP {container_ip} to container {container_id}")

    port_mappings = parse_port_mappings(getattr(args, "p", None))
    restart_policy = parse_restart_policy(getattr(args, "restart", None))

    resource_limits = {}
    if getattr(args, "memory", None):
        raw = args.memory.strip().upper()
        mult = 1
        if raw.endswith("G"):
            mult = 1024 ** 3
            raw = raw[:-1]
        elif raw.endswith("M"):
            mult = 1024 ** 2
            raw = raw[:-1]
        elif raw.endswith("K"):
            mult = 1024
            raw = raw[:-1]
        try:
            resource_limits["memory"] = int(float(raw) * mult)
        except ValueError:
            print(f"Invalid memory limit: {args.memory}", file=sys.stderr)
    if getattr(args, "nofile", None):
        try:
            resource_limits["nofile"] = int(args.nofile)
        except ValueError:
            print(f"Invalid nofile limit: {args.nofile}", file=sys.stderr)

    metadata = {
        "id": container_id,
        "image": args.image,
        "cmd": args.cmd,
        "status": "running",
        "created_at": time.time(),
        "pid": None,
        "pgid": None,
        "ip": container_ip,
        "port_mappings": port_mappings,
        "restart_policy": restart_policy,
        "resource_limits": resource_limits,
        "restart_count": 0,
    }
    save_container_metadata(container_id, metadata)

    container_dir = CONTAINERS_DIR / container_id
    stdout_log = container_dir / "stdout.log"
    if stdout_log.exists():
        stdout_log.unlink()

    logger = StdoutLogger(str(stdout_log))
    logger.start()

    active_proxies = []
    for host_port, container_port in port_mappings:
        proxy = PortMappingProxy(host_port, container_ip, container_port, bridge)
        if proxy.start():
            active_proxies.append(proxy)
            ACTIVE_PORT_PROXIES[(container_id, host_port)] = proxy
            print(f"Port mapping: 127.0.0.1:{host_port} -> {container_ip}:{container_port}")

    retry_count = 0
    try:
        while True:
            child_pid = _spawn_container_process(container_id, rootfs, metadata, logger)
            pgid = child_pid
            metadata["pid"] = child_pid
            metadata["pgid"] = pgid
            metadata["status"] = "running"
            metadata["restart_count"] = retry_count
            save_container_metadata(container_id, metadata)

            print(f"Container {container_id} started (PID {child_pid}, PGID {pgid})")
            try:
                _, status = os.waitpid(child_pid, 0)
                exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                metadata["last_exit_code"] = exit_code
                metadata["last_stopped_at"] = time.time()
                save_container_metadata(container_id, metadata)
                print(f"Container {container_id} exited with code {exit_code}")
            except KeyboardInterrupt:
                print(f"\nStopping container {container_id}...")
                stop_container_internal(container_id, metadata, child_pid, pgid)
                break

            restart_info = metadata.get("restart_policy", {"policy": "no"})
            if not should_restart(restart_info, exit_code, retry_count):
                metadata["status"] = "stopped"
                metadata["exit_code"] = exit_code
                metadata["stopped_at"] = time.time()
                save_container_metadata(container_id, metadata)
                break

            retry_count += 1
            metadata["restart_count"] = retry_count
            save_container_metadata(container_id, metadata)
            delay = min(2 ** min(retry_count, 5), 30)
            print(f"Restarting container {container_id} in {delay}s (attempt {retry_count})...")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print(f"\nCanceled restart of {container_id}")
                metadata["status"] = "stopped"
                metadata["exit_code"] = exit_code
                metadata["stopped_at"] = time.time()
                save_container_metadata(container_id, metadata)
                break
    finally:
        logger.stop()
        for proxy in active_proxies:
            try:
                proxy.stop()
            except Exception:
                pass
            ACTIVE_PORT_PROXIES.pop((container_id, proxy.host_port), None)


def stop_container_internal(container_id, meta, pid, pgid):
    if pid and is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to container {container_id} (PID {pid})")
        except OSError as e:
            print(f"Warning: failed to send SIGTERM: {e}")
            if pgid:
                try:
                    os.kill(-pgid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 5
        while time.time() < deadline and is_process_alive(pid):
            time.sleep(0.2)
        if is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"Sent SIGKILL to container {container_id} (PID {pid})")
            except OSError:
                if pgid:
                    try:
                        os.kill(-pgid, signal.SIGKILL)
                    except OSError:
                        pass
            for _ in range(10):
                if not is_process_alive(pid):
                    break
                time.sleep(0.2)
    meta["status"] = "stopped"
    meta["stopped_at"] = time.time()
    save_container_metadata(container_id, meta)
    print(f"Container {container_id} stopped")


def cmd_ps(args):
    ensure_dirs()
    running = get_running_containers()
    if not running:
        print("No running containers")
        return
    print(f"{'CONTAINER ID':<15} {'IMAGE':<20} {'STATUS':<10} {'PID':<8} {'IP':<16} {'CMD'}")
    for cid, meta in running:
        cmd_display = (meta.get("cmd") or "/bin/sh")[:40]
        ip = meta.get("ip", "")
        print(f"{cid:<15} {meta.get('image',''):<20} {meta.get('status',''):<10} {str(meta.get('pid','')):<8} {ip:<16} {cmd_display}")


def stop_container(container_id):
    cid = resolve_container_id(container_id)
    if cid is None:
        print(f"Error: container {container_id} not found", file=sys.stderr)
        return False
    meta = load_container_metadata(cid)
    if not meta:
        print(f"Error: container {container_id} not found", file=sys.stderr)
        return False
    pid = meta.get("pid")
    pgid = meta.get("pgid")
    if not pid or not is_process_alive(pid):
        meta["status"] = "stopped"
        save_container_metadata(cid, meta)
        print(f"Container {cid} is already stopped")
        return True
    stop_container_internal(cid, meta, pid, pgid)
    for key in list(ACTIVE_PORT_PROXIES.keys()):
        if key[0] == cid:
            try:
                ACTIVE_PORT_PROXIES[key].stop()
            except Exception:
                pass
            ACTIVE_PORT_PROXIES.pop(key, None)
    return True


def cmd_stop(args):
    ensure_dirs()
    success = stop_container(args.container_id)
    sys.exit(0 if success else 1)


def cmd_exec(args):
    ensure_dirs()
    cid = resolve_container_id(args.container_id)
    if cid is None:
        print(f"Error: container {args.container_id} not found", file=sys.stderr)
        sys.exit(1)
    meta = load_container_metadata(cid)
    if not meta:
        print(f"Error: container {args.container_id} not found", file=sys.stderr)
        sys.exit(1)
    pid = meta.get("pid")
    if not pid or not is_process_alive(pid):
        print(f"Error: container {cid} is not running", file=sys.stderr)
        sys.exit(1)
    rootfs = CONTAINERS_DIR / cid / "rootfs"
    if not rootfs.exists():
        print(f"Error: container rootfs missing for {cid}", file=sys.stderr)
        sys.exit(1)
    cmd_str = " ".join(args.cmd) if args.cmd else ""
    child_pid = os.fork()
    if child_pid == 0:
        try:
            def _handle_sigterm(signum, frame):
                sys.exit(128 + signum)
            signal.signal(signal.SIGTERM, _handle_sigterm)
            signal.signal(signal.SIGINT, _handle_sigterm)
            apply_resource_limits(meta)
            try:
                os.chroot(str(rootfs))
                os.chdir("/")
            except PermissionError:
                os.environ["MYCONTAINER_ROOTFS"] = str(rootfs.resolve())
                os.chdir(str(rootfs.resolve()))
            run_inside_container(cmd_str, cid)
        except SystemExit:
            raise
        except Exception as e:
            print(f"exec error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            _, status = os.waitpid(child_pid, 0)
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
            sys.exit(exit_code)
        except KeyboardInterrupt:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass
            sys.exit(130)


def cmd_logs(args):
    ensure_dirs()
    cid = resolve_container_id(args.container_id)
    if cid is None:
        print(f"Error: container {args.container_id} not found", file=sys.stderr)
        sys.exit(1)
    log_path = CONTAINERS_DIR / cid / "stdout.log"
    if not log_path.exists():
        print(f"No logs found for container {cid}")
        return
    follow = getattr(args, "follow", False)
    tail = getattr(args, "tail", None)
    with open(log_path, "r") as f:
        lines = f.readlines()
        if tail is not None:
            try:
                n = int(tail)
                lines = lines[-n:]
            except ValueError:
                pass
        for line in lines:
            sys.stdout.write(line)
        sys.stdout.flush()
        if follow:
            try:
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.2)
            except KeyboardInterrupt:
                pass


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="mycontainer - simplified container runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a container")
    run_parser.add_argument("--image", required=True, help="Image name")
    run_parser.add_argument("--cmd", default="", help="Command to run")
    run_parser.add_argument("-p", action="append", help="Port mapping host_port:container_port")
    run_parser.add_argument("--memory", help="Memory limit (e.g. 128M, 1G)")
    run_parser.add_argument("--nofile", help="Max open files (RLIMIT_NOFILE)")
    run_parser.add_argument("--restart", help="Restart policy: always, on-failure[:N]")

    subparsers.add_parser("ps", help="List running containers")

    stop_parser = subparsers.add_parser("stop", help="Stop a container")
    stop_parser.add_argument("container_id", help="Container ID")

    exec_parser = subparsers.add_parser("exec", help="Execute command in running container")
    exec_parser.add_argument("container_id", help="Container ID")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run")

    logs_parser = subparsers.add_parser("logs", help="View container logs")
    logs_parser.add_argument("container_id", help="Container ID")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    logs_parser.add_argument("--tail", help="Only show last N lines")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "ps":
        cmd_ps(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "exec":
        cmd_exec(args)
    elif args.command == "logs":
        cmd_logs(args)


if __name__ == "__main__":
    main()

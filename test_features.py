#!/usr/bin/env python3
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
MYCONTAINER = PROJECT_DIR / "mycontainer.py"
MYCONTAINER_HOME = Path(os.environ.get("MYCONTAINER_HOME", PROJECT_DIR / ".mycontainer_test2"))
CONTAINERS_DIR = MYCONTAINER_HOME / "containers"
LAYERS_DIR = PROJECT_DIR / "layers"


def run(cmd_parts, check=True, timeout=30, capture=True, **kwargs):
    env = os.environ.copy()
    env["MYCONTAINER_HOME"] = str(MYCONTAINER_HOME)
    env["MYCONTAINER_LAYERS"] = str(LAYERS_DIR)
    cmd_str = " ".join(shlex.quote(str(p)) for p in cmd_parts)
    print(f"$ {cmd_str}")
    result = subprocess.run(
        cmd_parts,
        capture_output=capture,
        text=True,
        timeout=timeout,
        env=env,
        **kwargs,
    )
    if capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (rc={result.returncode}): {cmd_str}")
    return result


def check(condition, msg):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {msg}")
    if not condition:
        sys.exit(1)


def get_container_ids():
    result = run(["python3", str(MYCONTAINER), "ps"])
    ids = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if parts and parts[0] != "CONTAINER":
            ids.append(parts[0])
    return ids


def get_container_ip(cid):
    meta = json.load(open(CONTAINERS_DIR / cid / "metadata.json"))
    return meta.get("ip")


def main():
    print("=" * 60)
    print("MyContainer Feature Tests")
    print(f"Using MYCONTAINER_HOME = {MYCONTAINER_HOME}")
    print("=" * 60)

    print("\n[0] Cleanup old state...")
    if MYCONTAINER_HOME.exists():
        import shutil
        shutil.rmtree(MYCONTAINER_HOME)
    run(["python3", str(PROJECT_DIR / "build_test_image.py")])

    env = {**os.environ,
           "MYCONTAINER_HOME": str(MYCONTAINER_HOME),
           "MYCONTAINER_LAYERS": str(LAYERS_DIR)}

    running_procs = []

    try:
        # ===== Test 1: Virtual Network & IP Assignment =====
        print("\n[1] Test: Virtual Network - IP Assignment...")
        proc1 = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
             "echo started1; ip addr; sleep 300"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(proc1)
        time.sleep(3)

        ids = get_container_ids()
        check(len(ids) == 1, f"1 container running (got {len(ids)})")
        cid1 = ids[0]
        ip1 = get_container_ip(cid1)
        check(ip1 is not None, f"Container has IP assigned: {ip1}")
        check(ip1.startswith("172.17.0."), f"IP is in 172.17.0.0/16 subnet: {ip1}")
        check(ip1 != "172.17.0.1", f"Container IP is not bridge IP: {ip1}")

        proc2 = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
             "echo started2; sleep 300"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(proc2)
        time.sleep(3)

        ids = get_container_ids()
        check(len(ids) == 2, f"2 containers running (got {len(ids)})")
        cid2 = [x for x in ids if x != cid1][0]
        ip2 = get_container_ip(cid2)
        check(ip2 is not None, f"Second container has IP: {ip2}")
        check(ip1 != ip2, f"Different IPs assigned: {ip1} vs {ip2}")

        # ===== Test 2: Inter-container communication =====
        print("\n[2] Test: Inter-container Communication...")
        server_proc = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
             "echo SERVER_READY; echo HELLO_FROM_SERVER | nc -l -p 9999"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(server_proc)
        time.sleep(6)
        ids = get_container_ids()
        server_cid = [x for x in ids if x not in (cid1, cid2)][0]
        server_ip = get_container_ip(server_cid)
        print(f"Server container: {server_cid}, IP: {server_ip}")

        result = None
        for attempt in range(3):
            result = run(["python3", str(MYCONTAINER), "exec", cid1,
                          "curl", f"{server_ip}:9999"], check=False, timeout=20)
            if "HELLO_FROM_SERVER" in result.stdout:
                break
            time.sleep(2)
        print(f"curl result: rc={result.returncode}, stdout='{result.stdout.strip()}', stderr='{result.stderr.strip()}'")
        check("HELLO_FROM_SERVER" in result.stdout,
              f"Inter-container curl worked: got '{result.stdout.strip()}'")

        try:
            server_proc.wait(timeout=5)
        except Exception:
            pass

        # ===== Test 3: Port Mapping =====
        print("\n[3] Test: Port Mapping (-p host:container)...")
        HOST_PORT = 19876
        port_proc = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos",
             "-p", f"{HOST_PORT}:8888", "--cmd",
             "echo PORT_SERVER_READY; echo PORT_MAP_HELLO | nc -l -p 8888"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(port_proc)
        time.sleep(6)

        ids = get_container_ids()
        check(len(ids) >= 1, "Port-mapped container running")

        data = b""
        for attempt in range(3):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(8)
                s.connect(("127.0.0.1", HOST_PORT))
                data = b""
                while True:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                if b"PORT_MAP_HELLO" in data:
                    break
            except Exception as e:
                print(f"Socket attempt {attempt+1} error: {e}", file=sys.stderr)
                time.sleep(2)
        print(f"Received from port {HOST_PORT}: '{data.decode(errors='replace').strip()}'")
        check(b"PORT_MAP_HELLO" in data, f"Port mapping worked")

        try:
            port_proc.wait(timeout=5)
        except Exception:
            pass

        # ===== Test 4: exec command =====
        print("\n[4] Test: mycontainer exec...")
        result = run(["python3", str(MYCONTAINER), "exec", cid1, "echo", "exec_works"])
        check("exec_works" in result.stdout, "exec command works")

        result = run(["python3", str(MYCONTAINER), "exec", cid1, "ls", "/tmp"])
        check("base_layer_marker" in result.stdout, "exec sees container filesystem")

        result = run(["python3", str(MYCONTAINER), "exec", cid1, "false"], check=False)
        check(result.returncode == 1, f"exec propagates exit code (got {result.returncode})")

        # ===== Test 5: logs command =====
        print("\n[5] Test: mycontainer logs...")
        result = run(["python3", str(MYCONTAINER), "logs", cid1])
        check("started1" in result.stdout, "logs show container output (started1)")
        check("172.17.0" in result.stdout, "logs show ip addr output")

        log_file = CONTAINERS_DIR / cid1 / "stdout.log"
        check(log_file.exists(), f"stdout.log persists at {log_file}")
        with open(log_file) as f:
            log_content = f.read()
        check("started1" in log_content, "stdout.log file has container output")

        result = run(["python3", str(MYCONTAINER), "logs", cid1, "--tail", "2"])
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        check(len(lines) <= 3, f"--tail works: got {len(lines)} lines")

        # ===== Test 6: Resource Limits =====
        print("\n[6] Test: Resource Limits (setrlimit)...")
        result = run(
            ["python3", str(MYCONTAINER), "run", "--image", "testos",
             "--memory", "64M", "--nofile", "128", "--cmd",
             "echo limits_applied"],
            timeout=15,
        )
        all_cids = list(CONTAINERS_DIR.iterdir()) if CONTAINERS_DIR.exists() else []
        latest = sorted(all_cids, key=lambda p: p.stat().st_mtime)[-1].name
        meta = json.load(open(CONTAINERS_DIR / latest / "metadata.json"))
        limits = meta.get("resource_limits", {})
        check(limits.get("memory") == 64 * 1024 * 1024,
              f"Memory limit set in metadata: {limits.get('memory')}")
        check(limits.get("nofile") == 128,
              f"NOFILE limit set in metadata: {limits.get('nofile')}")
        check("limits_applied" in result.stdout, "Container with limits ran successfully")

        # ===== Test 7: Restart Policies =====
        print("\n[7] Test: Restart Policy --restart=on-failure:2...")
        proc_fail = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos",
             "--restart", "on-failure:2", "--cmd",
             "echo failing_now; exit 1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(proc_fail)
        try:
            stdout, stderr = proc_fail.communicate(timeout=40)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
        except subprocess.TimeoutExpired:
            proc_fail.terminate()
            stdout, stderr = proc_fail.communicate(timeout=5)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)

        all_cids = list(CONTAINERS_DIR.iterdir()) if CONTAINERS_DIR.exists() else []
        latest = sorted(all_cids, key=lambda p: p.stat().st_mtime)[-1].name
        meta = json.load(open(CONTAINERS_DIR / latest / "metadata.json"))
        print(f"  restart_count={meta.get('restart_count')}, status={meta.get('status')}")
        check(meta.get("restart_count", 0) >= 1,
              f"Container was restarted (restart_count={meta.get('restart_count')})")
        check(meta.get("status") == "stopped",
              f"Container stopped after max retries (status={meta.get('status')})")

        print("\n[8] Test: Restart Policy --restart=always...")
        proc_always = subprocess.Popen(
            ["python3", str(MYCONTAINER), "run", "--image", "testos",
             "--restart", "always", "--cmd",
             "echo always_run; sleep 2; exit 0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        running_procs.append(proc_always)
        time.sleep(6)
        proc_always.terminate()
        try:
            stdout, stderr = proc_always.communicate(timeout=10)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
        except subprocess.TimeoutExpired:
            proc_always.kill()
            stdout, stderr = proc_always.communicate(timeout=5)

        all_cids = list(CONTAINERS_DIR.iterdir()) if CONTAINERS_DIR.exists() else []
        latest = sorted(all_cids, key=lambda p: p.stat().st_mtime)[-1].name
        meta = json.load(open(CONTAINERS_DIR / latest / "metadata.json"))
        check(meta.get("restart_policy", {}).get("policy") == "always",
              "Restart policy 'always' recorded in metadata")
        print(f"  restart_count={meta.get('restart_count')}")

    finally:
        # ===== Cleanup =====
        print("\n[Cleanup] Stopping all containers...")
        for cid in get_container_ids():
            try:
                run(["python3", str(MYCONTAINER), "stop", cid], check=False, timeout=10)
            except Exception:
                pass

        for p in running_procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    print("\n" + "=" * 60)
    print("All feature tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    main()

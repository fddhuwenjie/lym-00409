#!/usr/bin/env python3
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
MYCONTAINER = PROJECT_DIR / "mycontainer.py"
MYCONTAINER_HOME = Path(os.environ.get("MYCONTAINER_HOME", PROJECT_DIR / ".mycontainer_test"))
CONTAINERS_DIR = MYCONTAINER_HOME / "containers"
LAYERS_DIR = PROJECT_DIR / "layers"


def run(cmd_parts, check=True, timeout=60, capture=True, **kwargs):
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


def main():
    print("=" * 60)
    print("MyContainer Acceptance Tests")
    print(f"Using MYCONTAINER_HOME = {MYCONTAINER_HOME}")
    print("=" * 60)

    print("\n[0] Cleanup old state and build test image...")
    if MYCONTAINER_HOME.exists():
        import shutil
        shutil.rmtree(MYCONTAINER_HOME)
    run(["python3", str(PROJECT_DIR / "build_test_image.py")])

    layers = list(LAYERS_DIR.glob("testos_*.tar.gz"))
    check(len(layers) >= 2, f"Found {len(layers)} test image layers (expected >=2)")

    print("\n[1] Test: ps with no running containers...")
    result = run(["python3", str(MYCONTAINER), "ps"])
    check("No running containers" in result.stdout, "ps reports no running containers")

    print("\n[2] Test: run short-lived container and verify it exits...")
    for stale in ["/tmp/testfile", "/tmp/container1_only", "/tmp/container2_only"]:
        try:
            os.unlink(stale)
        except FileNotFoundError:
            pass
    result = run(
        ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
         "echo hello from container; ls /; touch /tmp/testfile"],
        timeout=30,
    )
    check("hello from container" in result.stdout, "Container executed echo")

    ids = get_container_ids()
    check(len(ids) == 0, "No containers remain running after short-lived command")

    all_containers = list(CONTAINERS_DIR.iterdir()) if CONTAINERS_DIR.exists() else []
    check(len(all_containers) >= 1, f"Container metadata preserved (found {len(all_containers)})")

    cid_short = all_containers[0].name
    rootfs_short = CONTAINERS_DIR / cid_short / "rootfs"
    check(rootfs_short.exists(), f"Container rootfs exists at {rootfs_short}")
    check((rootfs_short / "tmp" / "testfile").exists(),
          "Container-created /tmp/testfile exists in container rootfs")
    check(not Path("/tmp/testfile").exists(),
          "Container-created /tmp/testfile does NOT appear on host /tmp")

    print("\n[3] Test: layered image files present...")
    check((rootfs_short / "tmp" / "base_layer_marker").exists(),
          "Layer 1 file /tmp/base_layer_marker present")
    check((rootfs_short / "opt" / "myapp" / "hello.txt").exists(),
          "Layer 2 file /opt/myapp/hello.txt present")
    check((rootfs_short / "data" / "layer2_marker").exists(),
          "Layer 2 file /data/layer2_marker present")
    with open(rootfs_short / "opt" / "myapp" / "hello.txt") as f:
        content = f.read().strip()
    check(content == "Hello from testos layer 2!",
          f"Layer 2 file has correct content: '{content}'")

    print("\n[4] Test: filesystem isolation - two containers互不影响...")
    env = {**os.environ,
           "MYCONTAINER_HOME": str(MYCONTAINER_HOME),
           "MYCONTAINER_LAYERS": str(LAYERS_DIR)}

    proc1 = subprocess.Popen(
        ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
         "touch /tmp/container1_only; echo container1_ready; sleep 60"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    time.sleep(4)

    proc2 = subprocess.Popen(
        ["python3", str(MYCONTAINER), "run", "--image", "testos", "--cmd",
         "touch /tmp/container2_only; echo container2_ready; sleep 60"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    time.sleep(4)

    ids = get_container_ids()
    check(len(ids) == 2, f"Found 2 running containers (got {len(ids)}): {ids}")

    cid_a, cid_b = ids[0], ids[1]
    fs_a = CONTAINERS_DIR / cid_a / "rootfs"
    fs_b = CONTAINERS_DIR / cid_b / "rootfs"

    a_has_c1 = (fs_a / "tmp" / "container1_only").exists()
    a_has_c2 = (fs_a / "tmp" / "container2_only").exists()
    b_has_c1 = (fs_b / "tmp" / "container1_only").exists()
    b_has_c2 = (fs_b / "tmp" / "container2_only").exists()

    check((a_has_c1 and not a_has_c2 and not b_has_c1 and b_has_c2) or
          (a_has_c2 and not a_has_c1 and not b_has_c2 and b_has_c1),
          "Each container has only its own /tmp file, not the other's")

    check(not Path("/tmp/container1_only").exists(),
          "Host /tmp does not have container1_only")
    check(not Path("/tmp/container2_only").exists(),
          "Host /tmp does not have container2_only")

    print("\n[5] Test: stop first container (SIGTERM -> SIGKILL)...")
    stop_result = run(["python3", str(MYCONTAINER), "stop", cid_a])
    check("Sent SIGTERM" in stop_result.stdout or "stopped" in stop_result.stdout.lower(),
          "stop command reported SIGTERM and/or stopped")

    for _ in range(20):
        ids = get_container_ids()
        if cid_a not in ids:
            break
        time.sleep(1)

    ids = get_container_ids()
    check(cid_a not in ids, f"Container {cid_a} no longer in ps list")

    meta_a = json.load(open(CONTAINERS_DIR / cid_a / "metadata.json"))
    check(meta_a.get("status") == "stopped",
          f"Container {cid_a} metadata status is 'stopped'")

    print("\n[6] Test: second container unaffected after stopping first...")
    ids = get_container_ids()
    check(cid_b in ids, f"Container {cid_b} still running (unaffected)")

    print("\n[7] Test: stop second container...")
    run(["python3", str(MYCONTAINER), "stop", cid_b])
    for _ in range(20):
        ids = get_container_ids()
        if cid_b not in ids:
            break
        time.sleep(1)

    ids = get_container_ids()
    check(len(ids) == 0, f"All containers stopped (remaining: {ids})")

    for p in [proc1, proc2]:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    print("\n" + "=" * 60)
    print("All acceptance tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    main()

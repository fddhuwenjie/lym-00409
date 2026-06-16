#!/usr/bin/env python3
import argparse
import io
import json
import os
import shlex
import shutil
import signal
import sys
import tarfile
import time
import uuid
from pathlib import Path

MYCONTAINER_HOME = Path(os.environ.get("MYCONTAINER_HOME", Path.home() / ".mycontainer"))
CONTAINERS_DIR = MYCONTAINER_HOME / "containers"
IMAGES_DIR = MYCONTAINER_HOME / "images"
LAYERS_DIR = Path(os.environ.get("MYCONTAINER_LAYERS", Path(__file__).parent / "layers"))


def ensure_dirs():
    for d in [MYCONTAINER_HOME, CONTAINERS_DIR, IMAGES_DIR, LAYERS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


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
            sys.stdout = buf
            try:
                last_rc = PYSH_BUILTINS[cmd](args)
            except SystemExit as e:
                last_rc = e.code
            finally:
                sys.stdout = old_stdout
            prev_out = buf.getvalue()
        else:
            try:
                import subprocess
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


def run_inside_container(cmd):
    os.environ["PATH"] = "/bin:/usr/bin"
    os.environ["HOME"] = "/root"
    os.environ["HOSTNAME"] = os.environ.get("HOSTNAME", "mycontainer")
    os.environ["TERM"] = os.environ.get("TERM", "xterm")

    if cmd:
        rc = pysh_exec_script(cmd)
        sys.exit(rc)
    else:
        rc = pysh_interactive()
        sys.exit(rc)


# ============================================================
# Command handlers
# ============================================================

def cmd_run(args):
    ensure_dirs()
    container_id = str(uuid.uuid4())[:12]
    print(f"Creating container {container_id} from image '{args.image}'...")

    rootfs = create_container_rootfs(args.image, container_id)

    metadata = {
        "id": container_id,
        "image": args.image,
        "cmd": args.cmd,
        "status": "running",
        "created_at": time.time(),
        "pid": None,
        "pgid": None,
    }
    save_container_metadata(container_id, metadata)

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
            try:
                os.chroot(str(rootfs))
                os.chdir("/")
            except PermissionError:
                print(f"Warning: chroot not permitted (running without root). "
                      f"Using namespace simulation via path remapping.", file=sys.stderr)
                os.environ["MYCONTAINER_ROOTFS"] = str(rootfs.resolve())
                os.chdir(str(rootfs.resolve()))
            run_inside_container(args.cmd)
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
        pgid = child_pid
        metadata["pid"] = child_pid
        metadata["pgid"] = pgid
        save_container_metadata(container_id, metadata)

        print(f"Container {container_id} started (PID {child_pid}, PGID {pgid})")
        try:
            _, status = os.waitpid(child_pid, 0)
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
            metadata["status"] = "stopped"
            metadata["exit_code"] = exit_code
            metadata["stopped_at"] = time.time()
            save_container_metadata(container_id, metadata)
            print(f"Container {container_id} exited with code {exit_code}")
        except KeyboardInterrupt:
            print(f"\nStopping container {container_id}...")
            stop_container(container_id)


def cmd_ps(args):
    ensure_dirs()
    running = get_running_containers()
    if not running:
        print("No running containers")
        return

    print(f"{'CONTAINER ID':<15} {'IMAGE':<20} {'STATUS':<10} {'PID':<8} {'CMD'}")
    for cid, meta in running:
        cmd_display = (meta.get("cmd") or "/bin/sh")[:50]
        print(f"{cid:<15} {meta.get('image',''):<20} {meta.get('status',''):<10} {meta.get('pid',''):<8} {cmd_display}")


def stop_container(container_id):
    meta = load_container_metadata(container_id)
    if not meta:
        print(f"Error: container {container_id} not found", file=sys.stderr)
        return False

    pid = meta.get("pid")
    pgid = meta.get("pgid")

    if not pid or not is_process_alive(pid):
        meta["status"] = "stopped"
        save_container_metadata(container_id, meta)
        print(f"Container {container_id} is already stopped")
        return True

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to container {container_id} (PID {pid})")
    except OSError as e:
        print(f"Warning: failed to send SIGTERM to PID {pid}: {e}")
        if pgid:
            try:
                os.kill(-pgid, signal.SIGTERM)
                print(f"Sent SIGTERM to process group -{pgid}")
            except OSError:
                pass

    deadline = time.time() + 5
    while time.time() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.2)

    if is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"Sent SIGKILL to container {container_id} (PID {pid})")
        except OSError as e:
            print(f"Warning: failed to send SIGKILL to PID {pid}: {e}")
            if pgid:
                try:
                    os.kill(-pgid, signal.SIGKILL)
                    print(f"Sent SIGKILL to process group -{pgid}")
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
    return True


def cmd_stop(args):
    ensure_dirs()
    success = stop_container(args.container_id)
    sys.exit(0 if success else 1)


def main():
    parser = argparse.ArgumentParser(description="mycontainer - simplified container runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a container")
    run_parser.add_argument("--image", required=True, help="Image name")
    run_parser.add_argument("--cmd", default="", help="Command to run")

    subparsers.add_parser("ps", help="List running containers")

    stop_parser = subparsers.add_parser("stop", help="Stop a container")
    stop_parser.add_argument("container_id", help="Container ID")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "ps":
        cmd_ps(args)
    elif args.command == "stop":
        cmd_stop(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os
import tarfile
import tempfile
from pathlib import Path

LAYERS_DIR = Path(__file__).parent / "layers"
IMAGE_NAME = "testos"


def build_layer0_base():
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    layer_path = LAYERS_DIR / f"{IMAGE_NAME}_01_base.tar.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)

        dirs = [
            "bin", "usr/bin", "usr/lib", "lib", "tmp", "root",
            "etc", "var/tmp", "var/run", "home", "proc", "sys", "dev",
            "opt", "data",
        ]
        for d in dirs:
            (tmp_root / d).mkdir(parents=True, exist_ok=True)

        with open(tmp_root / "etc" / "passwd", "w") as f:
            f.write("root:x:0:0:root:/root:/bin/sh\n")
            f.write("nobody:x:65534:65534:nobody:/home:/bin/false\n")

        with open(tmp_root / "etc" / "group", "w") as f:
            f.write("root:x:0:\n")
            f.write("nobody:x:65534:\n")

        with open(tmp_root / "etc" / "hostname", "w") as f:
            f.write(f"{IMAGE_NAME}\n")

        with open(tmp_root / "root" / ".profile", "w") as f:
            f.write("export PATH=/bin:/usr/bin\nexport HOME=/root\n")

        with open(tmp_root / "tmp" / "base_layer_marker", "w") as f:
            f.write("This file is from base layer (layer 1)\n")

        with open(tmp_root / "etc" / "os-release", "w") as f:
            f.write('NAME="testos"\n')
            f.write('VERSION="1.0"\n')
            f.write('ID=testos\n')

        print("Creating layer 1 (base filesystem)...")
        with tarfile.open(layer_path, "w:gz") as tf:
            for item in sorted(tmp_root.rglob("*")):
                tf.add(item, arcname=str(item.relative_to(tmp_root)))

    print(f"Layer 1 created: {layer_path}")


def build_layer1_app():
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    layer_path = LAYERS_DIR / f"{IMAGE_NAME}_02_app.tar.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)

        (tmp_root / "opt" / "myapp").mkdir(parents=True, exist_ok=True)

        with open(tmp_root / "opt" / "myapp" / "hello.txt", "w") as f:
            f.write("Hello from testos layer 2!\n")

        with open(tmp_root / "opt" / "myapp" / "run.sh", "w") as f:
            f.write("#!/bin/sh\n")
            f.write("echo 'Running myapp from layer 2'\n")
            f.write("cat /opt/myapp/hello.txt\n")
        os.chmod(tmp_root / "opt" / "myapp" / "run.sh", 0o755)

        (tmp_root / "data").mkdir(parents=True, exist_ok=True)
        with open(tmp_root / "data" / "layer2_marker", "w") as f:
            f.write("This file comes from layer 2\n")

        (tmp_root / "tmp").mkdir(parents=True, exist_ok=True)
        with open(tmp_root / "tmp" / "from_layer2", "w") as f:
            f.write("Layer 2 adds this file\n")

        print("Creating layer 2 (app layer)...")
        with tarfile.open(layer_path, "w:gz") as tf:
            for item in sorted(tmp_root.rglob("*")):
                tf.add(item, arcname=str(item.relative_to(tmp_root)))

    print(f"Layer 2 created: {layer_path}")


def main():
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    for old_layer in LAYERS_DIR.glob(f"{IMAGE_NAME}_*.tar.gz"):
        old_layer.unlink()
        print(f"Removed old layer: {old_layer}")

    build_layer0_base()
    build_layer1_app()

    print("\nDone! Test image layers created in layers/")
    print("Run with: sudo python3 mycontainer.py run --image testos --cmd 'ls /'")
    print("Interactive: sudo python3 mycontainer.py run --image testos")


if __name__ == "__main__":
    main()

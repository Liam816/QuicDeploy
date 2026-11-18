"""Start/reuse a read-only sidecar before live deployment; never starts inference."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
import json
import hashlib

from .server import SERVICE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()
    port = int(os.environ.get("QUICDEPLOY_WEBVIZ_PORT", "8765"))
    host = os.environ.get("QUICDEPLOY_WEBVIZ_HOST", "0.0.0.0")
    from quicdeploy.config.resolve import resolve
    config = resolve(Path(args.config), model_card=Path(args.model) if args.model else None)
    kinematics = config["kinematics"]
    urdf = os.environ.get("QUICDEPLOY_WEBVIZ_URDF", kinematics.get("urdf", ""))
    frame = kinematics.get("frame", "world")
    joint_names = kinematics.get("joint_names")
    ends = kinematics.get("ends")
    end_links = [ends["left"], ends["right"]] if ends else None
    root = Path(__file__).resolve().parent.parent
    if not (root / "webviz/dist/index.html").is_file():
        raise RuntimeError("Build webviz first: cd webviz/frontend && npm ci && npm run build")

    def healthy():
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.4) as response:
                return json.load(response).get("service") == SERVICE
        except Exception:
            return False

    if not healthy():
        out = root / "outputs/webviz"
        out.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "webviz.server", "--host", host, "--port", str(port)]
        if urdf:
            command += ["--urdf", urdf]
        if joint_names:
            command += ["--joint-names", *joint_names, "--end-links", *end_links, "--frame", frame]
        with (out / "server.log").open("a") as log:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        (out / "server.pid").write_text(str(process.pid) + "\n")
        for _ in range(20):
            if healthy():
                break
            if process.poll() is not None:
                raise RuntimeError(f"Webviz failed to start; see {out / 'server.log'}")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Webviz startup timed out; see {out / 'server.log'}")
    with urlopen(f"http://127.0.0.1:{port}/api/config", timeout=0.5) as response:
        active = json.load(response)
    different_urdf = urdf and active.get("urdf_sha256") != hashlib.sha256(Path(urdf).read_bytes()).hexdigest()
    if (active.get("frame", "world") != frame
            or (joint_names and (different_urdf or active.get("end_links") != end_links
                                 or active.get("joint_names") != [joint_names[:6], joint_names[6:]]))):
        raise RuntimeError("Webviz uses a different kinematics profile. Restart it or use a different port.")
    print(f"Web visualization ready on port {port} (read-only; no browser motion controls)", flush=True)
    if different_urdf:
        print("Webviz is reusing a server with a different URDF. Restart it to load the new URDF.", flush=True)


if __name__ == "__main__":
    main()

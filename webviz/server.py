"""Standard-library HTTP/SSE/MJPEG sidecar. No CAN, camera capture or GPU access."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

SERVICE = "quicdeploy-webviz-v1"
ROOT = Path(__file__).resolve().parent


class State:
    def __init__(self, urdf=None, packages=None, joint_names=None, end_links=None):
        self.condition = threading.Condition()
        self.revision = 0
        self.received = None
        self.packet = {"session_id": None, "source": "live", "status": "waiting",
                       "current": None, "previous": None, "cameras": {}, "robot": None}
        self.images = {}
        self.urdf = Path(urdf).resolve() if urdf else None
        self.urdf_sha256 = hashlib.sha256(self.urdf.read_bytes()).hexdigest() if self.urdf else None
        self.packages = {name: Path(path).resolve() for name, path in (packages or {}).items()}
        self.joint_names = joint_names or [[f"{side}_joint{i}" for i in range(1, 7)] for side in ("left", "right")]
        self.end_links = end_links or ["left_Link6", "right_Link6"]

    def ingest(self, packet):
        if not isinstance(packet, dict) or not isinstance(packet.get("session_id"), str):
            raise ValueError("session_id is required")
        cameras, images = {}, {}
        for name, camera in packet.get("cameras", {}).items():
            if not isinstance(name, str) or not name.replace("_", "").isalnum():
                raise ValueError("invalid camera name")
            cameras[name] = {key: value for key, value in camera.items() if key != "jpeg"}
            if camera.get("jpeg"):
                images[name] = (float(camera["timestamp"]), base64.b64decode(camera["jpeg"], validate=True))
        packet = dict(packet, cameras=cameras)
        with self.condition:
            if packet["session_id"] != self.packet.get("session_id"):
                self.images.clear()
            else:
                # A skipped camera lock or final status packet keeps last-frame metadata.
                packet["cameras"] = dict(self.packet.get("cameras", {}), **cameras)
            self.images.update(images)
            self.packet = packet
            self.received = time.monotonic()
            self.revision += 1
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(self.packet, revision=self.revision, server_monotonic=time.monotonic(),
                        received_age_s=None if self.received is None else time.monotonic() - self.received)

    def config(self):
        return {"service": SERVICE, "read_only": True,
                "urdf_url": "/robot/" + self.urdf.name if self.urdf else None,
                "urdf_sha256": self.urdf_sha256,
                "packages": {name: f"/packages/{name}/" for name in self.packages},
                "frame": "world", "units": "m", "end_links": self.end_links,
                "joint_names": self.joint_names}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Avoid per-frame logging; failures still have HTTP status codes.
        if args and str(args[1] if len(args) > 1 else "") not in ("200", "204"):
            super().log_message(fmt, *args)

    @property
    def state(self):
        return self.server.state

    def respond(self, data, content_type="application/json", status=200):
        if not isinstance(data, bytes):
            data = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def file(self, root, relative):
        root = root.resolve()
        path = (root / unquote(relative)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            self.respond({"error": "file not found"}, status=404)
            return
        self.respond(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_GET(self):
        route = urlsplit(self.path).path
        try:
            if route == "/api/health":
                self.respond({"service": SERVICE})
            elif route == "/api/config":
                self.respond(self.state.config())
            elif route == "/api/state":
                self.respond(self.state.snapshot())
            elif route == "/api/events":
                self.events()
            elif route.startswith("/api/camera/"):
                self.camera(unquote(route.removeprefix("/api/camera/")))
            elif route.startswith("/robot/") and self.state.urdf:
                self.file(self.state.urdf.parent, route.removeprefix("/robot/"))
            elif route.startswith("/packages/"):
                name, _, relative = route.removeprefix("/packages/").partition("/")
                if name in self.state.packages:
                    self.file(self.state.packages[name], relative)
                else:
                    self.respond({"error": "unknown URDF package"}, status=404)
            elif route == "/" or route == "/index.html":
                if not (ROOT / "dist/index.html").exists():
                    self.respond(b"Build frontend first: cd webviz/frontend && npm ci && npm run build",
                                 "text/plain; charset=utf-8", 503)
                else:
                    self.file(ROOT / "dist", "index.html")
            else:
                self.file(ROOT / "dist", route.lstrip("/"))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_POST(self):
        # Ingest is a loopback-only producer API with a non-simple header; no CORS.
        if (self.client_address[0] not in ("127.0.0.1", "::1") or
                self.headers.get("X-QuicDeploy-Ingest") != "1"):
            self.close_connection = True
            self.respond({"error": "local producer only"}, status=403)
            return
        if urlsplit(self.path).path != "/api/ingest":
            self.close_connection = True
            self.respond({"error": "not found"}, status=404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8 * 1024 * 1024:
                raise ValueError("body size must be 1..8 MiB")
            self.connection.settimeout(3)
            packet = json.loads(self.rfile.read(size), parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON value")))
            self.state.ingest(packet)
            self.respond(b"", status=204)
        except (ValueError, KeyError, TypeError) as error:
            self.close_connection = True
            self.respond({"error": str(error)}, status=400)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.connection.settimeout(3)
        revision = -1
        while True:
            with self.state.condition:
                if self.state.revision == revision:
                    self.state.condition.wait(timeout=0.8)
                packet = self.state.snapshot()
                revision = packet["revision"]
            payload = json.dumps(packet, allow_nan=False, separators=(",", ":"))
            self.wfile.write(f"id: {revision}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

    def camera(self, name):
        if name not in ("head", "left_wrist", "right_wrist"):
            self.respond({"error": "unknown camera"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.connection.settimeout(3)
        last = None
        while True:
            pending = False
            with self.state.condition:
                frame = self.state.images.get(name)
                if frame is None or (frame[0], self.state.packet.get("session_id")) == last:
                    self.state.condition.wait(timeout=1)
                    pending = True
                else:
                    stamp, jpeg = frame
                    last = (stamp, self.state.packet.get("session_id"))
            if pending:
                # Release abandoned streams even when inference has stopped producing frames.
                if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
                    return
                continue
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                             str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            self.wfile.flush()


def make_server(host="127.0.0.1", port=8765, *, urdf=None, packages=None, joint_names=None, end_links=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.state = State(urdf, packages, joint_names, end_links)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--urdf", type=Path)
    parser.add_argument("--package", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--joint-names", nargs=12, metavar="JOINT", help="six left joints then six right joints")
    parser.add_argument("--end-links", nargs=2, metavar="LINK", help="left and right controlled wrist links")
    parser.add_argument("--demo", action="store_true", help="synthetic data, no hardware")
    parser.add_argument("--replay", type=Path, help="directory of diagnostic chunk_*.npz, no hardware")
    args = parser.parse_args()
    if args.demo and args.replay:
        parser.error("choose --demo or --replay")
    if args.urdf and not args.urdf.is_file():
        parser.error("URDF file does not exist")
    packages = dict(item.split("=", 1) for item in args.package)
    if any(not path or not Path(path).is_dir() for path in packages.values()):
        parser.error("URDF package directory does not exist")
    names = [args.joint_names[:6], args.joint_names[6:]] if args.joint_names else None
    server = make_server(args.host, args.port, urdf=args.urdf, packages=packages,
                         joint_names=names, end_links=args.end_links)
    if args.demo or args.replay:
        from .offline import feed
        threading.Thread(target=feed, args=(server.state, args.replay), daemon=True).start()
    print(f"Web visualization: http://{args.host}:{args.port} (read-only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

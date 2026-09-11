"""HTTP API and static host for the exception queue UI.

Stdlib ``http.server``: one process, one workspace, a handful of JSON routes
and a built React bundle. It is a review tool for a small team, not a public
service, so it binds to loopback by default and guards writes with a shared
token rather than pretending to have real identity.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import vision
from .flagstore import ESCALATION_DAYS, FlagRecord
from .workspace import MAX_UPLOAD_BYTES, Paths, UploadRejected, Workspace

UI_DIST = Path(__file__).parent / "ui" / "dist"
MAX_BODY = 64 * 1024


def record_json(record: FlagRecord, now: datetime | None = None) -> dict[str, Any]:
    payload = record.to_dict()
    payload["age_days"] = record.age_days(now)
    payload["escalated"] = record.escalated(now)
    return payload


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "invoice-audit"

    def __init__(
        self, *args: Any, workspace: Workspace, token: str, demo: bool = False, **kw: Any
    ) -> None:
        self.workspace = workspace
        self.token = token
        self.demo = demo
        super().__init__(*args, **kw)

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # the audit trail that matters is the flag store

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _authorised(self) -> bool:
        """Reads are open on loopback; writes need the token.

        Deliberately modest: it stops a stray script or another user on a shared
        machine from resolving flags, and it is not a substitute for real auth
        if this ever leaves a trusted network.
        """
        if self.demo:
            # A public demo has no one to hand a token to. Writes are open, and
            # everything they can touch is disposable: the data resets on
            # restart and model calls are capped.
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        return secrets.compare_digest(supplied, self.token)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        ws = self.workspace
        now = datetime.now(timezone.utc)

        routes = {
            "/api/summary": lambda: ws.summary(),
            "/api/flags": lambda: [record_json(r, now) for r in ws.flag_store.all()],
            "/api/invoices": lambda: ws.invoices(),
            "/api/trends": lambda: {
                "months": ws.trends(),
                "repeats": ws.repeats(),
            },
            "/api/taxonomy": self._taxonomy,
            "/api/failures": lambda: ws.failures,
            "/api/mode": lambda: {
                "demo": self.demo,
                "vision_calls_used": vision.calls_made(),
                "vision_call_limit": int(
                    os.environ.get("INVOICE_AUDIT_VISION_LIMIT", "0")
                ),
            },
        }
        if path in routes:
            self._json(200, routes[path]())
            return

        self._static(path)

    def _taxonomy(self) -> list[dict[str, Any]]:
        from .flags import FLAGS

        return [
            {
                "id": f.id,
                "severity": f.severity.value,
                "description": f.description,
                "detection": f.detection.value,
                "inputs": f.inputs,
                "phase": f.phase,
            }
            for f in FLAGS.values()
        ]

    def _static(self, path: str) -> None:
        if not UI_DIST.exists():
            self._send(
                503,
                b"UI not built. Run: npm --prefix ui install && npm --prefix ui run build",
                "text/plain; charset=utf-8",
            )
            return

        # Decode first: without this, "..%2f.." never becomes a separator and
        # the guard below is bypassed into the SPA fallback rather than a 403.
        rel = unquote(path).lstrip("/") or "index.html"
        target = (UI_DIST / rel).resolve()
        try:
            target.relative_to(UI_DIST.resolve())
        except ValueError:
            self._error(403, "outside the bundle")
            return
        if not target.is_file():
            target = UI_DIST / "index.html"  # SPA fallback
        if not target.is_file():
            self._error(404, "not found")
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorised():
            self._error(401, "a valid token is required to change anything")
            return

        if path == "/api/upload":
            self._upload()
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(413, "body too large")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error(400, "body was not JSON")
            return

        if path == "/api/rescan":
            self.workspace.rescan()
            self._json(200, self.workspace.summary())
            return

        if path.startswith("/api/flags/"):
            self._resolve(path.rsplit("/", 1)[-1], body)
            return

        self._error(404, "no such endpoint")

    def _upload(self) -> None:
        """Raw file bytes with the name in a header.

        Not multipart: the browser can POST a File object directly, which skips
        a parser this server has no business containing and keeps the upload a
        single stream rather than a buffered form.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._error(400, "no file was sent")
            return
        if length > MAX_UPLOAD_BYTES:
            self._error(
                413,
                f"that file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )
            return

        filename = unquote(self.headers.get("X-Filename", "")).strip()
        if not filename:
            self._error(400, "the upload is missing its filename")
            return

        # Read exactly Content-Length; a short read means the browser gave up
        # mid-upload and the bytes on disk would be a truncated invoice.
        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(min(65536, length - len(data)))
            if not chunk:
                self._error(400, "the upload ended early — try again")
                return
            data.extend(chunk)

        try:
            payload = self.workspace.ingest(filename, bytes(data))
        except UploadRejected as exc:
            self._error(422, str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, f"the audit failed: {exc}")
            return

        self._json(201, payload)

    def _resolve(self, fingerprint: str, body: dict[str, Any]) -> None:
        store = self.workspace.flag_store
        action = body.get("action")
        try:
            if action == "reopen":
                record = store.reopen(fingerprint)
            elif action in ("resolve", "dismiss"):
                who = (body.get("by") or "").strip()
                note = (body.get("note") or "").strip()
                if not who or not note:
                    # Provenance is the point of the queue. A resolution with
                    # no name and no reason is worse than leaving it open.
                    self._error(400, "both a name and a note are required")
                    return
                record = store.resolve(
                    fingerprint, resolution=note, by=who,
                    dismissed=(action == "dismiss"),
                )
            else:
                self._error(400, f"unknown action {action!r}")
                return
        except KeyError as exc:
            self._error(404, str(exc))
            return

        store.flush()
        self._json(200, record_json(record))


def serve(
    workspace: Workspace,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    open_browser: bool = True,
    demo: bool = False,
) -> None:
    token = token or secrets.token_urlsafe(16)
    handler = partial(ApiHandler, workspace=workspace, token=token, demo=demo)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if exc.errno in (48, 98):  # EADDRINUSE on BSD / Linux
            raise SystemExit(
                f"Port {port} is already in use — another queue is probably "
                f"running. Stop it, or pass --port {port + 1}."
            ) from exc
        raise

    url = f"http://{host}:{port}/" + ("" if demo else f"?token={token}")
    summary = workspace.summary()
    print(f"Invoice audit UI on http://{host}:{port}/")
    if demo:
        print("  mode      DEMO — writes are open, data resets on restart")
    print(f"  invoices  {summary['invoices']} ({summary['flagged']} flagged)")
    print(f"  flags     {summary['open_flags']} open, {summary['escalated']} escalated")
    if not demo:
        print(f"  token     {token}")
    if not UI_DIST.exists():
        print("  ! UI bundle missing — run: npm --prefix ui install && npm --prefix ui run build")
    print("  ctrl-c to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        workspace.flag_store.flush()

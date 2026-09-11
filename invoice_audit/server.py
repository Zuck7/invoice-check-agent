"""The exception queue as a local web app.

`queue`/`resolve` on the CLI are fine for one reviewer. This is the same store
behind a page three people can work at once, which is what phase 3 actually
asked for.

Deliberately stdlib ``http.server``: it binds to localhost, holds the flag
store in one process, and needs no framework to serve one page and one
endpoint. It is a review tool for a small team, not a public service -- so it
refuses to bind to anything but the loopback interface unless told otherwise,
and it has no authentication of its own.
"""

from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .flagstore import ESCALATION_DAYS, FlagRecord, FlagStore, Status

TEMPLATE = Path(__file__).parent / "ui" / "queue.html"
MAX_BODY = 64 * 1024


def record_json(record: FlagRecord, now: datetime | None = None) -> dict[str, Any]:
    """One queue row, shaped for the page."""
    payload = record.to_dict()
    payload["age_days"] = record.age_days(now)
    payload["escalated"] = record.escalated(now)
    return payload


def page(store: FlagStore, live: bool = True) -> str:
    """Render the template with the store's data baked in.

    One template, two data sources: the live server injects the real store, and
    ``build_preview`` injects a sample so the page can be shared without a
    machine to run it on.
    """
    now = datetime.now(timezone.utc)
    data = {
        "flags": [record_json(r, now) for r in store.all()],
        "meta": {
            "live": live,
            "path": str(store.path) if store.path else "in memory",
            "escalation_days": ESCALATION_DAYS,
            "generated_at": now.isoformat(),
        },
    }
    html = TEMPLATE.read_text(encoding="utf-8")
    seed = (
        "<script>window.__QUEUE__ = "
        + json.dumps(data).replace("</", "<\\/")
        + ";</script>\n"
    )
    # Ahead of the page script, which reads window.__QUEUE__ on load.
    return seed + html


class QueueHandler(BaseHTTPRequestHandler):
    server_version = "invoice-audit"

    def __init__(self, *args: Any, store: FlagStore, **kw: Any) -> None:
        self.store = store
        super().__init__(*args, **kw)

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # the audit log that matters is the flag store, not access logs

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

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, page(self.store).encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/flags":
            now = datetime.now(timezone.utc)
            self._json(200, [record_json(r, now) for r in self.store.all()])
        else:
            self._error(404, "no such page")

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/api/flags/"):
            self._error(404, "no such endpoint")
            return

        fingerprint = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(413, "body too large")
            return

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error(400, "body was not JSON")
            return

        action = body.get("action")
        try:
            if action == "reopen":
                record = self.store.reopen(fingerprint)
            elif action in ("resolve", "dismiss"):
                who = (body.get("by") or "").strip()
                note = (body.get("note") or "").strip()
                if not who or not note:
                    # Provenance is the point of the queue. A resolution with
                    # no name and no reason is worse than leaving it open.
                    self._error(400, "both a name and a note are required")
                    return
                record = self.store.resolve(
                    fingerprint, resolution=note, by=who,
                    dismissed=(action == "dismiss"),
                )
            else:
                self._error(400, f"unknown action {action!r}")
                return
        except KeyError as exc:
            self._error(404, str(exc))
            return

        self.store.flush()
        self._json(200, record_json(record))


def serve(
    store: FlagStore,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    handler = partial(QueueHandler, store=store)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if exc.errno in (48, 98):  # EADDRINUSE on BSD / Linux
            raise SystemExit(
                f"Port {port} is already in use — another queue is probably "
                f"running. Stop it, or pass --port {port + 1}."
            ) from exc
        raise
    url = f"http://{host}:{port}/"
    print(f"Exception queue on {url}")
    print(f"  store   {store.path or 'in memory'}")
    print(f"  flags   {len(store)} ({len(store.open_records())} open)")
    print("  ctrl-c to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        store.flush()

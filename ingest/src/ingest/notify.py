"""Generic push notifications (self-hosted ntfy) and the MFA code form.

Push text is fixed and generic: never amounts, account names or institutions
beyond what the caller passes as a short label.
"""

from __future__ import annotations

import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs


class Notifier(Protocol):
    def notify(self, message: str, click_url: str | None = None) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    def notify(self, message: str, click_url: str | None = None) -> None:
        self.sent.append((message, click_url))


class WebhookNotifier:
    """POST {"title", "message", "url"} as JSON with a bearer token. For a
    self-hosted push service that delivers straight to the phone (for example
    an Archimedes endpoint that bypasses its agents)."""

    def __init__(self, url: str, token: str | None = None):
        self.url, self.token = url, token

    def notify(self, message: str, click_url: str | None = None) -> None:
        import json

        body = json.dumps({"title": "Finance sync", "message": message, "url": click_url}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError:
            pass


class NtfyNotifier:
    def __init__(self, url: str, topic: str, token: str | None = None):
        self.endpoint = f"{url.rstrip('/')}/{topic}"
        self.token = token

    def notify(self, message: str, click_url: str | None = None) -> None:
        req = urllib.request.Request(self.endpoint, data=message.encode(), method="POST")
        req.add_header("Title", "Finance sync")
        if click_url:
            req.add_header("Click", click_url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError:
            pass  # a lost notification must not crash a run; the MFA wait times out cleanly


class MfaTimeout(RuntimeError):
    pass


class MfaNeeded(RuntimeError):
    """The bank asked for a code during an unattended run. Nobody is there to
    answer at night, so stop at once; the human signs in once by hand
    (``ingest scrape <bank> --interactive``) and the profile remembers the device."""


class NoWaitCodes:
    def __init__(self, notifier: "Notifier", institution: str):
        self.notifier, self.institution = notifier, institution

    def wait_for_code(self, timeout_s: float) -> str:
        self.notifier.notify(f"Finance sync: {self.institution} wants a verification code. Run it by hand when you can.")
        raise MfaNeeded("verification code requested during an unattended run")


class TerminalCodes:
    """For a run started by hand: ask in the terminal."""

    def wait_for_code(self, timeout_s: float) -> str:
        import getpass

        code = getpass.getpass("Verification code from the bank: ").strip()
        if not code:
            raise MfaTimeout("no code entered")
        return code


class CodeProvider(Protocol):
    def wait_for_code(self, timeout_s: float) -> str: ...


FORM = """<!doctype html><meta name=viewport content="width=device-width">
<title>Verification code</title>
<form method=post style="font:16px sans-serif;margin:2em">
<label>Verification code<br><input name=code autocomplete=one-time-code inputmode=numeric autofocus></label>
<button>Send</button></form>"""


class MfaServer:
    """A one-shot form on the LAN/tailnet. The path carries a random token, so a
    stray visitor can't submit a code, and the server stops once a code arrives."""

    def __init__(self, bind: str, port: int, public_base: str, notifier: Notifier):
        self.bind, self.port, self.public_base = bind, port, public_base.rstrip("/")
        self.notifier = notifier

    def wait_for_code(self, timeout_s: float) -> str:
        token = secrets.token_urlsafe(16)
        got: dict[str, str] = {}
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep codes out of logs
                pass

            def _ok_path(self) -> bool:
                if self.path != f"/{token}":
                    self.send_error(404)
                    return False
                return True

            def do_GET(self):
                if self._ok_path():
                    self._send(FORM)

            def do_POST(self):
                if not self._ok_path():
                    return
                length = min(int(self.headers.get("Content-Length") or 0), 1024)
                code = parse_qs(self.rfile.read(length).decode()).get("code", [""])[0].strip()
                if not code or len(code) > 12 or not code.isalnum():
                    self._send(FORM.replace("<form", "<p>Codes are letters and digits only.</p><form"))
                    return
                got["code"] = code
                done.set()
                self._send("<p style='font:16px sans-serif;margin:2em'>Thanks. You can close this page.</p>")

            def _send(self, body: str):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer((self.bind, self.port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.notifier.notify(
                "Finance sync is waiting for a verification code.", f"{self.public_base}/{token}"
            )
            if not done.wait(timeout_s):
                raise MfaTimeout(f"no verification code within {int(timeout_s)}s")
            return got["code"]
        finally:
            server.shutdown()
            server.server_close()


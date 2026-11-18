"""A local fake bank: login, MFA, accounts page with a promo pop-up, a CSV
download and a money-moving link that the allowlist must block.

Everything here is made up. No real institution, credentials or data.
"""

from __future__ import annotations

import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USERNAME = "4500123456789012"
PASSWORD = "correct horse battery"
MFA_CODE = "123456"
HOLDER = "Jane Quixote"
BALANCE = "1,234.56"

CSV = (
    "2026-11-02,TIM HORTONS #1234,4.25,\n"
    "2026-11-02,TIM HORTONS #1234,4.25,\n"
    "2026-11-03,PAYROLL DEPOSIT,,2500.00\n"
)

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title></head>
<body>{body}</body></html>"""

LOGIN = """<h1>Sign on</h1>
<form method=post action=/login>
<label>Card number <input name=user></label>
<label>Password <input name=password type=password></label>
<button>Sign on</button></form>"""

MFA = """<h1>Verify your identity</h1>
<form method=post action=/mfa>
<label>Verification code <input name=code></label>
<label><input type=checkbox name=remember> Remember this device</label>
<button>Verify</button></form>"""

# Variant where the bank sends the code only after "Send code" is pressed.
MFA_SEND = """<h1>Verify your identity</h1>
<button id=send onclick="fetch('/mfa/send', {method: 'POST'}).then(() => { document.getElementById('f').hidden = false })">Send code</button>
<form id=f method=post action=/mfa hidden>
<label>Verification code <input name=code></label>
<label><input type=checkbox name=remember> Remember this device</label>
<button>Verify</button></form>"""

DOWNLOAD_PAGE = """<h1>Download transactions</h1>
<nav><a href="/download">Download page</a> <a href="/lazy">Long page</a></nav>
<label>Account <select id=acct>
  <option value="a1">Chequing 4455-1234</option>
  <option value="a2">Visa ending 9911</option>
  <option value="a3">Visa ending 9912</option>
</select></label>
<label>Format <select id=fmt><option>Quicken</option><option>CSV</option></select></label>
<button onclick="if (acct.value === 'a3') { document.getElementById('msg').hidden = false } else { location.href='/export/transactions.csv?acct=' + acct.value + '&fmt=' + fmt.value }">Download</button>
<p id=msg role=alert hidden>There are no transactions found that meet your request.</p>"""

# An "Extras" panel far down the page that only renders once scrolled into view.
LAZY_PAGE = """<h1>Home</h1><div style="height:4000px">accounts</div>
<aside id=extras style="min-height:40px"></aside>
<script>
new IntersectionObserver((e, o) => {
  if (e[0].isIntersecting) { extras.innerHTML = '<h2>Extras</h2><a href="/download">Download Transactions</a>'; o.disconnect() }
}).observe(extras)
</script>"""

ACCOUNTS = """<h1>Accounts</h1>
<p>Welcome back, {holder}</p>
<section aria-label="Chequing">
  <h2>Chequing account 4500 1234 5678 9012</h2>
  <p>Balance ${balance}</p>
  <p data-testid="plan-balance">Total balance ${balance}</p>
  <a href="/accounts/77812/history">Account history</a>
  <a href="/download">Download page</a>
  <a href="/transfer?from=77812">Transfer funds</a>
  <button onclick="fetch('/api/v2/quick', {{method: 'POST', body: 'amount=500'}}).catch(() => {{}})">Quick action</button>
  <button id=dl onclick="location.href='/export/transactions.csv?acct=77812'">{download_label}</button>
</section>
<script>fetch('/beacon?webdriver=' + navigator.webdriver + '&ua=' + encodeURIComponent(navigator.userAgent))</script>
<div role=dialog aria-label="Special offer" id=promo>
  <h2>Special offer for {holder}</h2>
  <button onclick="document.getElementById('promo').remove()">No thanks</button>
</div>"""


class FakeBank:
    def __init__(self, *, download_label: str = "Download transactions", require_mfa: bool = True, send_code: bool = False):
        self.download_label = download_label
        self.require_mfa = require_mfa
        self.send_code = send_code
        self.exports: list[str] = []
        self.hits: Counter[str] = Counter()
        self.beacons: list[dict[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        bank = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _page(self, title, body, status=200):
                data = PAGE.format(title=title, body=body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, to, cookie=None):
                self.send_response(303)
                self.send_header("Location", to)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()

            def _trusted(self):
                return "trusted_device=1" in (self.headers.get("Cookie") or "")

            def _form(self):
                n = int(self.headers.get("Content-Length") or 0)
                return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

            def do_GET(self):
                path = urlsplit(self.path).path
                bank.hits[f"GET {path}"] += 1
                if path == "/login":
                    self._page("Sign on", LOGIN)
                elif path == "/mfa":
                    self._page("Verify", MFA_SEND if bank.send_code else MFA)
                elif path == "/download":
                    self._page("Download", DOWNLOAD_PAGE)
                elif path == "/lazy":
                    self._page("Long page", LAZY_PAGE)
                elif path == "/accounts":
                    self._page(
                        "Accounts",
                        ACCOUNTS.format(holder=HOLDER, balance=BALANCE, download_label=bank.download_label),
                    )
                elif path == "/export/transactions.csv":
                    bank.exports.append(urlsplit(self.path).query)
                    data = CSV.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition", 'attachment; filename="transactions.csv"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/beacon":
                    bank.beacons.append({k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})
                    self._page("ok", "")
                elif path == "/transfer":
                    self._page("Send money", "<h1>Send money</h1>")
                else:
                    self._page("Not found", "<h1>Not found</h1>", 404)

            def do_POST(self):
                path = urlsplit(self.path).path
                bank.hits[f"POST {path}"] += 1
                form = self._form()
                if path == "/login":
                    if form.get("user") == USERNAME and form.get("password") == PASSWORD:
                        # A remembered device skips the code, as banks do.
                        self._redirect("/mfa" if bank.require_mfa and not self._trusted() else "/accounts")
                    else:
                        self._page("Sign on", "<h1>Sign on</h1><p role=alert>Incorrect details</p>" + LOGIN)
                elif path == "/mfa/send":
                    self._page("ok", "")
                elif path == "/mfa":
                    if form.get("code") == MFA_CODE:
                        trust = "trusted_device=1; Path=/; Max-Age=31536000; HttpOnly"
                        self._redirect("/accounts", trust if form.get("remember") or not bank.send_code else None)
                    else:
                        self._redirect("/mfa")
                else:
                    self._page("Not found", "<h1>Not found</h1>", 404)

        return Handler


def script_dict(base: str) -> dict:
    return {
        "institution": "fakebank",
        "currency": "CAD",
        "allowlist": {
            "navigation": [rf"^{base}/(login|mfa|accounts|download|lazy|export/transactions\.csv)"],
            "hosts": ["127.0.0.1"],
            "post": [rf"^{base}/(login|mfa|mfa/send)$"],
        },
        "login": [
            {"action": "goto", "intent": "open the sign-on page", "url": f"{base}/login"},
            {"action": "fill", "intent": "enter the card number", "locator": {"label": "Card number"}, "secret": "fakebank/username"},
            {"action": "fill", "intent": "enter the password", "locator": {"label": "Password"}, "secret": "fakebank/password"},
            {"action": "click", "intent": "submit the sign-on form", "locator": {"role": "button", "name": "Sign on"}},
            {
                "action": "mfa",
                "intent": "answer a verification prompt if one appears",
                "detect": {"role": "heading", "name": "Verify your identity"},
                "input": {"label": "Verification code"},
                "submit": {"role": "button", "name": "Verify"},
                "timeout_ms": 2000,
            },
            {"action": "expect", "intent": "land on the accounts page", "url": "/accounts$", "locator": {"role": "heading", "name": "Accounts"}},
        ],
        "steps": [
            {"action": "dismiss", "intent": "close the promo pop-up", "locator": {"role": "button", "name": "No thanks"}, "timeout_ms": 2000},
            {
                "action": "download",
                "intent": "download transactions",
                "locator": {"role": "button", "name": "Download transactions"},
                "save_as": "chequing__{today}.{ext}",
            },
        ],
    }

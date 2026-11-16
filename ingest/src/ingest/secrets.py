"""Bank logins from Vaultwarden, via the ``bw`` CLI.

Secret names look like ``simplii/password``: the part before the slash is the
vault item name, the part after is ``username``, ``password``, ``totp`` or a
custom field name. Values live only in this process's memory for one run: they
are never put in the container environment, logs, CLI arguments, traces or
screenshots.

Files under the credentials directory (scraper UID only, mode 600):
  bw_server         Vaultwarden URL
  bw_clientid       API key client id of the dedicated bank-logins account
  bw_clientsecret   API key client secret
  bw_password       master password (needed to unlock after an API-key login)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class SecretError(RuntimeError):
    pass


class SecretStore(Protocol):
    def get(self, name: str) -> str: ...

    def close(self) -> None: ...


class DictStore:
    """In-memory store for tests and the fake bank."""

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretError(f"no secret named {name}") from None

    def close(self) -> None:
        self._values.clear()


class BitwardenStore:
    def __init__(self, creds_dir: Path, bw: str = "bw"):
        self.creds_dir = creds_dir
        self.bw = bw
        # bw keeps login state in its appdata dir; a fresh temp dir per run means
        # nothing survives the run on disk.
        self._appdata = tempfile.TemporaryDirectory(prefix="bw-")
        self._session: str | None = None
        self._items: dict[str, dict] = {}

    def _read(self, name: str) -> str:
        path = self.creds_dir / name
        try:
            return path.read_text().strip()
        except FileNotFoundError:
            raise SecretError(f"missing credentials file {path}") from None

    def _run(self, *args: str, extra_env: dict[str, str] | None = None) -> str:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self._appdata.name,
            "BITWARDENCLI_APPDATA_DIR": self._appdata.name,
            "BW_NOINTERACTION": "true",
        }
        if self._session:
            env["BW_SESSION"] = self._session
        env.update(extra_env or {})
        proc = subprocess.run([self.bw, *args], env=env, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            # bw's stderr never contains secret values, but keep it short anyway.
            raise SecretError(f"bw {args[0]} failed: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def unlock(self) -> None:
        self._run("config", "server", self._read("bw_server"))
        self._run(
            "login",
            "--apikey",
            extra_env={"BW_CLIENTID": self._read("bw_clientid"), "BW_CLIENTSECRET": self._read("bw_clientsecret")},
        )
        self._session = self._run(
            "unlock", "--raw", "--passwordfile", str(self.creds_dir / "bw_password")
        ).strip()
        if not self._session:
            raise SecretError("bw unlock returned no session")

    def _item(self, item_name: str) -> dict:
        if self._session is None:
            self.unlock()
        if item_name not in self._items:
            self._items[item_name] = json.loads(self._run("get", "item", item_name))
        return self._items[item_name]

    def get(self, name: str) -> str:
        item_name, _, field = name.partition("/")
        if not field:
            raise SecretError(f"secret name {name!r} must look like item/field")
        item = self._item(item_name)
        login = item.get("login") or {}
        if field in ("username", "password"):
            value = login.get(field)
        elif field == "totp":
            value = self._run("get", "totp", item["id"]).strip()
        else:
            value = next((f.get("value") for f in item.get("fields") or [] if f.get("name") == field), None)
        if not value:
            raise SecretError(f"secret {name} is empty or missing")
        return value

    def all_values(self) -> list[str]:
        """Every username and custom field across the bank items, for the repair blocking check."""
        if self._session is None:
            self.unlock()
        items = json.loads(self._run("list", "items"))
        values = []
        for item in items:
            login = item.get("login") or {}
            values += [login.get("username") or ""]
            values += [f.get("value") or "" for f in item.get("fields") or []]
        return [v for v in values if v]

    def close(self) -> None:
        self._items.clear()
        if self._session:
            try:
                self._run("lock")
            except SecretError:
                pass
            self._session = None
        self._appdata.cleanup()

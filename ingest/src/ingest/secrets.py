"""Bank logins, encrypted on the VM with systemd-creds.

Secret names in bank scripts look like ``simplii/password``: bank, then field.
Each one is a file ``<bank>.<field>.cred`` in /etc/ingest/credentials (root
only), written by deploy/ingest/set-credential.sh and encrypted with this VM's
credential key. For a scrape, deploy/ingest/scrape.sh decrypts only that bank's
files into a RAM directory, mounts it read-only into the scraper container as
``<bank>.<field>`` files, and deletes it when the run ends.

Values live only in this process's memory for one run: they are never put in
the container environment, logs, CLI arguments, traces or screenshots.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Protocol

NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


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


def file_name(name: str) -> str:
    """'simplii/password' -> 'simplii.password'."""
    bank, _, field = name.partition("/")
    if not (NAME.fullmatch(bank) and NAME.fullmatch(field)):
        raise SecretError(f"secret name {name!r} must look like bank/field (lowercase, digits, - and _)")
    return f"{bank}.{field}"


class DirStore:
    """Decrypted ``<bank>.<field>`` files in a RAM directory (inside the scraper container)."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        key = file_name(name)
        if key not in self._cache:
            try:
                value = (self.directory / key).read_text()
            except FileNotFoundError:
                raise SecretError(f"no credential {name}: add it with set-credential.sh {name.replace('/', ' ')}") from None
            if not value:
                raise SecretError(f"credential {name} is empty")
            self._cache[key] = value
        return self._cache[key]

    def close(self) -> None:
        self._cache.clear()


class SystemdCredsStore:
    """The encrypted ``*.cred`` files themselves, decrypted with systemd-creds (root
    on the VM). The repair command uses it to learn which values must never leave."""

    def __init__(self, directory: Path, binary: str = "systemd-creds"):
        self.directory = directory
        self.binary = binary

    def _decrypt(self, path: Path) -> str:
        proc = subprocess.run(
            [self.binary, "decrypt", f"--name={path.name.removesuffix('.cred')}", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise SecretError(f"could not decrypt {path.name}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def get(self, name: str) -> str:
        path = self.directory / f"{file_name(name)}.cred"
        if not path.exists():
            raise SecretError(f"no credential {name}")
        return self._decrypt(path)

    def all_values(self) -> list[str]:
        return [v for v in (self._decrypt(p) for p in sorted(self.directory.glob("*.cred"))) if v]

    def close(self) -> None:
        pass

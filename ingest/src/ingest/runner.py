"""Run one bank script, deterministically, once.

Rules enforced here rather than left to each script:
  * one login attempt per run: no retries anywhere; any failure stops the run;
  * every request goes through the allowlist; money-moving URLs are always aborted,
    and an aborted navigation stops the run as an unexpected page;
  * no Playwright tracing or video, ever, and no screenshots during login;
  * secret values only ever reach ``locator.fill`` and are redacted from errors.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .notify import CodeProvider, Notifier
from .scrub import scrub_snapshot, url_pattern
from .secrets import SecretStore
from .steps import Script, Step

log = logging.getLogger("ingest.runner")


class UnexpectedPage(RuntimeError):
    pass


@dataclass
class RunConfig:
    raw_root: Path
    profile_dir: Path
    bundle_root: Path
    headless: bool = False
    channel: str | None = "chrome"
    # Chrome's own process sandbox (renderer isolation). A security control, not
    # a stealth one: with it off, a hostile page that exploits the renderer can
    # read everything this UID can, including the vault credentials.
    chromium_sandbox: bool = True
    # From the deployment's .env (INGEST_LOCALE, INGEST_TIMEZONE). None = the
    # system's, which in a container is UTC: set them to match where you are.
    locale: str | None = None
    timezone_id: str | None = None
    step_timeout_ms: int = 20_000
    dismiss_wait_ms: int = 4_000
    mfa_detect_ms: int = 8_000
    mfa_timeout_s: float = 300
    failure_screenshots: bool = True


@dataclass
class RunResult:
    institution: str
    ok: bool
    downloads: list[Path] = field(default_factory=list)
    aborted_requests: list[tuple[str, str]] = field(default_factory=list)
    failure_bundle: Path | None = None
    error: str | None = None


class Runner:
    def __init__(self, script: Script, config: RunConfig, secrets: SecretStore, codes: CodeProvider, notifier: Notifier):
        self.script = script
        self.config = config
        self.secrets = secrets
        self.codes = codes
        self.notifier = notifier
        self._used_secrets: list[str] = []
        self._blocked_navigation: str | None = None
        self._extra_pages = 0

    # -- request filtering -------------------------------------------------

    def _route(self, route, result: RunResult) -> None:
        req = route.request
        verdict = self.script.allowlist.decide(req.url, req.method, req.is_navigation_request())
        if verdict == "allow":
            route.continue_()
            return
        pattern = url_pattern(req.url)
        result.aborted_requests.append((pattern, verdict))
        log.info("aborted %s %s (%s)", req.method, pattern, verdict)
        if req.is_navigation_request():
            try:
                top_level = req.frame.parent_frame is None
            except PlaywrightError:
                top_level = True  # can't tell (e.g. service worker): treat as the page
            if top_level:
                self._blocked_navigation = f"{pattern} ({verdict})"
        route.abort("blockedbyclient")

    # -- steps ---------------------------------------------------------------

    def _timeout(self, step: Step) -> int:
        return step.timeout_ms or self.config.step_timeout_ms

    def _secret(self, name: str) -> str:
        value = self.secrets.get(name)
        self._used_secrets.append(value)
        return value

    def _run_step(self, page, step: Step, result: RunResult) -> None:
        t = self._timeout(step)
        a = step.action
        if a == "goto":
            page.goto(step.url, timeout=t)
        elif a == "fill":
            step.locator.resolve(page).fill(self._secret(step.secret), timeout=t)
        elif a == "click":
            step.locator.resolve(page).click(timeout=t)
        elif a == "wait":
            step.locator.resolve(page).wait_for(state="visible", timeout=t)
        elif a == "dismiss":
            loc = step.locator.resolve(page)
            try:
                loc.wait_for(state="visible", timeout=step.timeout_ms or self.config.dismiss_wait_ms)
            except PlaywrightTimeout:
                log.info("nothing to dismiss: %s", step.intent)
                return
            loc.click(timeout=t)
        elif a == "expect":
            if step.url and not re.search(step.url, page.url):
                raise UnexpectedPage(f"expected a page matching {step.url}, got {url_pattern(page.url)}")
            if step.locator:
                step.locator.resolve(page).wait_for(state="visible", timeout=t)
        elif a == "mfa":
            try:
                step.detect.resolve(page).wait_for(state="visible", timeout=step.timeout_ms or self.config.mfa_detect_ms)
            except PlaywrightTimeout:
                log.info("no verification prompt; device still remembered")
                return
            log.info("verification prompt shown; waiting for the code form")
            code = self.codes.wait_for_code(self.config.mfa_timeout_s)
            self._used_secrets.append(code)
            step.input.resolve(page).fill(code, timeout=t)
            step.submit.resolve(page).click(timeout=t)
        elif a == "download":
            with page.expect_download(timeout=max(t, 60_000)) as info:
                step.locator.resolve(page).click(timeout=t)
            result.downloads.append(self._save_download(info.value, step.save_as))
        elif a == "balance":
            text = step.locator.resolve(page).inner_text(timeout=t).strip()
            if not re.search(r"\d", text):
                raise UnexpectedPage(f"the balance element holds no number ({len(text)} chars)")
            body = json.dumps({"kind": "balance", "date": dt.date.today().isoformat(), "text": text}, ensure_ascii=False)
            result.downloads.append(self._save_bytes(body.encode(), step.save_as, "json"))
        else:  # pragma: no cover - Step.parse rejects unknown actions
            raise ValueError(a)

        if self._blocked_navigation:
            raise UnexpectedPage(f"navigation to {self._blocked_navigation} was aborted")
        if self._extra_pages:
            raise UnexpectedPage("the site opened a new window")

    def _save_download(self, download, template: str) -> Path:
        suggested = download.suggested_filename or "download"
        ext = Path(suggested).suffix.lstrip(".").lower() or "bin"
        target = self._raw_target(template, ext)
        download.save_as(target)
        return self._seal(target)

    def _save_bytes(self, data: bytes, template: str, ext: str) -> Path:
        target = self._raw_target(template, ext)
        target.write_bytes(data)
        return self._seal(target)

    @staticmethod
    def _seal(target: Path) -> Path:
        os.chmod(target, 0o444)
        log.info("saved %s", target.name)
        return target

    def _raw_target(self, template: str, ext: str) -> Path:
        today = dt.date.today()
        name = template.format(today=today.isoformat(), ext=ext)
        if Path(name).name != name or name.startswith("."):
            raise ValueError(f"save_as must be a plain file name, got {name!r}")
        folder = self.config.raw_root / self.script.institution / str(today.year)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        n = 1
        while target.exists():  # raw files are immutable; never overwrite
            target = folder / f"{Path(name).stem}-{n}{Path(name).suffix}"
            n += 1
        return target

    # -- failure bundle ------------------------------------------------------

    def _redact(self, text: str) -> str:
        for value in sorted(self._used_secrets, key=len, reverse=True):
            if value:
                text = text.replace(value, "‹secret›")
        return text

    def _bundle(self, page, phase: str, index: int, step: Step, error: Exception) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        folder = self.config.bundle_root / f"{self.script.institution}-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        snapshot = ""
        page_pattern = ""
        if page is not None:
            try:
                page_pattern = url_pattern(page.url)
                snapshot = scrub_snapshot(page.locator("body").aria_snapshot(timeout=5_000))
            except PlaywrightError as exc:
                snapshot = f"(snapshot unavailable: {type(exc).__name__})"
            if phase != "login" and self.config.failure_screenshots:
                try:
                    # Local only, for the human. Never read by the repair command.
                    page.screenshot(path=str(folder / "screenshot.local.png"))
                except PlaywrightError:
                    pass
        bundle = {
            "institution": self.script.institution,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "phase": phase,
            "step_index": index,
            "step": step.to_dict(),
            "error_type": type(error).__name__,
            "error": self._redact(str(error))[:2000],
            "page": page_pattern,
            "snapshot": snapshot,
            "script_path": str(self.script.path) if self.script.path else None,
            "script": self.script.to_dict(),
        }
        (folder / "bundle.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        return folder

    # -- entry point ---------------------------------------------------------

    def run(self) -> RunResult:
        result = RunResult(institution=self.script.institution, ok=False)
        cfg = self.config
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(cfg.profile_dir),
                channel=cfg.channel,
                headless=cfg.headless,
                chromium_sandbox=cfg.chromium_sandbox,
                # Look like the person's own Chrome, not a test harness: Playwright's
                # --enable-automation sets navigator.webdriver and shows a banner.
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
                no_viewport=not cfg.headless,  # real window size under Xvfb, not an emulated viewport
                accept_downloads=True,
                locale=cfg.locale,
                timezone_id=cfg.timezone_id,
            )
            page = None
            try:
                context.route("**/*", lambda route: self._route(route, result))
                page = context.pages[0] if context.pages else context.new_page()
                context.on("page", self._on_new_page)
                for phase, steps in (("login", self.script.login), ("steps", self.script.steps)):
                    for i, step in enumerate(steps):
                        log.info("[%s %d] %s", phase, i, step.intent)
                        try:
                            self._run_step(page, step, result)
                        except Exception as exc:  # noqa: BLE001 - every failure stops the run
                            result.error = f"{phase} step {i} ({step.intent}): {type(exc).__name__}"
                            result.failure_bundle = self._bundle(page, phase, i, step, exc)
                            log.error("stopped: %s; bundle at %s", result.error, result.failure_bundle)
                            self.notifier.notify(f"Finance sync stopped: {self.script.institution} needs attention.")
                            return result
                result.ok = True
                return result
            finally:
                self._used_secrets.clear()
                context.close()

    def _on_new_page(self, new_page) -> None:
        self._extra_pages += 1
        new_page.close()

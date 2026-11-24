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
import random
import re
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .notify import CodeProvider, MfaNeeded, Notifier
from .scrub import mask_long_numbers, scrub_snapshot, url_pattern
from .secrets import SecretStore
from .steps import ACCOUNT, Script, Step, account_ref

log = logging.getLogger("ingest.runner")


class UnexpectedPage(RuntimeError):
    pass


@dataclass
class RunConfig:
    raw_root: Path
    profile_dir: Path
    bundle_root: Path
    headless: bool = False
    # Start Chrome ourselves with the same plain command line as attend.sh's
    # sign-in window, then connect Playwright to it, instead of letting
    # Playwright launch it with its ~20 extra flags (extensions off, background
    # networking off, ...) that a person's Chrome never has. Off = Playwright
    # launches `channel` itself (the tests, which use the bundled Chromium).
    attach: bool = True
    chrome_binary: str = "google-chrome"
    channel: str | None = "chrome"
    # Chrome's own process sandbox (renderer isolation). A security control, not
    # a stealth one: with it off, a hostile page that exploits the renderer can
    # read everything this UID can, including the decrypted bank logins.
    chromium_sandbox: bool = True
    # From the deployment's .env (INGEST_LOCALE, INGEST_TIMEZONE). None = the
    # system's, which in a container is UTC: set them to match where you are.
    locale: str | None = None
    timezone_id: str | None = None
    step_timeout_ms: int = 20_000
    full_history: bool = False  # run "history": "full" steps instead of "recent" ones
    dismiss_wait_ms: int = 4_000
    mfa_detect_ms: int = 8_000
    mfa_timeout_s: float = 300
    failure_screenshots: bool = True
    # Act at a person's pace: type key by key with uneven gaps, move the mouse
    # to a button before clicking it, and pause between steps. Anti-bot checks
    # score instant form fills. Tests turn it off for speed.
    human_pacing: bool = True


@dataclass
class RunResult:
    institution: str
    ok: bool
    downloads: list[Path] = field(default_factory=list)
    aborted_requests: list[tuple[str, str]] = field(default_factory=list)
    failure_bundle: Path | None = None
    error: str | None = None


# Developer labels on the page (data-test-id / data-testid) and whether each is
# visible: code names, not personal data, and often what a broken locator needs.
# What a site's bot check sees first. No page content, no personal data.
BROWSER_FACTS = """() => ({
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, language: navigator.language,
  screen: screen.width + 'x' + screen.height, window: outerWidth + 'x' + outerHeight,
  webdriver: navigator.webdriver,
})"""


TEST_ID_INVENTORY = """() => {
  // Plain loops and objects only: a site's own scripts can replace Set,
  // iterators and spread, and the inventory must still work on those pages.
  const els = document.querySelectorAll('[data-test-id],[data-testid]');
  const seen = Object.create(null), out = [];
  for (let i = 0; i < els.length && out.length < 400; i++) {
    const id = els[i].getAttribute('data-test-id') || els[i].getAttribute('data-testid');
    // Developer labels only: no spaces; long digit runs (dates, ids) masked.
    if (!/^[A-Za-z0-9][A-Za-z0-9_.\/-]{0,127}$/.test(id)) continue;
    const r = els[i].getBoundingClientRect();
    const entry = id.replace(/[0-9]{4,}/g, '#') + (r.width && r.height ? '' : ' (hidden)');
    if (!seen[entry]) { seen[entry] = 1; out.push(entry); }
  }
  return out;
}"""


def chrome_command(cfg: RunConfig) -> list[str]:
    """The sign-in window's command line (deploy/ingest/scraper-attend.sh), so
    the bank sees the same browser in both. Keep the two in step. Only
    --remote-debugging-port is added (port 0: Chrome picks a free random port
    on 127.0.0.1). Chrome 158 sets navigator.webdriver for any DevTools port,
    and Emulation.setAutomationOverride can't clear it; turning off the
    AutomationControlled feature does (tested: pages, iframes, new tabs). The
    sign-in window passes that flag too, so both command lines stay the same."""
    cmd = [
        cfg.chrome_binary,
        f"--user-data-dir={cfg.profile_dir}",
        f"--lang={cfg.locale or 'en-US'}",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-position=0,0",
        "--window-size=1920,1080",
        "--disable-blink-features=AutomationControlled",
        "--remote-debugging-port=0",
    ]
    if cfg.headless:
        cmd.append("--headless=new")
    if not cfg.chromium_sandbox:
        cmd.append("--no-sandbox")
    return cmd + ["about:blank"]


def chrome_env(cfg: RunConfig) -> dict[str, str]:
    """Timezone and language come from the process environment, as in the
    sign-in window: an attached browser can't be given Playwright's overrides."""
    env = dict(os.environ)
    if cfg.timezone_id:
        env["TZ"] = cfg.timezone_id
    if cfg.locale:
        env["LANG"] = cfg.locale.replace("-", "_") + ".UTF-8"
    return env


def start_chrome(cfg: RunConfig, timeout_s: float = 30) -> tuple[subprocess.Popen, str]:
    """Start plain Chrome on the profile; return it and its DevTools address."""
    port_file = cfg.profile_dir / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        chrome_command(cfg), env=chrome_env(cfg),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome exited at start (status {proc.returncode})")
        try:
            port = port_file.read_text().split("\n", 1)[0].strip()
        except FileNotFoundError:
            port = ""
        if port.isdigit():
            return proc, f"http://127.0.0.1:{port}"
        time.sleep(0.2)
    stop_chrome(proc, None)
    raise RuntimeError("Chrome did not open its DevTools port in time")


def stop_chrome(proc: subprocess.Popen, browser) -> None:
    """Close Chrome the way its own X button does, so the profile (cookies,
    remembered device) is written out; kill it only if it hangs."""
    if browser is not None:
        try:
            browser.new_browser_cdp_session().send("Browser.close")
        except PlaywrightError:
            pass
    if proc.poll() is None and browser is None:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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
        self._accounts: list[tuple[str, str]] | None = None  # (label, account_ref), for_each_account only
        self._account = 0

    # -- request filtering -------------------------------------------------

    def _route(self, route, result: RunResult) -> None:
        req = route.request
        verdict = self.script.allowlist.decide(req.url, req.method, req.is_navigation_request())
        if verdict == "allow":
            route.continue_()
            return
        pattern = url_pattern(req.url)
        result.aborted_requests.append((pattern, verdict))
        # Host plus path shape: enough to extend an allowlist, no ids or query.
        log.info("aborted %s %s%s (%s)", req.method, urlsplit(req.url).hostname or "", pattern, verdict)
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
        if step.history and step.history != ("full" if self.config.full_history else "recent"):
            log.info("skipped: only on %s-history runs", step.history)
            return
        t = self._timeout(step)
        a = step.action
        if a == "goto":
            page.goto(step.url, timeout=t)
        elif a == "fill":
            self._type(page, step.locator.resolve(page), self._secret(step.secret), t)
        elif a == "click":
            self._click(page, step.locator.resolve(page), t)
        elif a == "wait":
            step.locator.resolve(page).wait_for(state="visible", timeout=t)
        elif a == "scroll":
            self._scroll(page, step.locator.resolve(page) if step.locator else None, t)
        elif a == "dismiss":
            loc = step.locator.resolve(page)
            try:
                loc.wait_for(state="visible", timeout=step.timeout_ms or self.config.dismiss_wait_ms)
            except PlaywrightTimeout:
                log.info("nothing to dismiss: %s", step.intent)
                return
            if step.key:
                page.keyboard.press(step.key)
            else:
                self._click(page, loc, t)
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
            if step.request is not None:
                # The bank sends the code only when asked. Nobody to answer (an
                # unattended run): stop before asking, so no code is sent at 3 a.m.
                if not getattr(self.codes, "attended", False):
                    self.codes.wait_for_code(0)  # notifies and raises MfaNeeded
                    raise MfaNeeded("verification code needed; nobody to answer")
                ask = step.request.resolve(page)
                if ask.count():  # some banks (or some visits) send the code without being asked
                    self._click(page, ask.first, t)
            log.info("verification prompt shown; waiting for the code")
            code = self.codes.wait_for_code(self.config.mfa_timeout_s)
            self._used_secrets.append(code)
            self._type(page, step.input.resolve(page), code, t)
            if step.remember is not None:
                box = step.remember.resolve(page)
                if box.count() and not box.is_checked():
                    box.check(timeout=t)
            self._click(page, step.submit.resolve(page), t)
        elif a == "select" and step.option == ACCOUNT:
            field = step.locator.resolve(page)
            if self._accounts is None:
                self._accounts = self._read_accounts(field, t)
            label, ref = self._accounts[self._account]
            log.info("account %d of %d: %s", self._account + 1, len(self._accounts), ref)
            labels = [x.strip() for x in field.locator("option").all_inner_texts()]
            if labels.count(label) != 1:
                raise UnexpectedPage(f"account {self._account + 1} is no longer in the dropdown exactly once")
            field.select_option(index=labels.index(label), timeout=t)
        elif a == "select":
            field = step.locator.resolve(page)
            if step.option is not None:
                wanted, name = step.option, repr(step.option)
            else:
                wanted, name = self.secrets.get(step.option_secret), step.option_secret
                self._used_secrets.append(wanted)
            labels = [label.strip() for label in field.locator("option").all_inner_texts()]
            exact = [i for i, label in enumerate(labels) if label == wanted]
            matches = exact or [i for i, label in enumerate(labels) if wanted in label]
            if len(matches) != 1:
                raise UnexpectedPage(f"{len(matches)} of {len(labels)} options match {name}")
            field.select_option(index=matches[0], timeout=t)
        elif a == "download" and step.empty is None:
            with page.expect_download(timeout=max(t, 60_000)) as info:
                self._click(page, step.locator.resolve(page), t)
            result.downloads.append(self._save_download(info.value, step.save_as))
        elif a == "download":
            self._download_or_empty(page, step, result, t)
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

    def _scroll(self, page, target, t: int) -> None:
        """Scroll a screen at a time: lazy panels load as they come into view."""
        waited = 0
        while True:
            if target is not None and target.count() and target.first.is_visible():
                target.first.scroll_into_view_if_needed(timeout=t)
                return
            at_bottom = page.evaluate(
                "() => { window.scrollBy(0, Math.round(window.innerHeight * 0.8));"
                " return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2 }"
            )
            page.wait_for_timeout(400)
            waited += 400
            if at_bottom and target is None:
                return
            if waited >= t:
                raise UnexpectedPage("scrolled to the end of the page without finding the target")

    def _read_accounts(self, field, t: int) -> list[tuple[str, str]]:
        field.wait_for(state="visible", timeout=t)
        labels = field.evaluate(
            "el => [...el.options].filter(o => !o.disabled && o.value !== '').map(o => o.textContent.trim())"
        )
        accounts = [(label, account_ref(label)) for label in labels]
        if not accounts or any(not ref for _, ref in accounts):
            raise UnexpectedPage(f"the account dropdown lists {len(accounts)} usable accounts")
        refs = [ref for _, ref in accounts]
        if len(set(refs)) != len(refs):
            raise UnexpectedPage("two accounts in the dropdown have the same name")
        self._used_secrets.extend(labels)  # keep account labels out of failure bundles
        log.info("accounts found: %s", ", ".join(refs))
        return accounts

    def _download_or_empty(self, page, step: Step, result: RunResult, t: int) -> None:
        """Click, then wait for whichever comes first: a file, or the bank's
        "nothing to download" message. A message already showing before the
        click (left over from an earlier attempt) only counts once it has had
        time to be replaced."""
        empty = step.empty.resolve(page)
        stale = empty.first.is_visible()
        got: list = []
        page.once("download", lambda download: got.append(download))
        self._click(page, step.locator.resolve(page), t)
        waited, limit = 0, max(t, 60_000)
        while waited < limit:
            if got:
                result.downloads.append(self._save_download(got[0], step.save_as))
                return
            if empty.first.is_visible() and (not stale or waited >= 5_000):
                log.info("nothing to download (%s)", step.intent)
                return
            page.wait_for_timeout(250)
            waited += 250
        raise PlaywrightTimeout(f"no download and no 'nothing to download' message within {limit // 1000}s")

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
        ref = self._accounts[self._account][1] if self._accounts else None
        name = template.format(today=today.isoformat(), ext=ext, **({"account": ref} if ref else {}))
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
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder = self.config.bundle_root / f"{self.script.institution}-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        snapshot = ""
        page_pattern = page_host = ""
        test_ids: list = []
        if page is not None:
            page_pattern = url_pattern(page.url)
            page_host = urlsplit(page.url).hostname or ""
            # Independent: a page whose text snapshot fails can still list its test ids.
            try:
                snapshot = scrub_snapshot(page.locator("body").aria_snapshot(timeout=5_000))
            except PlaywrightError as exc:
                snapshot = f"(snapshot unavailable: {type(exc).__name__}: {mask_long_numbers(str(exc).splitlines()[0][:120])})"
            try:
                test_ids = self._test_ids(page)
            except PlaywrightError as exc:
                test_ids = [f"(unavailable: {type(exc).__name__}: {mask_long_numbers(str(exc).splitlines()[0][:160])})"]
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
            "host": page_host,
            "snapshot": snapshot,
            "test_ids": test_ids,
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
        # Every run is a fresh container, so a lock left in the profile is from a
        # run that was killed; Chrome would otherwise refuse to start.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (cfg.profile_dir / name).unlink(missing_ok=True)
        with sync_playwright() as pw, ExitStack() as cleanup:
            context = self._open_browser(pw, cleanup)
            page = None
            try:
                context.route("**/*", lambda route: self._route(route, result))
                page = context.pages[0] if context.pages else context.new_page()
                log.info("browser: %s", page.evaluate(BROWSER_FACTS))
                page.on("framenavigated", lambda f: log.info("%s %s%s", "page" if f == page.main_frame else "frame", urlsplit(f.url).hostname or "", url_pattern(f.url)) if f.url.startswith("http") else None)
                page.on("response", lambda r: r.request.method == "POST" and log.info("POST %s %s%s", r.status, urlsplit(r.url).hostname or "", url_pattern(r.url)))
                context.on("page", self._on_new_page)
                if not self._run_phase(page, "login", self.script.login, result):
                    return result
                self._account = 0
                while True:
                    if not self._run_phase(page, "steps", self.script.steps, result):
                        return result
                    if self.script.for_each_account is None or self._account + 1 >= len(self._accounts or ()):
                        break
                    self._account += 1
                result.ok = True
                return result
            finally:
                self._used_secrets.clear()

    def _open_browser(self, pw, cleanup: ExitStack):
        """The browser context for this run; `cleanup` closes it at the end."""
        cfg = self.config
        if not cfg.attach:
            context = pw.chromium.launch_persistent_context(
                str(cfg.profile_dir),
                channel=cfg.channel,
                headless=cfg.headless,
                chromium_sandbox=cfg.chromium_sandbox,
                # Playwright's --enable-automation sets navigator.webdriver and shows a banner.
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"]
                + ([] if cfg.headless else ["--window-position=0,0", "--window-size=1920,1080"]),
                no_viewport=not cfg.headless,  # real window size under Xvfb, not an emulated viewport
                accept_downloads=True,
                locale=cfg.locale,
                timezone_id=cfg.timezone_id,
            )
            cleanup.callback(context.close)
            return context
        proc, endpoint = start_chrome(cfg)
        holder: list = [None]
        cleanup.callback(lambda: stop_chrome(proc, holder[0]))
        holder[0] = browser = pw.chromium.connect_over_cdp(endpoint)
        log.info("browser: plain %s, attached", cfg.chrome_binary)
        # Chrome's own default context: the profile on disk, its real window size.
        return browser.contexts[0]

    def _test_ids(self, page) -> list[str]:
        """Developer labels on the page, read through element queries rather
        than a script in the page: some sites disable eval, which page.evaluate
        needs. Labels only (no spaces); long digit runs masked."""
        try:
            return page.evaluate(TEST_ID_INVENTORY)
        except PlaywrightError as exc:
            if "eval" not in str(exc):
                raise
        out: list[str] = []
        for el in page.locator("[data-test-id],[data-testid]").all()[:400]:
            ident = el.get_attribute("data-test-id") or el.get_attribute("data-testid") or ""
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}", ident):
                continue
            entry = re.sub(r"\d{4,}", "#", ident) + ("" if el.is_visible() else " (hidden)")
            if entry not in out:
                out.append(entry)
        return out

    def _pause(self, page, low_ms: int, high_ms: int) -> None:
        if self.config.human_pacing:
            page.wait_for_timeout(random.randint(low_ms, high_ms))

    def _click(self, page, loc, t: int) -> None:
        """Move the mouse to a random point inside the element, then click."""
        if self.config.human_pacing:
            loc.wait_for(state="visible", timeout=t)
            box = loc.bounding_box()
            if box:
                x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                page.mouse.move(x, y, steps=random.randint(8, 20))
                self._pause(page, 80, 250)
        loc.click(timeout=t)

    def _type(self, page, loc, value: str, t: int) -> None:
        """Click into the field and type key by key with uneven gaps."""
        if not self.config.human_pacing:
            loc.fill(value, timeout=t)
            return
        self._click(page, loc, t)
        loc.fill("", timeout=t)
        for ch in value:
            page.keyboard.type(ch)
            page.wait_for_timeout(random.randint(60, 190) if random.random() > 0.08 else random.randint(250, 500))

    def _run_phase(self, page, phase: str, steps, result: RunResult) -> bool:
        for i, step in enumerate(steps):
            if i:
                self._pause(page, 500, 1600)
            log.info("[%s %d] %s", phase, i, step.intent)
            try:
                self._run_step(page, step, result)
            except Exception as exc:  # noqa: BLE001 - every failure stops the run
                result.error = f"{phase} step {i} ({step.intent}): {type(exc).__name__}"
                result.failure_bundle = self._bundle(page, phase, i, step, exc)
                log.error("stopped: %s; bundle at %s", result.error, result.failure_bundle)
                self.notifier.notify(f"Finance sync stopped: {self.script.institution} needs attention.")
                return False
        return True

    def _on_new_page(self, new_page) -> None:
        self._extra_pages += 1
        new_page.close()

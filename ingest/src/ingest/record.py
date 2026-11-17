"""Record a bank routine in a browser set up like the nightly scraper.

`playwright codegen` launches Chromium in automation mode (navigator.webdriver,
the "controlled by automated software" switch) and pins the page to 1280x720.
Bank sign-on pages often ignore clicks from such a browser, and a tiled or
resized window shows a cut-off page. This launches the same way the scraper
does (automation flags off, page follows the window, fresh throwaway profile in
RAM) and attaches Playwright's recorder, which writes a codegen-style Python
script to the output file as you go. Close the browser window to finish.

The recorder hook is Playwright's internal one (what codegen itself uses); if a
Playwright upgrade removes it, this fails loudly rather than recording nothing.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def record(url: str, output: Path, channel: str | None = None) -> int:
    from playwright.sync_api import sync_playwright

    ram = "/dev/shm" if os.path.isdir("/dev/shm") else None
    with tempfile.TemporaryDirectory(prefix="ingest-record-", dir=ram) as profile, sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            profile,
            channel=channel,
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
            no_viewport=True,
            accept_downloads=True,
        )
        show = getattr(context._impl_obj, "_show_recorder", None)
        if show is None:
            context.close()
            raise RuntimeError("this Playwright version has no recorder hook; use `playwright codegen` instead")
        context._sync(show({"language": "python", "mode": "recording", "outputFile": str(output), "handleSIGINT": False}))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        print(f"recording to {output}. Close the browser window when you're done.")
        context.wait_for_event("close", timeout=0)
    return 0

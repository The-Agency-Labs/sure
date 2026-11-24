# syntax=docker/dockerfile:1
# Two images from one build: `--target loader` and `--target scraper`.

FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

FROM base AS loader
USER 2002:2000
ENTRYPOINT ["python", "-m", "ingest"]

FROM base AS scraper
# Real Google Chrome (not the bundled Chromium) in headed mode under Xvfb.
# (curl is only for Playwright's Chrome installer.) x11vnc + noVNC are for
# attend.sh: the owner signs in live in the bank's profile, seen in a browser.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 xvfb xauth ca-certificates curl \
    x11vnc novnc websockify \
 && playwright install --with-deps chrome \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
# Patchright drives Chrome without Playwright's page-level tells (Runtime.enable).
# Same deps as Playwright (pyee, greenlet), already installed: --no-deps keeps
# the hash check to the one pinned file.
COPY deploy/ingest/patchright.txt /tmp/patchright.txt
RUN pip install --no-deps --require-hashes -r /tmp/patchright.txt && rm /tmp/patchright.txt
ENV INGEST_BROWSER_DRIVER=patchright
COPY deploy/ingest/scraper-entrypoint.sh /usr/local/bin/scraper-entrypoint
COPY deploy/ingest/scraper-attend.sh /usr/local/bin/scraper-attend
USER 2001:2000
ENV HOME=/tmp/home
ENTRYPOINT ["/usr/local/bin/scraper-entrypoint"]

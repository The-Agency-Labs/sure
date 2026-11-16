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
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 xvfb xauth unzip ca-certificates curl \
 && playwright install --with-deps chrome \
 && rm -rf /var/lib/apt/lists/*
# Bitwarden CLI, native build. Pin the version and its SHA-256 from
# https://github.com/bitwarden/clients/releases (cli-v<version>); the build
# fails if either is missing.
ARG BW_VERSION
ARG BW_SHA256
RUN test -n "$BW_VERSION" && test -n "$BW_SHA256" \
 && curl -fsSL -o /tmp/bw.zip "https://github.com/bitwarden/clients/releases/download/cli-v${BW_VERSION}/bw-linux-${BW_VERSION}.zip" \
 && echo "${BW_SHA256}  /tmp/bw.zip" | sha256sum -c - \
 && unzip -q /tmp/bw.zip -d /usr/local/bin && chmod 755 /usr/local/bin/bw && rm /tmp/bw.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y
COPY deploy/ingest/scraper-entrypoint.sh /usr/local/bin/scraper-entrypoint
USER 2001:2000
ENV HOME=/tmp/home
ENTRYPOINT ["/usr/local/bin/scraper-entrypoint"]

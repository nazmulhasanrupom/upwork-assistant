FROM python:3.12-slim

# Set by BuildKit. Empty when built without it (classic builder), then dpkg decides.
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51
ARG SUPERCRONIC_SHA1_AMD64=1692066e350a88ff4d7d6474fe3c7a6c41ec5e89
ARG SUPERCRONIC_SHA1_ARM64=9d4c2000493215a1898502b2fc2dada3e8ea8c7a
ARG SUPERCRONIC_SHA1_ARM=9224966499cd319ed85759e9b6442f885d920c4f

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata git \
 && rm -rf /var/lib/apt/lists/*

# supercronic: static cron runner, picked by CPU architecture, checksum verified
RUN set -eu; \
    arch="${TARGETARCH:-}"; \
    if [ -z "${arch}" ]; then arch="$(dpkg --print-architecture)"; fi; \
    case "${arch}" in \
      amd64|x86_64) arch=amd64; SHA="${SUPERCRONIC_SHA1_AMD64}" ;; \
      arm64|aarch64) arch=arm64; SHA="${SUPERCRONIC_SHA1_ARM64}" ;; \
      arm|armhf|armv7) arch=arm; SHA="${SUPERCRONIC_SHA1_ARM}" ;; \
      *) echo "unsupported architecture ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${SHA}  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod +x /usr/local/bin/supercronic

# non-root user with a home dir and the data volume
RUN useradd --create-home --home-dir /home/app --shell /bin/bash app \
 && mkdir -p /data /app \
 && chown app:app /data /app

USER app
ENV HOME=/home/app \
    PATH="/home/app/.local/bin:${PATH}" \
    DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/data/claude \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Claude Code, installed for the app user
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /app
COPY --chown=app:app requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt -r requirements-dev.txt
COPY --chown=app:app . .
RUN chmod +x /app/docker/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

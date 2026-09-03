# One container, no separate tor service: the tool fetches and runs its own Tor.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[tor]"

RUN useradd --create-home --uid 10001 itd \
    && mkdir -p /data /config && chown itd /data /config
USER itd

ENV ITD_DATA_DIR=/data \
    ITD_TARGETS_FILE=/config/targets.yaml \
    ITD_RULES_FILE=/config/rules.yaml \
    ITD_SECTORS_FILE=/config/sectors.yaml \
    ITD_ENGINES_FILE=/config/engines.yaml \
    ITD_BOOKMARKS_FILE=/config/bookmarks.json

COPY --chown=itd deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["itd", "--help"]

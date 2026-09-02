FROM python:3.12-slim

# curl backs the compose healthcheck and ad-hoc SOCKS checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[tor]"

# Run unprivileged; /data is a named volume owned by this user.
RUN useradd --create-home --uid 10001 itd && mkdir -p /data && chown itd /data
USER itd
ENV ITD_DATA_DIR=/data

CMD ["itd", "--help"]

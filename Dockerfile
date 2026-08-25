# Two stages so the wheels are built with a compiler that the final image does
# not have to carry. argon2-cffi and asyncpg both build native code on
# platforms that have no wheel published, which arm64 regularly does not.

FROM python:3.13-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt


FROM python:3.13-slim

# curl is here for the health check and nothing else. It is a few hundred
# kilobytes and it means the container reports unhealthy by itself rather than
# waiting for someone to notice.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 pitwatch

COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PITWATCH_PORT=8080

WORKDIR /app
COPY --chown=pitwatch:pitwatch pitwatch ./pitwatch
COPY --chown=pitwatch:pitwatch pyproject.toml README.md LICENSE ./

USER pitwatch
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PITWATCH_PORT}/healthz" || exit 1

# tini reaps the websocket and Modbus tasks properly on stop, so a restart does
# not leave a socket held open for a minute.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pitwatch"]

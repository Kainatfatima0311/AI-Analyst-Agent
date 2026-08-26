# Multi-stage, non-root. One image serves both the API and the UI — they share every dependency
# and differ only in the command, so building two would mean maintaining two.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependency metadata is copied on its own first, so a source-only change does not invalidate
# the layer that installs the dependencies.
COPY pyproject.toml README.md ./
COPY src/analyst_agent/__init__.py src/analyst_agent/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

COPY src/ src/
RUN /opt/venv/bin/pip install --no-deps .


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is here for the container healthchecks and nothing else.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# A dedicated unprivileged user. The service needs to read its own code and talk to Postgres;
# it has no reason to be able to write anywhere in the image.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 analyst

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=analyst:analyst src/ src/
COPY --chown=analyst:analyst db/ db/
COPY --chown=analyst:analyst evals/ evals/
COPY --chown=analyst:analyst scripts/ scripts/

USER analyst
EXPOSE 8000 8501

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "analyst_agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

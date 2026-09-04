# Multi-stage: uv and the build tooling stay in the first stage, and the image
# that runs in the cluster ships a virtual environment, a slim Python and
# nothing else. It runs as an unprivileged user, because a process that can
# rewrite its own filesystem turns one bug into a foothold.
FROM python:3.12-slim-bookworm AS builder

# The pinned uv binary on the pinned Python, rather than a combined image whose
# tag would pin one of the two by accident.
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The manifests first: the layer with the third-party dependencies is then
# reused on every build where only the source of a service changed.
COPY pyproject.toml uv.lock ./
COPY libs/common/pyproject.toml libs/common/
COPY services/api-gateway/pyproject.toml services/api-gateway/
COPY services/auth/pyproject.toml services/auth/
COPY services/catalog/pyproject.toml services/catalog/
COPY services/booking/pyproject.toml services/booking/
COPY services/notification/pyproject.toml services/notification/
RUN uv sync --frozen --no-dev --no-install-workspace --package barber-gateway

COPY libs/common libs/common
COPY services/api-gateway services/api-gateway
RUN uv sync --frozen --no-dev --package barber-gateway

FROM python:3.12-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 barber

WORKDIR /app
COPY --from=builder --chown=barber:barber /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER barber
EXPOSE 8000

# The factory form: settings are read when the application is built rather than
# when the module is imported, so a missing variable fails the start with the
# message of the chassis instead of an import error.
CMD ["uvicorn", "barber_gateway.main:create_application", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel --wheel-dir /wheels .


FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/hraedon/touchstone"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 65532 touchstone \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin touchstone

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels touchstone \
    && rm -rf /wheels

WORKDIR /app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

USER 65532:65532
EXPOSE 8080
STOPSIGNAL SIGTERM

CMD ["uvicorn", "touchstone.sink.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

# The app image. It listens on 0.0.0.0:8000. Mount the data directory and set PROJECT_DATA_DIR to it.
FROM python:{{python}}-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /workspace

# The dependency layer sees only pyproject.toml and uv.lock, so an app
# change keeps the venv layer cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-dev

COPY . /workspace

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

FROM python:{{python}}-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    STARIO_HOST=0.0.0.0 \
    STARIO_PORT=8000

WORKDIR /workspace

COPY --from=builder /workspace /workspace

ENV PATH="/workspace/.venv/bin:$PATH"

CMD ["python", "-m", "app.main"]

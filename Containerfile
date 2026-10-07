FROM debian:trixie-slim AS tools

RUN apt-get update && \
    apt-get install -y --no-install-recommends binutils git xz-utils && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /out/usr/bin /out/usr/lib/git-core /out/usr/share/git-core /out/lib/x86_64-linux-gnu && \
    cp -a /usr/lib/git-core/git /out/usr/lib/git-core/git && \
    ln -s ../lib/git-core/git /out/usr/bin/git && \
    cp -a /usr/lib/git-core/git-http-backend /out/usr/lib/git-core/git-http-backend && \
    ln -s git /out/usr/lib/git-core/git-upload-pack && \
    ln -s git /out/usr/lib/git-core/git-receive-pack && \
    cp -a /usr/share/git-core/templates /out/usr/share/git-core/templates && \
    cp -a "$(readlink -f /lib/x86_64-linux-gnu/libpcre2-8.so.0)" /out/lib/x86_64-linux-gnu/libpcre2-8.so.0

ADD https://github.com/stunnel/static-curl/releases/download/8.22.0/curl-linux-x86_64-glibc-8.22.0.tar.xz /tmp/curl.tar.xz
ADD https://github.com/containers/podman/releases/download/v4.9.3/podman-remote-static-linux_amd64.tar.gz /tmp/podman-remote.tar.gz

RUN echo "d74460e43c0e6eaf40ec1fdda92c53dee94da8a6e6db066711541ada5aab0fa6  /tmp/curl.tar.xz" | sha256sum -c && \
    tar -xJf /tmp/curl.tar.xz -C /out/usr/bin && \
    chmod 755 /out/usr/bin/curl && \
    tar -xzf /tmp/podman-remote.tar.gz -C /tmp && \
    strip --strip-debug /tmp/bin/podman-remote-static-linux_amd64 && \
    install -D -m 755 /tmp/bin/podman-remote-static-linux_amd64 /out/usr/local/bin/podman

FROM python:3.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /workspace

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

COPY . /workspace

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.14-slim-trixie

COPY --from=tools /out/usr /usr
COPY --from=tools /out/lib/x86_64-linux-gnu/libpcre2-8.so.0 /lib/x86_64-linux-gnu/libpcre2-8.so.0
COPY --from=builder /workspace/.venv /workspace/.venv

ENV PYTHONUNBUFFERED=1 \
    CONTAINER_HOST=unix:///run/podman/podman.sock \
    GIT_EXEC_PATH=/usr/lib/git-core \
    STARIO_HOST=0.0.0.0 \
    STARIO_PORT=8000 \
    STARIO_LOOP=uvloop

WORKDIR /workspace

EXPOSE 8000
CMD ["/workspace/.venv/bin/python", "-m", "app.main"]

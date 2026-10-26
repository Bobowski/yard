FROM {{fetch}} AS fetch

ARG TARGETARCH

RUN apk add --no-cache ca-certificates wget \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "$arch" in \
         amd64|x86_64) arch=amd64; sum={{sha_amd64}} ;; \
         arm64|aarch64) arch=arm64; sum={{sha_arm64}} ;; \
         *) echo "http-nu has no build for $arch" >&2; exit 1 ;; \
       esac \
    && wget -q -O /tmp/http-nu.tgz "https://github.com/cablehead/http-nu/releases/download/v{{version}}/http-nu-v{{version}}-linux-${arch}.tar.gz" \
    && echo "$sum  /tmp/http-nu.tgz" | sha256sum -c \
    && tar -xzf /tmp/http-nu.tgz -C /tmp \
    && install -m 755 /tmp/http-nu-v{{version}}/http-nu /http-nu

FROM {{static}}

ENV PORT=8000

WORKDIR /app

COPY --from=fetch /http-nu /usr/local/bin/http-nu

COPY . /app

CMD ["http-nu", "--datastar", "--store", "/data/store", "0.0.0.0:8000", "./serve.nu"]

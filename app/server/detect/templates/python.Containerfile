FROM {{python}} AS builder

{{builder}}

WORKDIR /workspace

{{deps}}

COPY . /workspace

{{project}}

FROM {{python}}

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /workspace

COPY --from=builder /workspace /workspace

{{env}}

CMD {{cmd}}

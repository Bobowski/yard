FROM {{golang}} AS builder

WORKDIR /workspace

COPY go.* ./
RUN go mod download

COPY . /workspace

RUN CGO_ENABLED=0 go build -trimpath -o /out/app {{pkg}}

FROM {{static}}

ENV PORT=8000

COPY --from=builder /out/app /app

ENTRYPOINT ["/app"]

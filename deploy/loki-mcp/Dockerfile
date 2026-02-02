# grafana/loki-mcp は公式Dockerイメージがないため、ソースからビルド
FROM mirror.gcr.io/library/golang:1.24-alpine AS builder

RUN apk add --no-cache git

WORKDIR /build
RUN git clone --depth 1 https://github.com/grafana/loki-mcp.git .
RUN go build -o /loki-mcp ./cmd/server

FROM mirror.gcr.io/library/alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=builder /loki-mcp /usr/local/bin/loki-mcp

EXPOSE 8080

ENTRYPOINT ["loki-mcp"]

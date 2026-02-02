FROM mirror.gcr.io/library/golang:1.24-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates
WORKDIR /build
RUN git clone --depth 1 https://github.com/grafana/mcp-grafana.git .
RUN CGO_ENABLED=0 go build -o /mcp-grafana ./cmd/mcp-grafana

FROM mirror.gcr.io/library/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd -r -u 1000 -m mcp-grafana
USER mcp-grafana
COPY --from=builder --chown=mcp-grafana:mcp-grafana /mcp-grafana /app/mcp-grafana
EXPOSE 8000
ENTRYPOINT ["/app/mcp-grafana"]
CMD ["--transport", "sse", "--address", "0.0.0.0:8000"]

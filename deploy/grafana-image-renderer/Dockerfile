FROM mirror.gcr.io/library/golang:1.24-alpine AS builder
WORKDIR /build
RUN apk add --no-cache git ca-certificates
RUN git clone --depth 1 https://github.com/grafana/grafana-image-renderer.git .
ENV GOTOOLCHAIN=auto
RUN CGO_ENABLED=0 go build -ldflags="-s -w -extldflags '-static'" -o /grafana-image-renderer .

FROM mirror.gcr.io/library/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates tini openssl locales \
      chromium fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg \
      fonts-khmeros fonts-freefont-ttf fonts-liberation \
    && sed -i 's/# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    CHROME_BIN=/usr/bin/chromium \
    GF_RENDERER_PLUGIN_CHROME_BIN=/usr/bin/chromium

RUN groupadd -r renderer && useradd -r -g renderer -u 65532 renderer
USER renderer

COPY --from=builder /grafana-image-renderer /usr/local/bin/grafana-image-renderer

EXPOSE 8081
HEALTHCHECK --interval=10s --timeout=5s CMD ["/usr/local/bin/grafana-image-renderer", "-health"]
ENTRYPOINT ["tini", "--"]
CMD ["grafana-image-renderer", "server", "--enable-metrics", "--port=8081"]

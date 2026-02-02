# Ollama CPU-only build from source (no Docker Hub dependency)
FROM mirror.gcr.io/library/golang:1.24-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 https://github.com/ollama/ollama.git .

# Build CPU-only backend using CMake preset
RUN cmake --preset CPU \
    && cmake --build build --parallel $(nproc) \
    && cmake --install build

# Build Go binary
RUN CGO_ENABLED=1 go build -trimpath -o /ollama .

FROM mirror.gcr.io/library/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -r -m -u 1000 ollama
USER ollama

COPY --from=builder /ollama /usr/local/bin/ollama
COPY --from=builder /build/dist/ /usr/local/

ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434

ENTRYPOINT ["/usr/local/bin/ollama"]
CMD ["serve"]

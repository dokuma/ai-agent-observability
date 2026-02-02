FROM mirror.gcr.io/library/node:24-alpine AS alpine
RUN apk update && apk upgrade --no-cache \
    libcrypto3 libssl3 libc6-compat busybox ssl_client

FROM alpine AS base
RUN npm install -g turbo@2.7.6
RUN corepack enable && corepack prepare pnpm@9.5.0 --activate

FROM base AS pruner
RUN apk add --no-cache git
WORKDIR /app
RUN git clone --depth 1 https://github.com/langfuse/langfuse.git .
RUN turbo prune --scope=worker --docker

FROM base AS builder
WORKDIR /app
COPY --from=pruner /app/out/json/ .
RUN pnpm install --frozen-lockfile
COPY --from=pruner /app/out/full/ .
RUN turbo build --filter=worker

FROM alpine AS runner
RUN apk add --no-cache dumb-init git
RUN addgroup --system --gid 1001 expressjs && \
    adduser --system --uid 1001 expressjs
USER expressjs
WORKDIR /app
COPY --from=builder --chown=expressjs:expressjs /app .

EXPOSE 3030
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "worker/dist/index.js"]

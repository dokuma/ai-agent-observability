# ClickHouse server built from official apt repository (no Docker Hub dependency)
ARG VERSION=24.3.*
FROM public.ecr.aws/docker/library/ubuntu:22.04

ARG VERSION
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | \
       gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" \
       > /etc/apt/sources.list.d/clickhouse.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       clickhouse-server=${VERSION} \
       clickhouse-client=${VERSION} \
       clickhouse-common-static=${VERSION} \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/lib/clickhouse /var/log/clickhouse-server \
       /etc/clickhouse-server/config.d /etc/clickhouse-server/users.d \
    && chown -R clickhouse:clickhouse /var/lib/clickhouse /var/log/clickhouse-server /etc/clickhouse-server

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8123 9000 9009

VOLUME /var/lib/clickhouse /var/log/clickhouse-server

ENTRYPOINT ["/entrypoint.sh"]

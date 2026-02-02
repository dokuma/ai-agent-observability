#!/bin/bash
set -eo pipefail

# Apply environment variables to config
if [ -n "$CLICKHOUSE_DB" ] && [ "$CLICKHOUSE_DB" != "default" ]; then
    mkdir -p /docker-entrypoint-initdb.d
    cat > /docker-entrypoint-initdb.d/create_db.sql <<EOF
CREATE DATABASE IF NOT EXISTS ${CLICKHOUSE_DB};
EOF
fi

if [ -n "$CLICKHOUSE_USER" ] && [ "$CLICKHOUSE_USER" != "default" ]; then
    cat > /etc/clickhouse-server/users.d/custom_user.xml <<EOF
<clickhouse>
    <users>
        <${CLICKHOUSE_USER}>
            <password>${CLICKHOUSE_PASSWORD:-}</password>
            <networks><ip>::/0</ip></networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </${CLICKHOUSE_USER}>
    </users>
</clickhouse>
EOF
fi

if [ -n "$CLICKHOUSE_PASSWORD" ]; then
    cat > /etc/clickhouse-server/users.d/default_password.xml <<EOF
<clickhouse>
    <users>
        <default>
            <password>${CLICKHOUSE_PASSWORD}</password>
        </default>
    </users>
</clickhouse>
EOF
fi

# Run init scripts
if [ -d /docker-entrypoint-initdb.d ]; then
    for f in /docker-entrypoint-initdb.d/*; do
        case "$f" in
            *.sql) echo "Running $f"; clickhouse-client --query "$(cat "$f")" || true ;;
        esac
    done
fi

# Start server
exec su-exec clickhouse clickhouse-server --config-file=/etc/clickhouse-server/config.xml "$@"

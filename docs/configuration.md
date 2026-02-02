# 設定リファレンス

すべての設定は環境変数または `.env` ファイルで指定する。
`core/config.py` の `Settings` クラスで定義。

## 環境変数一覧

### LLM

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `LLM_ENDPOINT` | `http://localhost:8000` | LLM API エンドポイント (OpenAI 互換) |
| `LLM_MODEL` | `llama-3.1-8b` | 使用モデル名 |

### 監視スタック

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus API |
| `LOKI_URL` | `http://localhost:3100` | Loki API |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana API |
| `GRAFANA_API_KEY` | (空) | Grafana API キー |

### MCP サーバ

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `MCP_GRAFANA_URL` | `http://localhost:8080` | Grafana MCP |
| `MCP_LOKI_URL` | `http://localhost:8081` | Loki MCP |
| `MCP_PROMETHEUS_URL` | `http://localhost:8082` | Prometheus MCP |

### Agent 動作制御

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `MAX_ITERATIONS` | `5` | 調査ループの最大反復回数 |
| `INVESTIGATION_TIMEOUT_SECONDS` | `120` | 調査タイムアウト (秒) |

### 通知

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SLACK_WEBHOOK_URL` | (空) | Slack 通知用 Webhook URL |

### Langfuse トレーシング

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `LANGFUSE_ENABLED` | `true` | トレーシング有効化 |
| `LANGFUSE_PUBLIC_KEY` | (空) | Langfuse Public Key |
| `LANGFUSE_SECRET_KEY` | (空) | Langfuse Secret Key |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Langfuse ホスト URL |

> **注:** Langfuse v3 では `LangfuseCallbackHandler` が環境変数 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` から自動設定される。
> `tracing.py` で `os.environ.setdefault()` を使い、Settings の値を環境変数に反映している。

## .env.example

```bash
# LLM
LLM_ENDPOINT=http://ollama:11434/v1
LLM_MODEL=qwen2.5:0.5b

# MCP
MCP_PROMETHEUS_URL=http://prometheus-mcp:9090
MCP_LOKI_URL=http://loki-mcp:8080
MCP_GRAFANA_URL=http://grafana-mcp:8080

# Monitoring
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100
GRAFANA_URL=http://grafana:3000

# Langfuse
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
LANGFUSE_BASE_URL=http://langfuse-web:3000
```

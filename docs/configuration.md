# 設定リファレンス

すべての設定は環境変数または `.env` ファイルで指定する。
`core/config.py` の `Settings` クラスで定義。

## 環境変数一覧

### LLM

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `LLM_ENDPOINT` | `http://localhost:8000` | LLM API エンドポイント (OpenAI 互換) |
| `LLM_MODEL` | `llama-3.1-8b` | 使用モデル名 |
| `LLM_API_KEY` | `not-needed` | LLM API キー |
| `LLM_CUSTOM_HEADER_*` | (なし) | カスタムヘッダー (`LLM_CUSTOM_HEADER_X_API_KEY=xxx` → `X-API-KEY: xxx`) |
| `LLM_VERIFY_SSL` | `true` | LLM API の SSL 検証 |
| `LLM_MAX_RETRIES` | `5` | LLM API の最大リトライ回数 |

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
| `MCP_KUBERNETES_URL` | `http://localhost:8083` | Kubernetes MCP |
| `MCP_TRANSPORT` | `sse` | グローバルデフォルトトランスポート (`sse` / `streamable_http`) |
| `MCP_GRAFANA_TRANSPORT` | (空 = `MCP_TRANSPORT`) | Grafana MCP トランスポート |
| `MCP_LOKI_TRANSPORT` | (空 = `MCP_TRANSPORT`) | Loki MCP トランスポート |
| `MCP_PROMETHEUS_TRANSPORT` | (空 = `MCP_TRANSPORT`) | Prometheus MCP トランスポート |
| `MCP_KUBERNETES_TRANSPORT` | (空 = `MCP_TRANSPORT`) | Kubernetes MCP トランスポート |

### Agent 動作制御

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `MAX_ITERATIONS` | `5` | 調査ループの最大反復回数 |
| `INVESTIGATION_TIMEOUT_SECONDS` | `300` | 調査タイムアウト (秒) |
| `REPORT_SEARCH_TIMEOUT_SECONDS` | `60` | レポート検索タイムアウト (秒) |
| `MAX_REACT_STEPS` | `5` | 各サブエージェントの ReAct ループ最大ステップ数 |
| `MAX_TOOL_RESULT_CHARS` | `8000` | MCP ツール結果の最大文字数 |
| `SEARCH_RELEVANCE_THRESHOLD` | `0.3` | Search-First のレポート検索スコア閾値 |

### LLM レートリミット

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `LLM_RATE_LIMIT_MAX_ATTEMPTS` | `3` | レートリミット時の最大リトライ回数 |
| `LLM_RATE_LIMIT_WAIT_MIN` | `5` | リトライ最小待機時間 (秒) |
| `LLM_RATE_LIMIT_WAIT_MAX` | `120` | リトライ最大待機時間 (秒) |

### MCP TLS

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `MCP_USE_TLS` | `false` | MCP 接続で TLS を使用するか |
| `MCP_VERIFY_SSL` | `true` | MCP 接続の SSL 検証 |
| `MCP_CA_BUNDLE` | (空) | カスタム CA 証明書パス (空の場合はシステムデフォルト) |

### データソースプリファレンス

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `DATASOURCE_PREFERENCES_PATH` | `data/datasource_preferences.json` | データソース選択のプリファレンス保存先 |

### レポートストア

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `REPORT_STORE_PATH` | `data/rca_reports.db` | RCA レポートの SQLite 保存先 |

### Embedding / ベクトル検索

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `QDRANT_ENABLED` | `false` | Qdrant ベクトル検索の有効化 |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant サーバー URL |
| `QDRANT_REPORTS_COLLECTION` | `rca_reports` | レポート用コレクション名 |
| `QDRANT_CHECKPOINTS_COLLECTION` | `checkpoint_outputs` | チェックポイント用コレクション名 |
| `EMBEDDING_ENDPOINT` | (空 = `LLM_ENDPOINT`) | Embedding API エンドポイント |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding モデル名 |
| `EMBEDDING_API_KEY` | (空 = `LLM_API_KEY`) | Embedding API キー |
| `EMBEDDING_DIMENSIONS` | `0` (モデルデフォルト) | ベクトル次元数の明示指定 |
| `RRF_K` | `60` | RRF (Reciprocal Rank Fusion) パラメータ |

> **注:** `EMBEDDING_ENDPOINT` / `EMBEDDING_API_KEY` が未設定の場合、LLM の設定値がフォールバックとして使用される。
> カスタムヘッダー (`LLM_CUSTOM_HEADER_*`) は Embedding リクエストにも自動適用される。

### 通知

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SLACK_WEBHOOK_URL` | (空) | Slack 通知用 Webhook URL |

### CORS

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:3000"]` | CORS 許可オリジン (JSON リスト) |

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
MCP_TRANSPORT=sse
MCP_PROMETHEUS_URL=http://prometheus-mcp:9090
MCP_LOKI_URL=http://loki-mcp:8080
MCP_GRAFANA_URL=http://grafana-mcp:8080
MCP_KUBERNETES_URL=http://kubernetes-mcp:8080
MCP_KUBERNETES_TRANSPORT=sse

# Monitoring
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100
GRAFANA_URL=http://grafana:3000

# Qdrant (ナレッジストア)
QDRANT_ENABLED=true
QDRANT_URL=http://qdrant:6333

# Embedding
EMBEDDING_ENDPOINT=http://ollama:11434/v1
EMBEDDING_MODEL=text-embedding-3-small

# Langfuse
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
LANGFUSE_BASE_URL=http://langfuse-web:3000
```

# セットアップ・運用ガイド

## 前提条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (パッケージマネージャ)
- Docker / Docker Compose

## ローカル開発

```bash
# 依存インストール
make install

# 開発用 (devツール含む)
make dev

# API サーバ起動
make run

# テスト
make test
make test-cov    # カバレッジ付き

# Lint / 型チェック
make lint        # ruff + mypy
make format      # ruff format
```

## Docker Compose (全サービス)

```bash
# .env がなければ .env.example からコピーされる
make docker-up
make docker-ps     # ステータス確認
make docker-logs   # ログ確認
make docker-down   # 停止
```

## 結合テスト

Agent アプリ以外のインフラを Docker で起動し、テストはホスト側から実行する。

```bash
# 1. インフラ起動
make integration-up

# 2. Ollama モデル準備待ち & ヘルスチェック
make integration-wait

# 3. テスト実行
make integration-test

# 4. 停止
make integration-down

# 5. ボリュームごと完全削除
make integration-clean
```

## サービス一覧とポート

| サービス | ポート | 用途 |
|----------|--------|------|
| agent (FastAPI) | 8000 | AI Agent API |
| Ollama | 11434 | LLM 推論 |
| Prometheus | 9090 | メトリクス収集 |
| Loki | 3100 | ログ集約 |
| Grafana | 3000 | ダッシュボード (admin/admin) |
| Langfuse | 3001 | LLM トレーシング |
| Prometheus MCP | 9091 | MCP サーバ |
| Loki MCP | 9092 | MCP サーバ |
| Grafana MCP | 9093 | MCP サーバ |

## Langfuse 初期設定

Docker Compose で自動的にセットアップされる。

- URL: `http://localhost:3001`
- 初期ユーザ: `admin@example.com` / `adminadmin`
- バックエンド: PostgreSQL + ClickHouse + Redis + MinIO (S3互換)

Langfuse v3 では環境変数でクライアントを構成する:

```
LANGFUSE_PUBLIC_KEY=pk-lf-dev
LANGFUSE_SECRET_KEY=sk-lf-dev
LANGFUSE_HOST=http://localhost:3001
```

# Secrets Management

本プロジェクトでは、Docker Secrets を活用したシークレット管理をサポートしています。
Docker Secrets はオプション機能であり、従来の環境変数（`.env`）方式との後方互換性を維持しています。

## 目次

- [概要](#概要)
- [Docker Secrets の使用方法](#docker-secrets-の使用方法)
- [シークレット一覧](#シークレット一覧)
- [Kubernetes Secrets への移行](#kubernetes-secrets-への移行)
- [HashiCorp Vault 統合](#hashicorp-vault-統合)
- [本番環境でのベストプラクティス](#本番環境でのベストプラクティス)

## 概要

シークレット（パスワード・トークン・暗号化キー）の管理には 3 段階のアプローチがあります。

| 段階 | 方式 | 推奨環境 |
|------|------|----------|
| 1 | 環境変数（`.env` ファイル） | 開発環境 |
| 2 | Docker Secrets（ファイルベース） | ステージング / 小規模本番 |
| 3 | HashiCorp Vault 等の外部シークレットマネージャ | 本番環境 |

## Docker Secrets の使用方法

### 1. secrets ディレクトリの作成

```bash
mkdir -p secrets
chmod 700 secrets
```

### 2. シークレットファイルの作成

各シークレットを個別のファイルに保存します。ファイルには値のみを記載し、末尾の改行は含めないでください。

```bash
# 例: 安全なパスワードを生成して保存
openssl rand -hex 16 | tr -d '\n' > secrets/gf_admin_password
openssl rand -hex 32 | tr -d '\n' > secrets/langfuse_nextauth_secret
openssl rand -hex 32 | tr -d '\n' > secrets/langfuse_salt
openssl rand -hex 32 | tr -d '\n' > secrets/langfuse_encryption_key
openssl rand -hex 16 | tr -d '\n' > secrets/postgres_password
openssl rand -hex 16 | tr -d '\n' > secrets/clickhouse_password
openssl rand -hex 16 | tr -d '\n' > secrets/redis_password
openssl rand -hex 16 | tr -d '\n' > secrets/minio_root_password
openssl rand -hex 16 | tr -d '\n' > secrets/langfuse_init_user_password
openssl rand -hex 16 | tr -d '\n' > secrets/webui_secret_key
echo -n "glsa_your_token_here" > secrets/grafana_service_account_token

chmod 600 secrets/*
```

### 3. .gitignore の確認

`secrets/` ディレクトリが `.gitignore` に含まれていることを確認してください。
シークレットファイルをリポジトリにコミットしてはいけません。

```
secrets/
```

### 4. コンテナ内でのシークレット読み取り

Docker Secrets は各コンテナ内の `/run/secrets/<シークレット名>` にマウントされます。

```bash
# コンテナ内でシークレットを読み取る例
cat /run/secrets/postgres_password
```

アプリケーションコードからシークレットを読み取る場合:

```python
from pathlib import Path

def get_secret(name: str, default: str = "") -> str:
    """Docker Secret またはフォールバックとして環境変数から値を取得する。"""
    secret_path = Path(f"/run/secrets/{name}")
    if secret_path.exists():
        return secret_path.read_text().strip()
    import os
    return os.environ.get(name.upper(), default)
```

### 5. 優先順位

シークレットの優先順位は以下のとおりです:

1. **環境変数**（`.env` または `docker-compose.yaml` の `environment:`）が設定されている場合はそちらを使用
2. **Docker Secrets**（`/run/secrets/` 配下のファイル）がフォールバックとして利用可能
3. **デフォルト値**（`docker-compose.yaml` の `${VAR:-default}` パターン）

> Docker Secrets は現在オプション機能です。`secrets/` ディレクトリが存在しない場合でも、
> 従来どおり `.env` ファイルの環境変数で動作します。

## シークレット一覧

| シークレット名 | 用途 | 使用サービス |
|----------------|------|-------------|
| `gf_admin_password` | Grafana 管理者パスワード | grafana |
| `grafana_service_account_token` | Grafana MCP 用サービスアカウントトークン | grafana, grafana-mcp |
| `postgres_password` | Langfuse PostgreSQL パスワード | langfuse-web, langfuse-worker, langfuse-postgres |
| `clickhouse_password` | Langfuse ClickHouse パスワード | langfuse-web, langfuse-worker, langfuse-clickhouse |
| `redis_password` | Langfuse Redis パスワード | langfuse-web, langfuse-worker, langfuse-redis |
| `minio_root_password` | Langfuse MinIO ルートパスワード | langfuse-web, langfuse-worker, langfuse-minio, langfuse-minio-init |
| `langfuse_nextauth_secret` | Langfuse NextAuth シークレット | langfuse-web |
| `langfuse_salt` | Langfuse ハッシュ用ソルト | langfuse-web |
| `langfuse_encryption_key` | Langfuse 暗号化キー（64文字 hex） | langfuse-web, langfuse-worker |
| `langfuse_init_user_password` | Langfuse 初期管理ユーザーパスワード | langfuse-web |
| `webui_secret_key` | Open WebUI セッションシークレット | open-webui |

## Kubernetes Secrets への移行

Docker Compose 環境から Kubernetes へ移行する場合、Docker Secrets を Kubernetes Secrets にマッピングできます。

### 基本的な移行手順

1. **Kubernetes Secret の作成**

```bash
# secrets/ ディレクトリのファイルから Kubernetes Secret を一括作成
kubectl create secret generic ai-agent-monitoring-secrets \
  --from-file=gf_admin_password=./secrets/gf_admin_password \
  --from-file=postgres_password=./secrets/postgres_password \
  --from-file=clickhouse_password=./secrets/clickhouse_password \
  --from-file=redis_password=./secrets/redis_password \
  --from-file=minio_root_password=./secrets/minio_root_password \
  --from-file=langfuse_nextauth_secret=./secrets/langfuse_nextauth_secret \
  --from-file=langfuse_salt=./secrets/langfuse_salt \
  --from-file=langfuse_encryption_key=./secrets/langfuse_encryption_key \
  --from-file=langfuse_init_user_password=./secrets/langfuse_init_user_password \
  --from-file=webui_secret_key=./secrets/webui_secret_key \
  --from-file=grafana_service_account_token=./secrets/grafana_service_account_token
```

2. **Pod での Secret マウント**

```yaml
# Deployment の例
spec:
  containers:
    - name: langfuse-web
      volumeMounts:
        - name: secrets
          mountPath: /run/secrets
          readOnly: true
  volumes:
    - name: secrets
      secret:
        secretName: ai-agent-monitoring-secrets
```

3. **環境変数として注入**

```yaml
env:
  - name: POSTGRES_PASSWORD
    valueFrom:
      secretKeyRef:
        name: ai-agent-monitoring-secrets
        key: postgres_password
```

> Docker Secrets と同じパス（`/run/secrets/`）にマウントすることで、
> アプリケーションコードの変更なしに移行可能です。

## HashiCorp Vault 統合

> この機能は将来対応予定です。以下は概要のみ記載します。

HashiCorp Vault を使用すると、以下の高度なシークレット管理が可能になります:

- **動的シークレット**: データベースパスワード等を一時的に生成し、自動的にローテーション
- **リース管理**: シークレットに有効期限を設定し、自動失効
- **監査ログ**: シークレットへのアクセスを完全に記録
- **暗号化サービス**: Vault の Transit エンジンを使ったデータ暗号化

### 想定する統合方式

1. **Vault Agent Sidecar**: 各 Pod に Vault Agent を配置し、`/run/secrets/` にシークレットを動的注入
2. **CSI Provider**: Kubernetes Secrets Store CSI Driver 経由で Vault からシークレットを取得
3. **直接 API 呼び出し**: アプリケーションコードから Vault API を直接利用

## 本番環境でのベストプラクティス

### 必須事項

- [ ] すべてのデフォルトパスワードを変更する（`admin`, `langfuse`, `clickhouse` 等）
- [ ] `openssl rand -hex 32` で生成した十分な長さのシークレットを使用する
- [ ] `secrets/` ディレクトリをバージョン管理から除外する（`.gitignore` に追加済み）
- [ ] シークレットファイルのパーミッションを `600` に設定する

### 推奨事項

- [ ] Docker Secrets またはそれ以上の仕組みを使い、`.env` ファイルへの平文保存を避ける
- [ ] シークレットのローテーション手順を策定する
- [ ] CI/CD パイプラインではシークレットマネージャ（GitHub Secrets, AWS Secrets Manager 等）を使用する
- [ ] 本番環境の `.env` ファイルはサーバー上にのみ配置し、ローカルに持ち出さない
- [ ] `LANGFUSE_ENCRYPTION_KEY` は 64 文字の hex 文字列（256 bit）を使用する
- [ ] Grafana のデフォルト匿名アクセス（`GF_AUTH_ANONYMOUS_ENABLED`）を無効にする

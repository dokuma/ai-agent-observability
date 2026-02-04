# LogQL 実践例

## 基本的なログ検索

### エラーログの検索

```logql
# シンプルなエラー検索
{job="varlogs"} |= "error"

# 大文字小文字を区別しない
{job="varlogs"} |~ "(?i)error"

# 複数キーワード（OR）
{job="varlogs"} |~ "error|warn|fatal"

# 複数キーワード（AND - 連結）
{job="varlogs"} |= "error" |= "connection"
```

### 特定ログの除外

```logql
# ヘルスチェックを除外
{job="nginx"} != "healthcheck"
{job="nginx"} !~ "/health|/ready|/live"

# デバッグログを除外
{job="app"} != "DEBUG"

# 複数パターンを除外
{job="app"} != "healthcheck" != "metrics" != "debug"
```

## Kubernetes ログ

### namespace/pod/container でのフィルタ

```logql
# 特定namespaceのログ
{namespace="production"}

# 特定Podのログ
{namespace="default", pod="api-server-abc123"}

# Podパターンマッチ
{namespace="default", pod=~"api-server.*"}

# 特定コンテナ
{namespace="default", container="app"}
```

### Podのエラーログ

```logql
# 本番環境のエラー
{namespace="production"} |= "error"

# 特定アプリのエラー
{namespace="default", app="api"} |= "error"

# OOMKillerのログ
{namespace="kube-system"} |~ "OOMKilled|Out of memory"
```

### コンテナ再起動の調査

```logql
# クラッシュループの兆候
{namespace="default"} |~ "CrashLoopBackOff|Error|Failed"

# Exit codeの確認
{namespace="default"} |~ "exit code|exited with"
```

## アプリケーションログ

### JSON ログの分析

```logql
# JSONをパースしてlevelでフィルタ
{job="app"} | json | level="error"

# ステータスコードでフィルタ
{job="app"} | json | status_code >= 500

# 複合条件
{job="app"} | json | level="error" | status_code >= 400
```

### レスポンスタイムの分析

```logql
# 遅いリクエストを抽出（1秒以上）
{job="app"} | json | duration > 1s

# 特定エンドポイントの遅いリクエスト
{job="app"} | json | path="/api/users" | duration > 500ms
```

### ユーザーアクティビティ

```logql
# 特定ユーザーのログ
{job="app"} | json | user_id="12345"

# ログインエラー
{job="app"} | json | event="login" | status="failed"
```

## nginx / Apache ログ

### アクセスログ分析

```logql
# 特定パスへのアクセス
{job="nginx"} |= "/api/"

# 特定ステータスコード
{job="nginx"} |~ `" 5[0-9]{2} `

# 特定IPからのアクセス
{job="nginx"} |= "192.168.1.100"
```

### エラーログ分析

```logql
# upstream接続エラー
{job="nginx"} |= "upstream"

# タイムアウト
{job="nginx"} |~ "timed out|timeout"

# 接続リセット
{job="nginx"} |= "connection reset"
```

## システムログ

### syslog

```logql
{job="varlogs", filename="/var/log/syslog"}
{job="varlogs", filename="/var/log/syslog"} |= "error"
```

### auth.log

```logql
# ログイン失敗
{job="varlogs", filename="/var/log/auth.log"} |= "Failed password"

# sudo実行
{job="varlogs", filename="/var/log/auth.log"} |= "sudo"

# SSH接続
{job="varlogs", filename="/var/log/auth.log"} |= "sshd"
```

### kernel ログ

```logql
# カーネルエラー
{job="varlogs"} |= "kernel" |~ "error|warning|critical"

# ディスクエラー
{job="varlogs"} |= "kernel" |~ "I/O error|disk"

# メモリ問題
{job="varlogs"} |= "kernel" |~ "Out of memory|oom-killer"
```

## メトリクス変換

### エラーカウント

```logql
# 5分間のエラー数
count_over_time({job="app"} |= "error" [5m])

# namespace別エラーレート
sum by (namespace) (rate({namespace=~".+"} |= "error" [5m]))
```

### ログボリューム

```logql
# アプリ別ログレート
sum by (app) (rate({job="containers"}[5m]))

# Pod別ログサイズ
sum by (pod) (bytes_rate({namespace="production"}[5m]))
```

### トップエラー発生源

```logql
# エラーが多いPodトップ10
topk(10, sum by (pod) (count_over_time({namespace="production"} |= "error" [1h])))
```

## トラブルシューティングパターン

### 500エラーの調査

```logql
# 500エラー発生時のログ
{job="nginx"} |~ `" 500 `

# バックエンドエラー
{job="app"} | json | status_code=500
```

### メモリ不足の調査

```logql
{namespace="production"} |~ "OutOfMemory|OOM|memory"
```

### 接続エラーの調査

```logql
{job="app"} |~ "connection refused|connection reset|timeout|ECONNREFUSED"
```

### 認証エラーの調査

```logql
{job="app"} |~ "unauthorized|forbidden|401|403|authentication failed"
```

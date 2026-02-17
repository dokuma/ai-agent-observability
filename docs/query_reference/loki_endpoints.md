# Loki HTTP API エンドポイントリファレンス

## 概要

このドキュメントは Grafana Loki の HTTP API エンドポイントの完全なリファレンスです。Loki はログ収集・検索基盤であり、HTTP API を通じてログの送信（push）、クエリ（query）、メタデータ取得（metadata）、ステータス確認（status）などの操作が可能です。

すべてのエンドポイントは REST API として提供され、curl や HTTP クライアントライブラリから呼び出すことができます。ログ取得、ログ送信、ラベル一覧取得、死活監視、ヘルスチェック、リアルタイムテール、インデックス統計など、運用に必要な操作を網羅しています。

**ベースURL**: 以下の例では `http://localhost:3100` をベースURLとして使用します。

---

## クエリAPI (Query API)

クエリAPIは LogQL クエリを実行してログデータやメトリクスデータを取得するためのエンドポイントです。ログ検索、ログ取得、ログストリーム取得の中核となるAPIです。

### インスタントクエリ (Instant Query) - `/loki/api/v1/query`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/query`

特定の時刻における単一時点のクエリ（インスタントクエリ）を実行します。指定した時刻のログエントリまたはメトリクス結果を返します。直近のログ確認や、ある瞬間の状態を調べる場合に使います。ログ取得の最も基本的なAPIです。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `query` | はい | string | - | 実行する LogQL クエリ文字列。ログストリームセレクタとフィルタを記述する |
| `limit` | いいえ | integer | 100 | 返却するログエントリの最大数。上限を制御する |
| `time` | いいえ | string | 現在時刻 | クエリの評価時刻。RFC3339 形式または Unix タイムスタンプ（ナノ秒）で指定 |
| `direction` | いいえ | string | `backward` | ログのソート順。`forward`（古い順）または `backward`（新しい順） |

**curlの例**:

```bash
# 基本的なインスタントクエリ - エラーログを取得
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query={job="varlogs"} |= "error"' \
  --data-urlencode 'limit=10' | jq .

# 特定時刻を指定してクエリ実行
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query={job="app"} |= "error"' \
  --data-urlencode 'time=2024-01-15T10:00:00Z' \
  --data-urlencode 'limit=50' | jq .

# メトリクスクエリ（直近5分のエラー数）
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query=count_over_time({job="app"} |= "error" [5m])' | jq .
```

**レスポンス例**:

```json
{
  "status": "success",
  "data": {
    "resultType": "streams",
    "result": [
      {
        "stream": {
          "job": "varlogs",
          "filename": "/var/log/syslog"
        },
        "values": [
          ["1705312800000000000", "Jan 15 10:00:00 server1 app[1234]: error: connection timeout"],
          ["1705312790000000000", "Jan 15 09:59:50 server1 app[1234]: error: disk full"]
        ]
      }
    ],
    "stats": {
      "summary": {
        "bytesProcessedPerSecond": 1048576,
        "totalBytesProcessed": 524288,
        "execTime": 0.5
      }
    }
  }
}
```

**ユースケース**:
- 直近のエラーログをすばやく確認する
- 特定時刻のログスナップショットを取得する
- メトリクスクエリで現在のカウント値を取得する

---

### 範囲クエリ (Range Query) - `/loki/api/v1/query_range`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/query_range`

時間範囲を指定してクエリ（範囲クエリ）を実行します。指定した開始時刻から終了時刻までのログエントリまたはメトリクス時系列データを返します。期間を指定したログ検索、時系列グラフの描画、ログ取得の中心的なAPIです。Grafana のダッシュボードパネルは内部的にこのエンドポイントを呼び出しています。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `query` | はい | string | - | 実行する LogQL クエリ文字列 |
| `limit` | いいえ | integer | 100 | 返却するログエントリの最大数 |
| `start` | いいえ | string | 1時間前 | 検索開始時刻。RFC3339 形式または Unix タイムスタンプ（ナノ秒） |
| `end` | いいえ | string | 現在時刻 | 検索終了時刻。RFC3339 形式または Unix タイムスタンプ（ナノ秒） |
| `step` | いいえ | string | 自動 | メトリクスクエリのステップ間隔（例: `5m`, `1h`）。ログクエリでは不要 |
| `direction` | いいえ | string | `backward` | ログのソート順。`forward` または `backward` |
| `interval` | いいえ | string | - | ログクエリ結果の間引き間隔。指定するとログの密度を下げて返却する |

**curlの例**:

```bash
# 過去1時間のエラーログを取得
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={job="varlogs"} |= "error"' \
  --data-urlencode 'start=1705309200000000000' \
  --data-urlencode 'end=1705312800000000000' \
  --data-urlencode 'limit=100' | jq .

# RFC3339形式で時間範囲を指定
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={job="app"} |= "error"' \
  --data-urlencode 'start=2024-01-15T09:00:00Z' \
  --data-urlencode 'end=2024-01-15T10:00:00Z' \
  --data-urlencode 'limit=500' \
  --data-urlencode 'direction=forward' | jq .

# メトリクス範囲クエリ（5分間隔でエラーレートを取得）
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query=rate({job="app"} |= "error" [5m])' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' \
  --data-urlencode 'step=5m' | jq .

# Kubernetes 名前空間別のログボリュームを時系列で取得
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query=sum by (namespace) (bytes_rate({namespace=~".+"} [5m]))' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' \
  --data-urlencode 'step=15m' | jq .
```

**レスポンス例（ログクエリ）**:

```json
{
  "status": "success",
  "data": {
    "resultType": "streams",
    "result": [
      {
        "stream": {
          "job": "app",
          "level": "error"
        },
        "values": [
          ["1705312800000000000", "error: connection timeout to database"],
          ["1705312700000000000", "error: failed to process request"],
          ["1705312600000000000", "error: upstream service unavailable"]
        ]
      }
    ],
    "stats": {
      "summary": {
        "bytesProcessedPerSecond": 2097152,
        "totalBytesProcessed": 1048576,
        "execTime": 0.5
      }
    }
  }
}
```

**レスポンス例（メトリクスクエリ）**:

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {
          "job": "app"
        },
        "values": [
          [1705312800, "5"],
          [1705313100, "3"],
          [1705313400, "8"]
        ]
      }
    ]
  }
}
```

**ユースケース**:
- 障害発生期間中のログを時系列で調査する
- エラーレートの推移をグラフ化する
- 特定期間のログを全件抽出してエクスポートする
- Grafana ダッシュボードのバックエンドクエリとして利用する

---

## メタデータAPI (Metadata API)

メタデータAPIは、Loki に保存されているラベル（label）やシリーズ（series）の情報を取得するためのエンドポイントです。ラベル一覧取得、ラベル値の確認、ログストリームの列挙など、クエリを組み立てる前の探索に使います。

### ラベル一覧取得 (Get Label Names) - `/loki/api/v1/labels`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/labels`

Loki に存在するすべてのラベル名（ラベルキー）の一覧を取得します。どのようなラベルが使えるかを調べるときに使います。ラベル一覧取得はクエリの組み立てに必須の操作です。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `start` | いいえ | string | 6時間前 | 検索開始時刻。この範囲内に存在するラベルのみ返却する |
| `end` | いいえ | string | 現在時刻 | 検索終了時刻 |

**curlの例**:

```bash
# 全ラベル名を取得
curl -G -s "http://localhost:3100/loki/api/v1/labels" | jq .

# 時間範囲を指定してラベル名を取得
curl -G -s "http://localhost:3100/loki/api/v1/labels" \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' | jq .
```

**レスポンス例**:

```json
{
  "status": "success",
  "data": [
    "app",
    "container",
    "env",
    "filename",
    "host",
    "instance",
    "job",
    "level",
    "namespace",
    "node_name",
    "pod",
    "service_name"
  ]
}
```

**ユースケース**:
- 利用可能なラベルを把握してクエリを構築する
- 新しく追加されたラベルの確認
- ラベル設計のレビュー・棚卸し

---

### ラベル値取得 (Get Label Values) - `/loki/api/v1/label/<name>/values`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/label/<name>/values`

指定したラベル名に対する値の一覧（ラベル値）を取得します。`<name>` 部分にラベル名を指定します。特定のラベルにどのような値が存在するか調べるときに使います。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `<name>` (パスパラメータ) | はい | string | - | 値を取得するラベル名（URLパスに埋め込む） |
| `start` | いいえ | string | 6時間前 | 検索開始時刻 |
| `end` | いいえ | string | 現在時刻 | 検索終了時刻 |
| `query` | いいえ | string | - | 対象を絞り込む LogQL セレクタ（例: `{job="app"}`） |

**curlの例**:

```bash
# "job" ラベルの全値を取得
curl -G -s "http://localhost:3100/loki/api/v1/label/job/values" | jq .

# "namespace" ラベルの値を取得
curl -G -s "http://localhost:3100/loki/api/v1/label/namespace/values" | jq .

# 特定ジョブに絞り込んで "level" ラベルの値を取得
curl -G -s "http://localhost:3100/loki/api/v1/label/level/values" \
  --data-urlencode 'query={job="app"}' | jq .

# 時間範囲を指定して取得
curl -G -s "http://localhost:3100/loki/api/v1/label/pod/values" \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' | jq .
```

**レスポンス例**:

```json
{
  "status": "success",
  "data": [
    "app-server",
    "nginx",
    "promtail",
    "varlogs"
  ]
}
```

**ユースケース**:
- 特定ラベルの取りうる値を列挙してクエリのフィルタ値を確認する
- Grafana のテンプレート変数（Variables）のデータソースとして利用する
- ログストリームのラベル値を動的に取得してドロップダウンリストを構成する

---

### シリーズ取得 (Get Series) - `/loki/api/v1/series`

**HTTPメソッド**: `GET` / `POST`
**パス**: `/loki/api/v1/series`

指定した条件にマッチするログストリーム（シリーズ）のラベルセットを取得します。どのようなラベルの組み合わせでログストリームが存在するかを調べるときに使います。シリーズ一覧の取得はカーディナリティの分析やデバッグに役立ちます。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `match[]` | はい | string | - | ログストリームセレクタ。複数指定可能 |
| `start` | いいえ | string | 6時間前 | 検索開始時刻 |
| `end` | いいえ | string | 現在時刻 | 検索終了時刻 |

**curlの例**:

```bash
# 特定ジョブのシリーズ一覧を取得
curl -G -s "http://localhost:3100/loki/api/v1/series" \
  --data-urlencode 'match[]={job="varlogs"}' | jq .

# 複数のマッチ条件を指定
curl -G -s "http://localhost:3100/loki/api/v1/series" \
  --data-urlencode 'match[]={job="app"}' \
  --data-urlencode 'match[]={job="nginx"}' | jq .

# 名前空間でフィルタ
curl -G -s "http://localhost:3100/loki/api/v1/series" \
  --data-urlencode 'match[]={namespace="production"}' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' | jq .

# POSTメソッドで送信（パラメータが長い場合）
curl -s "http://localhost:3100/loki/api/v1/series" \
  --data-urlencode 'match[]={namespace=~"prod.*", app=~"api.*"}' | jq .
```

**レスポンス例**:

```json
{
  "status": "success",
  "data": [
    {
      "job": "varlogs",
      "filename": "/var/log/syslog",
      "host": "server1"
    },
    {
      "job": "varlogs",
      "filename": "/var/log/auth.log",
      "host": "server1"
    },
    {
      "job": "varlogs",
      "filename": "/var/log/kern.log",
      "host": "server1"
    }
  ]
}
```

**ユースケース**:
- どのようなログストリームが存在するか列挙する
- ラベルのカーディナリティ（組み合わせ数）を分析する
- 不要なストリームの特定と削除対象の洗い出し
- ストリームセレクタの正しさを検証する

---

## ログ送信API (Push API)

### ログ送信 (Push Log Entries) - `/loki/api/v1/push`

**HTTPメソッド**: `POST`
**パス**: `/loki/api/v1/push`
**Content-Type**: `application/json`

ログエントリを Loki に送信（push）します。Promtail や Grafana Agent などのログ収集エージェントが内部的に使用するエンドポイントですが、直接呼び出してログを送信することも可能です。ログ送信、ログ投入、ログプッシュに使用します。

リクエストボディは `streams` 配列で構成され、各ストリームはラベルセット（`stream`）とログエントリの配列（`values`）を持ちます。

| パラメータ | 必須 | 型 | 説明 |
|-----------|------|-----|------|
| `streams` (ボディ) | はい | array | ログストリームの配列 |
| `streams[].stream` | はい | object | ログストリームのラベルセット（key-value） |
| `streams[].values` | はい | array | ログエントリの配列。各エントリは `[タイムスタンプ, ログ行]` の形式 |

**タイムスタンプ形式**: Unix タイムスタンプ（ナノ秒精度の文字列）。例: `"1705312800000000000"`

**curlの例**:

```bash
# 単一のログエントリを送信
curl -s -X POST "http://localhost:3100/loki/api/v1/push" \
  -H "Content-Type: application/json" \
  -d '{
    "streams": [
      {
        "stream": {
          "job": "test-app",
          "level": "info"
        },
        "values": [
          ["'$(date +%s)000000000'", "application started successfully"]
        ]
      }
    ]
  }'

# 複数のログエントリを一括送信
curl -s -X POST "http://localhost:3100/loki/api/v1/push" \
  -H "Content-Type: application/json" \
  -d '{
    "streams": [
      {
        "stream": {
          "job": "my-app",
          "env": "production",
          "level": "error"
        },
        "values": [
          ["1705312800000000000", "error: database connection failed"],
          ["1705312801000000000", "error: retrying connection attempt 1"],
          ["1705312802000000000", "error: retrying connection attempt 2"]
        ]
      }
    ]
  }'

# 複数ストリームを同時に送信
curl -s -X POST "http://localhost:3100/loki/api/v1/push" \
  -H "Content-Type: application/json" \
  -d '{
    "streams": [
      {
        "stream": {"job": "app", "level": "info"},
        "values": [["1705312800000000000", "request processed in 150ms"]]
      },
      {
        "stream": {"job": "app", "level": "error"},
        "values": [["1705312800000000000", "failed to connect to redis"]]
      }
    ]
  }'

# JSON構造化ログの送信
curl -s -X POST "http://localhost:3100/loki/api/v1/push" \
  -H "Content-Type: application/json" \
  -d '{
    "streams": [
      {
        "stream": {
          "job": "api-server",
          "env": "production"
        },
        "values": [
          ["'$(date +%s)000000000'", "{\"level\":\"error\",\"msg\":\"request failed\",\"status\":500,\"path\":\"/api/users\",\"duration_ms\":1234}"]
        ]
      }
    ]
  }'
```

**レスポンス**:

成功時は HTTP 204 No Content が返されます（レスポンスボディなし）。

```json

```

エラー時（例: 不正なリクエスト）:

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "entry out of order for stream"
}
```

**ユースケース**:
- カスタムアプリケーションから直接ログを送信する
- シェルスクリプトやバッチ処理のログを Loki に記録する
- テストデータの投入
- Promtail を使わない軽量なログ送信パイプラインの構築

**注意事項**:
- タイムスタンプは**ナノ秒精度**の文字列で指定する必要があります
- 同一ストリーム内のエントリは**時系列順**（古い順）で送信する必要があります。順序が逆だとエラーになります
- ラベルの組み合わせが一意のストリームを構成します。ラベルの数が多すぎるとカーディナリティが爆発するため注意が必要です

---

## テールAPI (Tail API)

### リアルタイムログテール (Tail Logs) - `/loki/api/v1/tail`

**HTTPメソッド**: `GET` (WebSocket)
**パス**: `/loki/api/v1/tail`

WebSocket 接続を確立して、リアルタイムにログをストリーミング受信（テール）します。`tail -f` コマンドのように、新しいログエントリが到着するたびにリアルタイムで受信できます。ライブログ、リアルタイムモニタリング、ログストリームの監視に使用します。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `query` | はい | string | - | テールする LogQL クエリ文字列 |
| `delay_for` | いいえ | integer | 0 | ログの遅延秒数。収集遅延を吸収するために使用 |
| `limit` | いいえ | integer | 100 | 初期接続時に返却する過去ログの最大数 |
| `start` | いいえ | string | 1時間前 | テール開始時刻 |

**curlの例**:

```bash
# websocat を使ったリアルタイムテール（推奨）
websocat "ws://localhost:3100/loki/api/v1/tail?query=%7Bjob%3D%22varlogs%22%7D"

# curlによるWebSocket接続（curl 7.86以降）
curl -s --no-buffer \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  "http://localhost:3100/loki/api/v1/tail?query=%7Bjob%3D%22varlogs%22%7D"

# wscat を使ったテール
wscat -c "ws://localhost:3100/loki/api/v1/tail?query=%7Bjob%3D%22varlogs%22%7D%20%7C%3D%20%22error%22"

# エラーログのみリアルタイムで監視
# クエリ: {job="app"} |= "error"
websocat "ws://localhost:3100/loki/api/v1/tail?query=%7Bjob%3D%22app%22%7D+%7C%3D+%22error%22&limit=10"
```

**レスポンス例（WebSocketメッセージ）**:

```json
{
  "streams": [
    {
      "stream": {
        "job": "varlogs",
        "filename": "/var/log/syslog"
      },
      "values": [
        ["1705312800000000000", "Jan 15 10:00:00 server1 app[1234]: new log entry arrived"]
      ]
    }
  ],
  "dropped_entries": []
}
```

**ユースケース**:
- デプロイ中のリアルタイムログ監視
- 障害発生中のライブデバッグ
- 特定エラーパターンのリアルタイム検知
- `tail -f` の代替としての運用中ログ確認

**注意事項**:
- WebSocket プロトコルを使用するため、通常の HTTP クライアントでは接続できません
- `websocat` または `wscat` などの WebSocket クライアントの使用を推奨します
- クエリパラメータはURLエンコードが必要です

---

## インデックスAPI (Index API)

インデックスAPIは、Loki のインデックスに関する統計情報やボリュームデータを取得するためのエンドポイントです。インデックス統計、ストレージ使用量、ボリューム分析に使用します。

### インデックス統計取得 (Get Index Stats) - `/loki/api/v1/index/stats`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/index/stats`

指定したクエリに対するインデックスの統計情報（ストリーム数、チャンク数、バイト数、エントリ数）を取得します。インデックス統計はストレージ使用量の分析やキャパシティプランニングに役立ちます。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `query` | はい | string | - | 統計を取得する LogQL セレクタ |
| `start` | いいえ | string | 6時間前 | 集計開始時刻 |
| `end` | いいえ | string | 現在時刻 | 集計終了時刻 |

**curlの例**:

```bash
# 特定ジョブのインデックス統計
curl -G -s "http://localhost:3100/loki/api/v1/index/stats" \
  --data-urlencode 'query={job="varlogs"}' | jq .

# 全ストリームの統計を取得
curl -G -s "http://localhost:3100/loki/api/v1/index/stats" \
  --data-urlencode 'query={job=~".+"}' | jq .

# 時間範囲を指定して統計を取得
curl -G -s "http://localhost:3100/loki/api/v1/index/stats" \
  --data-urlencode 'query={namespace="production"}' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' | jq .
```

**レスポンス例**:

```json
{
  "streams": 45,
  "chunks": 1230,
  "bytes": 52428800,
  "entries": 150000
}
```

**ユースケース**:
- ストレージ使用量の把握とキャパシティプランニング
- 特定ジョブやnamespaceのログボリューム確認
- カーディナリティの高いストリームの特定
- コスト分析のためのデータ量計測

---

### インデックスボリューム取得 (Get Index Volume) - `/loki/api/v1/index/volume`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/index/volume`

ラベルごとのログボリューム（データ量）を取得します。どのラベルの組み合わせが最も多くのデータを生成しているかを分析するために使用します。ボリューム分析、コスト最適化、ログの発生源分析に役立ちます。

| パラメータ | 必須 | 型 | デフォルト | 説明 |
|-----------|------|-----|-----------|------|
| `query` | はい | string | - | ボリュームを取得する LogQL セレクタ |
| `start` | いいえ | string | 6時間前 | 集計開始時刻 |
| `end` | いいえ | string | 現在時刻 | 集計終了時刻 |
| `limit` | いいえ | integer | 100 | 返却するシリーズの最大数 |
| `targetLabels` | いいえ | string | - | 集計対象のラベル名（カンマ区切り）。指定すると特定ラベルでグルーピングする |
| `aggregateBy` | いいえ | string | - | 集計方法の指定（`series` または `labels`） |

**curlの例**:

```bash
# 全ストリームのボリュームを取得
curl -G -s "http://localhost:3100/loki/api/v1/index/volume" \
  --data-urlencode 'query={job=~".+"}' \
  --data-urlencode 'limit=20' | jq .

# job ラベルでグルーピングしてボリュームを集計
curl -G -s "http://localhost:3100/loki/api/v1/index/volume" \
  --data-urlencode 'query={job=~".+"}' \
  --data-urlencode 'targetLabels=job' \
  --data-urlencode 'aggregateBy=labels' | jq .

# 特定 namespace のボリュームを pod 別に集計
curl -G -s "http://localhost:3100/loki/api/v1/index/volume" \
  --data-urlencode 'query={namespace="production"}' \
  --data-urlencode 'targetLabels=pod' \
  --data-urlencode 'aggregateBy=labels' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z' | jq .
```

**レスポンス例**:

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {
          "job": "varlogs"
        },
        "value": [1705312800, "52428800"]
      },
      {
        "metric": {
          "job": "nginx"
        },
        "value": [1705312800, "31457280"]
      },
      {
        "metric": {
          "job": "app"
        },
        "value": [1705312800, "10485760"]
      }
    ]
  }
}
```

**ユースケース**:
- ログ発生量の多いソース（ジョブ、Pod）を特定する
- ストレージコスト分析と最適化
- ログ量の異常増加を検知する
- ラベルごとのデータ量比較

---

## ステータスAPI (Status API)

### ビルド情報取得 (Get Build Info) - `/loki/api/v1/status/buildinfo`

**HTTPメソッド**: `GET`
**パス**: `/loki/api/v1/status/buildinfo`

Loki のビルド情報（バージョン、リビジョン、ビルド日時、Go バージョンなど）を取得します。バージョン確認、デバッグ情報の収集、互換性の確認に使用します。

| パラメータ | 必須 | 型 | 説明 |
|-----------|------|-----|------|
| なし | - | - | パラメータ不要 |

**curlの例**:

```bash
# ビルド情報を取得
curl -s "http://localhost:3100/loki/api/v1/status/buildinfo" | jq .

# バージョン番号だけ取得
curl -s "http://localhost:3100/loki/api/v1/status/buildinfo" | jq -r '.version'
```

**レスポンス例**:

```json
{
  "version": "2.9.4",
  "revision": "abc1234",
  "branch": "HEAD",
  "buildUser": "root@buildhost",
  "buildDate": "2024-01-10T12:00:00Z",
  "goVersion": "go1.21.5"
}
```

**ユースケース**:
- Loki のバージョンを確認する
- アップグレード前後のバージョン比較
- トラブルシューティング時のデバッグ情報収集
- API 互換性の確認

---

## 管理API (Admin API)

管理APIは Loki サーバーの運用・監視に使用するエンドポイントです。ヘルスチェック、死活監視、メトリクスエクスポート、設定確認などの管理操作を提供します。

### レディネスチェック (Readiness Check) - `/ready`

**HTTPメソッド**: `GET`
**パス**: `/ready`

Loki サーバーがリクエストを受け付ける準備ができているか（レディネス）を確認します。Kubernetes の readiness probe やロードバランサーのヘルスチェック、死活監視に使用します。サーバーが起動直後でまだ準備中の場合は 503 を返します。

| パラメータ | 必須 | 型 | 説明 |
|-----------|------|-----|------|
| なし | - | - | パラメータ不要 |

**curlの例**:

```bash
# レディネスチェック
curl -s -o /dev/null -w "%{http_code}" "http://localhost:3100/ready"

# レスポンスボディも確認
curl -s "http://localhost:3100/ready"

# ヘルスチェックスクリプトでの使用
if curl -sf "http://localhost:3100/ready" > /dev/null 2>&1; then
  echo "Loki is ready"
else
  echo "Loki is not ready"
fi
```

**レスポンス例**:

正常時（HTTP 200）:
```json
ready
```

異常時（HTTP 503）:
```json
Ingester not ready: waiting for 15s after being ready
```

**ユースケース**:
- Kubernetes の readinessProbe として設定する
- ロードバランサーのヘルスチェックエンドポイントとして使用する
- 起動スクリプトで Loki の起動完了を待機する
- 死活監視・ヘルスチェックの自動化

---

### メトリクスエクスポート (Metrics Export) - `/metrics`

**HTTPメソッド**: `GET`
**パス**: `/metrics`

Loki 自身の内部メトリクスを Prometheus 形式（テキストエクスポジション形式）でエクスポートします。Prometheus によるメトリクス収集（スクレイプ）の対象となるエンドポイントです。Loki の内部状態監視、パフォーマンス分析に使用します。

| パラメータ | 必須 | 型 | 説明 |
|-----------|------|-----|------|
| なし | - | - | パラメータ不要 |

**curlの例**:

```bash
# 全メトリクスを取得
curl -s "http://localhost:3100/metrics"

# 特定メトリクスをフィルタ（grepで絞り込み）
curl -s "http://localhost:3100/metrics" | grep "loki_request_duration"

# インジェスタ関連のメトリクスを確認
curl -s "http://localhost:3100/metrics" | grep "loki_ingester"

# ストレージ関連のメトリクスを確認
curl -s "http://localhost:3100/metrics" | grep "loki_chunk"
```

**レスポンス例**（テキスト形式、一部抜粋）:

```json
# HELP loki_request_duration_seconds Time (in seconds) spent serving HTTP requests.
# TYPE loki_request_duration_seconds histogram
loki_request_duration_seconds_bucket{method="GET",route="loki_api_v1_query",status_code="200",le="0.005"} 10
loki_request_duration_seconds_bucket{method="GET",route="loki_api_v1_query",status_code="200",le="0.01"} 25
# HELP loki_ingester_streams_created_total The total number of streams created per tenant.
# TYPE loki_ingester_streams_created_total counter
loki_ingester_streams_created_total{tenant=""} 45
```

**ユースケース**:
- Prometheus でスクレイプしてLokiの監視ダッシュボードを構築する
- クエリのレイテンシやエラーレートを監視する
- インジェスタやストレージのパフォーマンスを分析する
- アラートルール作成のための指標確認

---

### 設定取得 (Get Configuration) - `/config`

**HTTPメソッド**: `GET`
**パス**: `/config`

Loki の現在の実行時設定（ランタイムコンフィグ）を取得します。設定ファイルとデフォルト値がマージされた、実際に適用されている設定を確認できます。設定確認、トラブルシューティング、設定の差分比較に使用します。

| パラメータ | 必須 | 型 | 説明 |
|-----------|------|-----|------|
| なし | - | - | パラメータ不要 |

**curlの例**:

```bash
# 全設定を取得（YAML形式）
curl -s "http://localhost:3100/config"

# 特定セクションの設定を確認
curl -s "http://localhost:3100/config" | grep -A 10 "limits_config"

# 保持期間の設定を確認
curl -s "http://localhost:3100/config" | grep "retention"

# チャンクストアの設定を確認
curl -s "http://localhost:3100/config" | grep -A 20 "chunk_store_config"
```

**レスポンス例**（YAML形式、一部抜粋）:

```json
auth_enabled: false
server:
  http_listen_port: 3100
  grpc_listen_port: 9096
common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
limits_config:
  retention_period: 744h
  max_query_length: 721h
schema_config:
  configs:
    - from: 2020-10-24
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
```

**ユースケース**:
- 実際に適用されている設定値を確認する
- 保持期間（retention）やリミット値の確認
- 環境間の設定差分を比較する
- トラブルシューティング時に設定の不整合を特定する

---

## よくある使い方パターン (Common Usage Patterns)

### パターン1: ログ探索フロー（Discovery Flow）

ログを調査するときの典型的なフロー。まずラベルを確認し、段階的にクエリを絞り込みます。

```bash
# ステップ1: 利用可能なラベルを確認
curl -G -s "http://localhost:3100/loki/api/v1/labels" | jq .

# ステップ2: 特定ラベルの値を確認（例: job）
curl -G -s "http://localhost:3100/loki/api/v1/label/job/values" | jq .

# ステップ3: ストリームの存在を確認
curl -G -s "http://localhost:3100/loki/api/v1/series" \
  --data-urlencode 'match[]={job="app"}' | jq .

# ステップ4: ログを取得
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={job="app"} |= "error"' \
  --data-urlencode 'limit=50' | jq .
```

### パターン2: 障害調査パターン（Incident Investigation）

本番障害時のログ調査手順。時間範囲を絞り込んで原因を特定します。

```bash
# ステップ1: 障害時間帯のエラー数を確認
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query=count_over_time({namespace="production"} |= "error" [1h])' | jq .

# ステップ2: エラーログの詳細を取得
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={namespace="production"} |= "error"' \
  --data-urlencode 'start=2024-01-15T09:00:00Z' \
  --data-urlencode 'end=2024-01-15T10:00:00Z' \
  --data-urlencode 'limit=1000' \
  --data-urlencode 'direction=forward' | jq .

# ステップ3: 特定Podのログを全件取得
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={namespace="production", pod="api-server-abc123"}' \
  --data-urlencode 'start=2024-01-15T09:00:00Z' \
  --data-urlencode 'end=2024-01-15T10:00:00Z' \
  --data-urlencode 'limit=5000' \
  --data-urlencode 'direction=forward' | jq .
```

### パターン3: Loki ヘルスチェック・死活監視パターン（Health Check）

Loki サーバーの正常性を確認する手順。監視スクリプトや CI/CD パイプラインで使用します。

```bash
# レディネスチェック
curl -sf "http://localhost:3100/ready" && echo "OK" || echo "NOT READY"

# バージョン確認
curl -s "http://localhost:3100/loki/api/v1/status/buildinfo" | jq -r '.version'

# 簡単なクエリでデータアクセスを確認
curl -G -s "http://localhost:3100/loki/api/v1/labels" | jq -r '.status'
```

### パターン4: ストレージ容量分析パターン（Storage Analysis）

ログのストレージ使用量を分析し、最適化の対象を特定します。

```bash
# 全体のインデックス統計を確認
curl -G -s "http://localhost:3100/loki/api/v1/index/stats" \
  --data-urlencode 'query={job=~".+"}' | jq .

# ジョブ別のボリュームを確認（どのジョブが最もログを生成しているか）
curl -G -s "http://localhost:3100/loki/api/v1/index/volume" \
  --data-urlencode 'query={job=~".+"}' \
  --data-urlencode 'targetLabels=job' \
  --data-urlencode 'aggregateBy=labels' | jq .

# 特定期間のボリューム推移
curl -G -s "http://localhost:3100/loki/api/v1/index/volume" \
  --data-urlencode 'query={namespace="production"}' \
  --data-urlencode 'targetLabels=pod' \
  --data-urlencode 'aggregateBy=labels' \
  --data-urlencode 'start=2024-01-14T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T00:00:00Z' | jq .
```

### パターン5: スクリプトからのログ送信パターン（Programmatic Push）

シェルスクリプトや自動化パイプラインから Loki にログを送信します。

```bash
# 関数定義: Lokiにログを送信する汎用関数
send_log_to_loki() {
  local level="$1"
  local message="$2"
  local job="${3:-script}"
  local timestamp="$(date +%s)000000000"

  curl -s -X POST "http://localhost:3100/loki/api/v1/push" \
    -H "Content-Type: application/json" \
    -d "{
      \"streams\": [{
        \"stream\": {\"job\": \"${job}\", \"level\": \"${level}\"},
        \"values\": [[\"${timestamp}\", \"${message}\"]]
      }]
    }"
}

# 使用例
send_log_to_loki "info" "Deployment started" "deploy-script"
send_log_to_loki "error" "Deployment failed: timeout" "deploy-script"
```

### パターン6: Grafana テンプレート変数用のデータ取得（Template Variables）

Grafana ダッシュボードのテンプレート変数で使用する値を API から取得します。

```bash
# namespace 一覧を取得（ドロップダウンリスト用）
curl -G -s "http://localhost:3100/loki/api/v1/label/namespace/values" | jq -r '.data[]'

# 特定 namespace 内の pod 一覧を取得
curl -G -s "http://localhost:3100/loki/api/v1/label/pod/values" \
  --data-urlencode 'query={namespace="production"}' | jq -r '.data[]'

# アプリケーション名の一覧を取得
curl -G -s "http://localhost:3100/loki/api/v1/label/app/values" | jq -r '.data[]'
```

---

## エラーハンドリング (Error Handling)

Loki API のエラーレスポンスとその対処法をまとめます。HTTPステータスコードとエラーメッセージから原因を特定し、適切に対処する方法を説明します。

### 共通エラーレスポンス形式

Loki API はエラー発生時に以下のJSON形式でレスポンスを返します:

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "エラーメッセージの詳細"
}
```

### HTTP 400 Bad Request - リクエスト不正

クエリ構文エラーやパラメータの不備で発生します。

```bash
# エラー例: 不正なLogQLクエリ
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query=invalid query' | jq .
```

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "parse error at line 1, col 1: syntax error: unexpected IDENTIFIER"
}
```

**対処法**:
- LogQL クエリの構文を確認する（必ず `{}` で始まるセレクタが必要）
- パラメータ名のスペルミスを確認する
- URL エンコーディングが正しいか確認する

### HTTP 404 Not Found - エンドポイント不存在

存在しないエンドポイントにアクセスした場合に発生します。

```bash
# エラー例: パスの誤り
curl -s -o /dev/null -w "%{http_code}" "http://localhost:3100/loki/api/v1/wrong_endpoint"
```

**対処法**:
- エンドポイントのパスが正しいか確認する
- Loki のバージョンによって利用可能なエンドポイントが異なる場合がある
- `/loki/api/v1/` プレフィックスが正しいか確認する

### HTTP 422 Unprocessable Entity - 処理不可能なリクエスト

クエリは構文的に正しいが、実行できない場合に発生します。

```json
{
  "status": "error",
  "errorType": "execution",
  "error": "max query length exceeded"
}
```

**対処法**:
- クエリの時間範囲を狭くする（`start` と `end` を調整）
- `limit` パラメータで返却数を制限する
- `limits_config` の設定値を確認する

### HTTP 429 Too Many Requests - レート制限

短時間に大量のリクエストを送信した場合に発生します。リクエストのレート制限（スロットリング）に抵触しています。

```json
{
  "status": "error",
  "errorType": "too_many_requests",
  "error": "rate limit exceeded"
}
```

**対処法**:
- リクエストの送信頻度を下げる
- バッチ処理の間隔を広げる
- `limits_config.max_query_parallelism` の設定値を確認する
- マルチテナント環境の場合、テナントごとのレート制限を確認する

### HTTP 500 Internal Server Error - サーバー内部エラー

Loki サーバー内部で予期しないエラーが発生した場合に返されます。

```json
{
  "status": "error",
  "errorType": "internal",
  "error": "internal server error"
}
```

**対処法**:
- Loki サーバーのログを確認する
- ストレージバックエンド（S3、GCS、ファイルシステム）のアクセス状態を確認する
- Loki サーバーのリソース（メモリ、CPU、ディスク）を確認する
- サーバーを再起動して問題が解消するか確認する

### HTTP 503 Service Unavailable - サービス利用不可

Loki サーバーが起動中または利用不可能な状態の場合に発生します。

```bash
# ヘルスチェックで確認
curl -s -o /dev/null -w "%{http_code}" "http://localhost:3100/ready"
# 503 が返る場合はサーバーが準備中
```

**対処法**:
- Loki サーバーの起動完了を待つ
- `/ready` エンドポイントで準備状態を確認する
- インジェスタやディストリビュータの状態を確認する
- 依存サービス（consul、etcd等）の状態を確認する

### タイムアウトエラー

クエリの実行に時間がかかりすぎた場合に発生します。

```json
{
  "status": "error",
  "errorType": "timeout",
  "error": "context deadline exceeded"
}
```

**対処法**:
- クエリの時間範囲を狭くする
- より具体的なラベルセレクタで絞り込む
- フィルタ（`|=`, `|~`）を追加してスキャン範囲を減らす
- `limits_config.query_timeout` の設定値を確認・調整する
- `step` パラメータを大きくしてデータポイントを減らす

### エントリ順序エラー（Push API）

ログ送信時にタイムスタンプの順序が不正な場合に発生します。

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "entry out of order for stream {job=\"app\"}"
}
```

**対処法**:
- 同一ストリームに送信するエントリのタイムスタンプが昇順になっているか確認する
- 過去のタイムスタンプでログを送信していないか確認する
- `limits_config.reject_old_samples` と `limits_config.reject_old_samples_max_age` の設定値を確認する

### デバッグ用の共通確認手順

問題が発生した場合の汎用的な調査手順をまとめます。

```bash
# 1. Lokiサーバーの死活確認（ヘルスチェック）
curl -sf "http://localhost:3100/ready" && echo "OK" || echo "FAIL"

# 2. バージョン確認
curl -s "http://localhost:3100/loki/api/v1/status/buildinfo" | jq .

# 3. 設定確認（制限値の確認）
curl -s "http://localhost:3100/config" | grep -A 5 "limits_config"

# 4. メトリクスからエラー率を確認
curl -s "http://localhost:3100/metrics" | grep "loki_request_duration_seconds_count"

# 5. 簡単なクエリでデータアクセスの正常性を確認
curl -G -s "http://localhost:3100/loki/api/v1/labels" | jq .
```

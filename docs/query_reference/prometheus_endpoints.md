# Prometheus HTTP API エンドポイントリファレンス

## 概要

Prometheus HTTP API は、メトリクス取得、メタデータ照会、ターゲット管理、アラート確認、ステータス確認、死活監視など、Prometheusサーバーとのプログラム的な対話を可能にするRESTful APIです。すべてのエンドポイントは `/api/v1` プレフィックス配下に配置されています。

レスポンスはJSON形式で、以下の共通構造を持ちます：

```json
{
  "status": "success" | "error",
  "data": "<レスポンスデータ>",
  "errorType": "<エラー種別>",
  "error": "<エラーメッセージ>"
}
```

本ドキュメントでは `PROMETHEUS_URL` として `http://localhost:9090` を使用します。

---

## クエリAPI (Query API)

PromQLクエリを実行してメトリクスデータを取得するためのAPIです。即時クエリ (Instant Query) と範囲クエリ (Range Query) の2種類があります。

### `/api/v1/query` — 即時クエリ (Instant Query)

特定の時刻における単一の評価結果を返す即時クエリを実行します。メトリクス取得の最も基本的な操作です。

- **HTTPメソッド**: `GET` / `POST`
- **パス**: `/api/v1/query`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `query` | はい | PromQLクエリ文字列 (PromQL expression) |
| `time` | いいえ | 評価時刻。RFC3339形式またはUnixタイムスタンプ。省略時は現在時刻 |
| `timeout` | いいえ | クエリタイムアウト。例: `30s`, `1m`。省略時はサーバーのデフォルト値 |

**curlの例 — GETリクエスト:**

```bash
# 現在の全ターゲットの稼働状態を取得
curl -s 'http://localhost:9090/api/v1/query?query=up'

# 特定時刻を指定してクエリ実行
curl -s 'http://localhost:9090/api/v1/query?query=up&time=2024-01-15T10:00:00Z'

# Unixタイムスタンプで時刻を指定
curl -s 'http://localhost:9090/api/v1/query?query=up&time=1705312800'

# CPU使用率を取得
curl -s 'http://localhost:9090/api/v1/query?query=100%20-%20(avg%20by%20(instance)%20(rate(node_cpu_seconds_total%7Bmode%3D%22idle%22%7D%5B5m%5D))%20*%20100)'

# タイムアウトを指定してクエリ実行
curl -s 'http://localhost:9090/api/v1/query?query=up&timeout=10s'
```

**curlの例 — POSTリクエスト（長いクエリ向き）:**

```bash
curl -s -X POST 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)' \
  --data-urlencode 'time=2024-01-15T10:00:00Z'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {
          "__name__": "up",
          "instance": "localhost:9090",
          "job": "prometheus"
        },
        "value": [1705312800, "1"]
      }
    ]
  }
}
```

**ユースケース:**
- 現在のメトリクス値の取得（サービスの稼働状態確認、リソース使用率の確認）
- ダッシュボードのシングルスタットパネル用データ取得
- アラート条件の手動テスト
- スクリプトからの定期的なメトリクスチェック

---

### `/api/v1/query_range` — 範囲クエリ (Range Query)

指定した時間範囲にわたるクエリ結果を返します。グラフ描画やトレンド分析に使用します。時系列グラフの描画に不可欠なエンドポイントです。

- **HTTPメソッド**: `GET` / `POST`
- **パス**: `/api/v1/query_range`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `query` | はい | PromQLクエリ文字列 (PromQL expression) |
| `start` | はい | 開始時刻。RFC3339形式またはUnixタイムスタンプ |
| `end` | はい | 終了時刻。RFC3339形式またはUnixタイムスタンプ |
| `step` | はい | クエリの解像度ステップ幅。例: `15s`, `1m`, `5m` |
| `timeout` | いいえ | クエリタイムアウト |

**curlの例:**

```bash
# 過去1時間のCPU使用率を15秒間隔で取得
curl -s 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)' \
  --data-urlencode 'start=2024-01-15T09:00:00Z' \
  --data-urlencode 'end=2024-01-15T10:00:00Z' \
  --data-urlencode 'step=15s'

# 過去24時間のメモリ使用率を5分間隔で取得
curl -s 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100' \
  --data-urlencode 'start=2024-01-14T10:00:00Z' \
  --data-urlencode 'end=2024-01-15T10:00:00Z' \
  --data-urlencode 'step=5m'

# Unixタイムスタンプを使用した範囲クエリ
curl -s "http://localhost:9090/api/v1/query_range?query=up&start=$(date -d '-1 hour' +%s)&end=$(date +%s)&step=60s"
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {
          "__name__": "up",
          "instance": "localhost:9090",
          "job": "prometheus"
        },
        "values": [
          [1705309200, "1"],
          [1705309260, "1"],
          [1705309320, "1"]
        ]
      }
    ]
  }
}
```

**ユースケース:**
- Grafanaなどのダッシュボードでの時系列グラフ描画
- トレンド分析や容量計画のためのデータ収集
- 障害発生前後の時系列データの調査・振り返り
- SLO/SLIレポートの生成

---

## メタデータAPI (Metadata API)

ラベル名、ラベル値、時系列のメタ情報を取得するためのAPIです。ダッシュボード構築やオートコンプリート機能の実装に活用できます。

### `/api/v1/labels` — ラベル一覧取得 (Get Label Names)

Prometheus内に存在するすべてのラベル名の一覧を取得します。ラベル名の探索や、フィルタ条件の構築に使用します。

- **HTTPメソッド**: `GET` / `POST`
- **パス**: `/api/v1/labels`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `start` | いいえ | 検索対象の開始時刻 |
| `end` | いいえ | 検索対象の終了時刻 |
| `match[]` | いいえ | 対象を絞り込むシリーズセレクタ。複数指定可 |

**curlの例:**

```bash
# すべてのラベル名を取得
curl -s 'http://localhost:9090/api/v1/labels'

# 特定のメトリクスに関連するラベル名のみ取得
curl -s 'http://localhost:9090/api/v1/labels?match[]=node_cpu_seconds_total'

# 複数のセレクタでフィルタリング
curl -s 'http://localhost:9090/api/v1/labels?match[]=up&match[]=node_cpu_seconds_total'

# 時間範囲を指定してラベル名を取得
curl -s 'http://localhost:9090/api/v1/labels?start=2024-01-15T00:00:00Z&end=2024-01-15T12:00:00Z'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": [
    "__name__",
    "container",
    "cpu",
    "device",
    "instance",
    "job",
    "mode",
    "namespace",
    "pod"
  ]
}
```

**ユースケース:**
- ダッシュボードのドロップダウンフィルタ構築
- クエリエディタのオートコンプリート機能
- 利用可能なラベルの調査・探索

---

### `/api/v1/label/<name>/values` — ラベル値一覧取得 (Get Label Values)

指定したラベル名に対応する値の一覧を取得します。たとえば `job` ラベルにどのような値があるかを調べるときに使用します。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/label/<label_name>/values`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `<label_name>` | はい | パスに含めるラベル名 (URLパスの一部) |
| `start` | いいえ | 検索対象の開始時刻 |
| `end` | いいえ | 検索対象の終了時刻 |
| `match[]` | いいえ | 対象を絞り込むシリーズセレクタ。複数指定可 |

**curlの例:**

```bash
# "job" ラベルの全値を取得
curl -s 'http://localhost:9090/api/v1/label/job/values'

# "instance" ラベルの全値を取得
curl -s 'http://localhost:9090/api/v1/label/instance/values'

# "__name__" ラベルの値（= 全メトリクス名一覧）を取得
curl -s 'http://localhost:9090/api/v1/label/__name__/values'

# 特定メトリクスに関連する "mode" ラベルの値を取得
curl -s 'http://localhost:9090/api/v1/label/mode/values?match[]=node_cpu_seconds_total'

# 特定のジョブに限定して "instance" ラベル値を取得
curl -s 'http://localhost:9090/api/v1/label/instance/values?match[]=up{job="prometheus"}'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": [
    "alertmanager",
    "grafana",
    "node-exporter",
    "prometheus"
  ]
}
```

**ユースケース:**
- 特定ラベルの取り得る値をリストアップ（ジョブ名、インスタンス名の一覧）
- Grafanaテンプレート変数の値ソース
- メトリクス名の全量一覧取得（`__name__`ラベルを指定）

---

### `/api/v1/series` — 時系列メタデータ取得 (Find Series)

指定したセレクタにマッチする時系列のラベルセットを検索して返します。どのような時系列が存在するかを把握するのに役立ちます。

- **HTTPメソッド**: `GET` / `POST`
- **パス**: `/api/v1/series`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `match[]` | はい | シリーズセレクタ。少なくとも1つ必須。複数指定可 |
| `start` | いいえ | 検索対象の開始時刻 |
| `end` | いいえ | 検索対象の終了時刻 |

**curlの例:**

```bash
# "up" メトリクスにマッチする全時系列を取得
curl -s 'http://localhost:9090/api/v1/series?match[]=up'

# 特定ジョブの時系列を検索
curl -s 'http://localhost:9090/api/v1/series?match[]=up{job="prometheus"}'

# 複数のセレクタで検索（OR条件）
curl -s 'http://localhost:9090/api/v1/series?match[]=up&match[]=process_cpu_seconds_total'

# 正規表現を使ったセレクタで検索
curl -s 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]={__name__=~"node_cpu.*"}'

# 時間範囲を限定して検索
curl -s 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]=up' \
  --data-urlencode 'start=2024-01-15T00:00:00Z' \
  --data-urlencode 'end=2024-01-15T12:00:00Z'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": [
    {
      "__name__": "up",
      "instance": "localhost:9090",
      "job": "prometheus"
    },
    {
      "__name__": "up",
      "instance": "localhost:9100",
      "job": "node-exporter"
    }
  ]
}
```

**ユースケース:**
- 特定メトリクスのラベルの組み合わせを調査
- カーディナリティ調査（時系列数の把握）
- メトリクスの存在確認とラベル構成の理解

---

### `/api/v1/metadata` — メトリクスメタデータ取得 (Get Metric Metadata)

各メトリクスの型 (type)、説明 (HELP)、単位 (unit) といったメタデータを取得します。メトリクスの意味や使い方を調べるときに有用です。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/metadata`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `metric` | いいえ | 特定のメトリクス名でフィルタ |
| `limit` | いいえ | 返すメトリクスの最大数 |
| `limit_per_metric` | いいえ | メトリクスごとに返すメタデータの最大数 |

**curlの例:**

```bash
# 全メトリクスのメタデータを取得
curl -s 'http://localhost:9090/api/v1/metadata'

# 特定メトリクスのメタデータを取得
curl -s 'http://localhost:9090/api/v1/metadata?metric=node_cpu_seconds_total'

# 取得数を制限
curl -s 'http://localhost:9090/api/v1/metadata?limit=10'

# メトリクスごとのメタデータ数を制限
curl -s 'http://localhost:9090/api/v1/metadata?limit_per_metric=1'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "node_cpu_seconds_total": [
      {
        "type": "counter",
        "help": "Seconds the CPUs spent in each mode.",
        "unit": ""
      }
    ],
    "node_memory_MemTotal_bytes": [
      {
        "type": "gauge",
        "help": "Memory information field MemTotal_bytes.",
        "unit": ""
      }
    ]
  }
}
```

**ユースケース:**
- メトリクスの型（counter / gauge / histogram / summary）を確認
- HELP文字列からメトリクスの意味・用途を理解
- ドキュメント自動生成やメトリクスカタログの構築

---

## ターゲットAPI (Targets API)

Prometheus のスクレイプターゲット（監視対象）に関する情報を取得するAPIです。ターゲット一覧の確認やスクレイプの状態確認に使用します。

### `/api/v1/targets` — ターゲット一覧取得 (List Targets)

設定されたすべてのスクレイプターゲットの状態を取得します。ターゲットがアクティブか、ドロップされたか、最終スクレイプの成功/失敗状態を確認できます。監視対象の死活監視やスクレイプ失敗の調査に不可欠です。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/targets`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `state` | いいえ | フィルタするターゲット状態。`active`, `dropped`, `any` のいずれか |
| `scrapePool` | いいえ | 特定のスクレイププールでフィルタ（Prometheus 2.47+） |

**curlの例:**

```bash
# 全ターゲットの状態を取得
curl -s 'http://localhost:9090/api/v1/targets'

# アクティブなターゲットのみ取得
curl -s 'http://localhost:9090/api/v1/targets?state=active'

# ドロップされた（設定で除外された）ターゲットのみ取得
curl -s 'http://localhost:9090/api/v1/targets?state=dropped'

# 特定のスクレイププールでフィルタ
curl -s 'http://localhost:9090/api/v1/targets?scrapePool=node-exporter'

# jq で down のターゲットのみ抽出
curl -s 'http://localhost:9090/api/v1/targets?state=active' | \
  jq '.data.activeTargets[] | select(.health == "down")'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "activeTargets": [
      {
        "discoveredLabels": {
          "__address__": "localhost:9090",
          "__scheme__": "http",
          "job": "prometheus"
        },
        "labels": {
          "instance": "localhost:9090",
          "job": "prometheus"
        },
        "scrapePool": "prometheus",
        "scrapeUrl": "http://localhost:9090/metrics",
        "globalUrl": "http://localhost:9090/metrics",
        "lastError": "",
        "lastScrape": "2024-01-15T10:00:15.123Z",
        "lastScrapeDuration": 0.012,
        "health": "up",
        "scrapeInterval": "15s",
        "scrapeTimeout": "10s"
      }
    ],
    "droppedTargets": [
      {
        "discoveredLabels": {
          "__address__": "localhost:9091",
          "job": "dropped-service"
        }
      }
    ]
  }
}
```

**ユースケース:**
- スクレイプターゲットの死活監視、状態確認
- スクレイプ失敗の原因調査（`lastError` フィールドの確認）
- ドロップされたターゲットの特定（relabel設定の検証）
- サービスディスカバリの動作確認

---

### `/api/v1/targets/metadata` — ターゲットメタデータ取得 (Get Target Metadata)

各ターゲットが公開しているメトリクスのメタデータ（型、HELP文字列）を取得します。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/targets/metadata`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `match_target` | いいえ | ターゲットのラベルセレクタ。例: `{job="prometheus"}` |
| `metric` | いいえ | メトリクス名でフィルタ |
| `limit` | いいえ | 返すメタデータの最大数 |

**curlの例:**

```bash
# 全ターゲットのメタデータを取得
curl -s 'http://localhost:9090/api/v1/targets/metadata'

# 特定ターゲット（ジョブ）のメタデータを取得
curl -s 'http://localhost:9090/api/v1/targets/metadata?match_target={job="prometheus"}'

# 特定メトリクスのメタデータを取得
curl -s 'http://localhost:9090/api/v1/targets/metadata?metric=go_goroutines'

# 取得数を制限して取得
curl -s 'http://localhost:9090/api/v1/targets/metadata?limit=5'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": [
    {
      "target": {
        "instance": "localhost:9090",
        "job": "prometheus"
      },
      "metric": "prometheus_build_info",
      "type": "gauge",
      "help": "A metric with a constant '1' value labeled by version, revision, branch, goversion from which prometheus was built, and the goos and goarch it was built for.",
      "unit": ""
    }
  ]
}
```

**ユースケース:**
- 特定ターゲットが公開しているメトリクスの種類と意味を調査
- エクスポーターの出力内容の確認
- メトリクスの型（counter / gauge / histogram / summary）の確認

---

## ルール・アラートAPI (Rules & Alerts API)

Prometheusに設定されたアラートルール (Alerting Rules)、レコーディングルール (Recording Rules) の状態、および発火中のアラートを確認するAPIです。

### `/api/v1/rules` — ルール一覧取得 (List Rules)

設定されたすべてのアラートルールとレコーディングルールを取得します。ルールの状態、評価結果、エラー情報を確認できます。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/rules`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `type` | いいえ | ルール種別でフィルタ: `alert`（アラートルール）, `record`（レコーディングルール） |
| `rule_name[]` | いいえ | 特定のルール名でフィルタ。複数指定可 |
| `rule_group[]` | いいえ | 特定のルールグループ名でフィルタ。複数指定可 |
| `file[]` | いいえ | 特定のルールファイルでフィルタ。複数指定可 |

**curlの例:**

```bash
# 全ルールを取得
curl -s 'http://localhost:9090/api/v1/rules'

# アラートルールのみ取得
curl -s 'http://localhost:9090/api/v1/rules?type=alert'

# レコーディングルールのみ取得
curl -s 'http://localhost:9090/api/v1/rules?type=record'

# 特定のルール名でフィルタ
curl -s 'http://localhost:9090/api/v1/rules?rule_name[]=HighCpuUsage'

# 特定のルールグループでフィルタ
curl -s 'http://localhost:9090/api/v1/rules?rule_group[]=node_alerts'

# jq で発火中のアラートのみ抽出
curl -s 'http://localhost:9090/api/v1/rules?type=alert' | \
  jq '.data.groups[].rules[] | select(.state == "firing")'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "groups": [
      {
        "name": "node_alerts",
        "file": "/etc/prometheus/rules/node.yml",
        "rules": [
          {
            "state": "firing",
            "name": "InstanceDown",
            "query": "up == 0",
            "duration": 300,
            "labels": {
              "severity": "critical"
            },
            "annotations": {
              "summary": "Instance {{ $labels.instance }} is down",
              "description": "{{ $labels.instance }} of job {{ $labels.job }} has been down for more than 5 minutes."
            },
            "alerts": [
              {
                "labels": {
                  "alertname": "InstanceDown",
                  "instance": "localhost:9100",
                  "job": "node-exporter",
                  "severity": "critical"
                },
                "annotations": {
                  "summary": "Instance localhost:9100 is down"
                },
                "state": "firing",
                "activeAt": "2024-01-15T09:55:00.000Z",
                "value": "0e+00"
              }
            ],
            "health": "ok",
            "evaluationTime": 0.001,
            "lastEvaluation": "2024-01-15T10:00:00.000Z",
            "type": "alerting"
          }
        ],
        "interval": 60,
        "evaluationTime": 0.002,
        "lastEvaluation": "2024-01-15T10:00:00.000Z"
      }
    ]
  }
}
```

**ユースケース:**
- アラートルールの設定内容と状態確認
- レコーディングルールの評価結果チェック
- ルール評価エラーの調査
- 発火中のアラートの一覧取得

---

### `/api/v1/alerts` — アラート確認 (Get Alerts)

現在発火中 (firing) または保留中 (pending) のアラートを一覧で取得します。アラート確認の最も直接的な方法です。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/alerts`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# 全アラートを取得（firing + pending）
curl -s 'http://localhost:9090/api/v1/alerts'

# jq で firing のアラートのみ抽出
curl -s 'http://localhost:9090/api/v1/alerts' | \
  jq '.data.alerts[] | select(.state == "firing")'

# jq で pending のアラートのみ抽出
curl -s 'http://localhost:9090/api/v1/alerts' | \
  jq '.data.alerts[] | select(.state == "pending")'

# アラート数を数える
curl -s 'http://localhost:9090/api/v1/alerts' | \
  jq '.data.alerts | length'

# 重大度が critical のアラートのみ抽出
curl -s 'http://localhost:9090/api/v1/alerts' | \
  jq '.data.alerts[] | select(.labels.severity == "critical")'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "alerts": [
      {
        "labels": {
          "alertname": "InstanceDown",
          "instance": "localhost:9100",
          "job": "node-exporter",
          "severity": "critical"
        },
        "annotations": {
          "summary": "Instance localhost:9100 is down",
          "description": "localhost:9100 of job node-exporter has been down for more than 5 minutes."
        },
        "state": "firing",
        "activeAt": "2024-01-15T09:55:00.000Z",
        "value": "0e+00"
      }
    ]
  }
}
```

**ユースケース:**
- 現在発火中のアラートの確認
- アラート通知の検証・デバッグ
- インシデント対応時のアラート状況把握
- 監視ダッシュボードへのアラートステータス表示

---

## ステータスAPI (Status API)

Prometheusサーバー自体の設定情報、起動フラグ、ランタイム情報、ビルド情報、TSDBの状態を取得するAPIです。設定確認やトラブルシューティングに使用します。

### `/api/v1/status/config` — 設定確認 (Get Configuration)

現在のPrometheus設定ファイル（prometheus.yml）の内容をYAML文字列として返します。実行中の設定内容を確認するときに使います。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/status/config`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# 現在の設定を取得
curl -s 'http://localhost:9090/api/v1/status/config'

# YAML部分のみを抽出して表示
curl -s 'http://localhost:9090/api/v1/status/config' | jq -r '.data.yaml'

# 設定をファイルに保存
curl -s 'http://localhost:9090/api/v1/status/config' | jq -r '.data.yaml' > prometheus_current.yml
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "yaml": "global:\n  scrape_interval: 15s\n  evaluation_interval: 15s\nscrape_configs:\n  - job_name: prometheus\n    static_configs:\n      - targets:\n        - localhost:9090\n"
  }
}
```

**ユースケース:**
- 実行中のPrometheus設定内容の確認
- 設定変更後のリロード結果の検証
- 設定のバックアップ取得
- 複数環境間の設定比較

---

### `/api/v1/status/flags` — 起動フラグ確認 (Get Command-Line Flags)

Prometheusサーバーの起動時に適用されたコマンドラインフラグの一覧を取得します。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/status/flags`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# 全起動フラグを取得
curl -s 'http://localhost:9090/api/v1/status/flags'

# 特定フラグの値を確認（例: データ保持期間）
curl -s 'http://localhost:9090/api/v1/status/flags' | jq '.data["storage.tsdb.retention.time"]'

# ストレージ関連のフラグを確認
curl -s 'http://localhost:9090/api/v1/status/flags' | jq '.data | to_entries[] | select(.key | startswith("storage"))'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "alertmanager.notification-queue-capacity": "10000",
    "config.file": "/etc/prometheus/prometheus.yml",
    "log.level": "info",
    "storage.tsdb.path": "/prometheus",
    "storage.tsdb.retention.time": "15d",
    "web.enable-lifecycle": "true",
    "web.listen-address": "0.0.0.0:9090"
  }
}
```

**ユースケース:**
- データ保持期間 (retention) の確認
- ストレージパスの確認
- ライフサイクルAPI の有効/無効確認
- デバッグ時の起動パラメータ確認

---

### `/api/v1/status/runtimeinfo` — ランタイム情報取得 (Get Runtime Information)

Prometheusプロセスのランタイム情報（ゴルーチン数、メモリ使用量、ストレージ情報など）を取得します。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/status/runtimeinfo`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# ランタイム情報を取得
curl -s 'http://localhost:9090/api/v1/status/runtimeinfo'

# ストレージ保持期間の確認
curl -s 'http://localhost:9090/api/v1/status/runtimeinfo' | jq '.data.storageRetention'

# ゴルーチン数の確認
curl -s 'http://localhost:9090/api/v1/status/runtimeinfo' | jq '.data.goroutineCount'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "startTime": "2024-01-01T00:00:00.000Z",
    "CWD": "/",
    "reloadConfigSuccess": true,
    "lastConfigTime": "2024-01-15T08:00:00.000Z",
    "corruptionCount": 0,
    "goroutineCount": 42,
    "GOMAXPROCS": 4,
    "GOMEMLIMIT": "0",
    "GOGC": "",
    "GODEBUG": "",
    "storageRetention": "15d"
  }
}
```

**ユースケース:**
- Prometheusプロセスの健全性確認
- 設定リロードの成功/失敗確認（`reloadConfigSuccess`, `lastConfigTime`）
- リソース使用状況の確認（ゴルーチン数）
- ストレージの破損検出（`corruptionCount`）

---

### `/api/v1/status/buildinfo` — ビルド情報取得 (Get Build Information)

Prometheusのバージョン、リビジョン、ブランチ、Goバージョンなどのビルド情報を取得します。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/status/buildinfo`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# ビルド情報を取得
curl -s 'http://localhost:9090/api/v1/status/buildinfo'

# バージョン番号のみ取得
curl -s 'http://localhost:9090/api/v1/status/buildinfo' | jq -r '.data.version'

# Goバージョンを確認
curl -s 'http://localhost:9090/api/v1/status/buildinfo' | jq -r '.data.goVersion'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "version": "2.49.1",
    "revision": "a]b1c2d3e4f5",
    "branch": "HEAD",
    "buildUser": "root@builder",
    "buildDate": "20240110-12:00:00",
    "goVersion": "go1.21.5"
  }
}
```

**ユースケース:**
- Prometheusのバージョン確認
- アップグレード前後のバージョン検証
- バグレポート作成時の環境情報収集
- 互換性チェック

---

### `/api/v1/status/tsdb` — TSDB統計取得 (Get TSDB Statistics)

Prometheus内蔵のTSDB（時系列データベース）の統計情報を取得します。カーディナリティの高い（時系列数が多い）ラベルやメトリクスの特定に役立ちます。

- **HTTPメソッド**: `GET`
- **パス**: `/api/v1/status/tsdb`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| `limit` | いいえ | 上位N件を返す（デフォルト: 10） |

**curlの例:**

```bash
# TSDB統計を取得
curl -s 'http://localhost:9090/api/v1/status/tsdb'

# 上位20件のカーディナリティ情報を取得
curl -s 'http://localhost:9090/api/v1/status/tsdb?limit=20'

# 時系列数が多いメトリクスのトップ10を表示
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.seriesCountByMetricName[:10]'

# ラベル値数が多いラベルのトップ10を表示
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.labelValueCountByLabelName[:10]'
```

**レスポンス例:**

```json
{
  "status": "success",
  "data": {
    "headStats": {
      "numSeries": 1234,
      "numLabelPairs": 567,
      "chunkCount": 8901,
      "minTime": 1705200000000,
      "maxTime": 1705312800000
    },
    "seriesCountByMetricName": [
      {"name": "node_cpu_seconds_total", "value": 64},
      {"name": "node_filesystem_size_bytes", "value": 32}
    ],
    "labelValueCountByLabelName": [
      {"name": "__name__", "value": 200},
      {"name": "instance", "value": 15}
    ],
    "memoryInBytesByLabelName": [
      {"name": "__name__", "value": 4096},
      {"name": "instance", "value": 1024}
    ],
    "seriesCountByLabelValuePair": [
      {"name": "job=prometheus", "value": 150},
      {"name": "job=node-exporter", "value": 120}
    ]
  }
}
```

**ユースケース:**
- カーディナリティ爆発（高カーディナリティ問題）の調査
- TSDB のメモリ使用量の分析
- 時系列数が急増した原因の特定
- ストレージ最適化のためのラベル構成分析

---

## 管理API (Admin / Lifecycle API)

Prometheusサーバーのヘルスチェック、レディネス確認、設定リロードに使用する管理エンドポイントです。コンテナオーケストレーション（Kubernetes等）のヘルスチェックや運用自動化で頻繁に使用します。

### `/-/healthy` — ヘルスチェック (Health Check)

Prometheusサーバーが正常に動作しているかを確認するヘルスチェック用エンドポイントです。死活監視のプローブとして使用します。サーバーが起動していれば常に200を返します。

- **HTTPメソッド**: `GET`
- **パス**: `/-/healthy`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# ヘルスチェック（死活監視）
curl -s -o /dev/null -w '%{http_code}' 'http://localhost:9090/-/healthy'

# レスポンスボディを確認
curl -s 'http://localhost:9090/-/healthy'

# タイムアウト付きヘルスチェック
curl -s --max-time 5 -o /dev/null -w '%{http_code}' 'http://localhost:9090/-/healthy'
```

**レスポンス例:**

正常時はHTTP 200と以下のテキスト：

```
Prometheus Server is Healthy.
```

**ユースケース:**
- Kubernetesの `livenessProbe` として設定
- ロードバランサーのヘルスチェック
- 外部死活監視システムからの監視
- 起動スクリプトでの起動完了確認

---

### `/-/ready` — レディネス確認 (Readiness Check)

Prometheusサーバーがリクエストを処理する準備ができているかを確認します。WAL（Write-Ahead Log）のリプレイが完了し、クエリを受け付け可能な状態であるかを示します。

- **HTTPメソッド**: `GET`
- **パス**: `/-/ready`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし |

**curlの例:**

```bash
# レディネス確認
curl -s -o /dev/null -w '%{http_code}' 'http://localhost:9090/-/ready'

# レスポンスボディを確認
curl -s 'http://localhost:9090/-/ready'

# 起動完了を待機するスクリプト
until curl -s -o /dev/null -w '%{http_code}' 'http://localhost:9090/-/ready' | grep -q 200; do
  echo "Waiting for Prometheus to be ready..."
  sleep 2
done
echo "Prometheus is ready!"
```

**レスポンス例:**

準備完了時はHTTP 200と以下のテキスト：

```
Prometheus Server is Ready.
```

準備未完了時はHTTP 503を返します。

**ユースケース:**
- Kubernetesの `readinessProbe` として設定
- 起動後のクエリ実行可能状態の確認
- デプロイメント時のローリングアップデート制御
- ロードバランサーのトラフィック制御

---

### `/-/reload` — 設定リロード (Reload Configuration)

Prometheus設定ファイル（prometheus.yml）とルールファイルを再読み込みします。プロセスを再起動せずに設定変更を反映できます。

**注意**: このエンドポイントは `--web.enable-lifecycle` フラグを付けてPrometheusを起動した場合にのみ使用可能です。

- **HTTPメソッド**: `POST`
- **パス**: `/-/reload`

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (なし) | — | パラメータなし。POSTリクエストが必須 |

**curlの例:**

```bash
# 設定をリロード
curl -s -X POST 'http://localhost:9090/-/reload'

# ステータスコードも確認してリロード
curl -s -o /dev/null -w '%{http_code}' -X POST 'http://localhost:9090/-/reload'

# リロード後に設定の反映を確認
curl -s -X POST 'http://localhost:9090/-/reload' && \
  sleep 2 && \
  curl -s 'http://localhost:9090/api/v1/status/runtimeinfo' | jq '{reloadConfigSuccess: .data.reloadConfigSuccess, lastConfigTime: .data.lastConfigTime}'
```

**レスポンス例:**

成功時はHTTP 200を返します。設定ファイルに文法エラーがある場合は200を返しますが、設定は適用されません。`/api/v1/status/runtimeinfo` の `reloadConfigSuccess` フィールドでリロード成否を確認してください。

**ユースケース:**
- 設定変更後のホットリロード（再起動不要の設定反映）
- アラートルール追加・変更の即時反映
- CI/CDパイプラインでの自動設定デプロイ
- スクレイプターゲット追加時の設定適用

---

## OTLP API (OpenTelemetry Protocol)

OpenTelemetry Protocol (OTLP) を使ってメトリクスをPrometheusに送信するためのエンドポイントです。OpenTelemetryエコシステムとの連携に使用します。

### `/api/v1/otlp/v1/metrics` — OTLPメトリクス受信 (OTLP Metrics Receiver)

OpenTelemetry形式のメトリクスデータをPrometheusにプッシュします。OTLPエクスポーターからのメトリクス受信に使用します。

**注意**: このエンドポイントは `--web.enable-otlp-receiver` フラグを付けてPrometheusを起動した場合にのみ使用可能です（Prometheus 2.47以降で実験的サポート）。

- **HTTPメソッド**: `POST`
- **パス**: `/api/v1/otlp/v1/metrics`
- **Content-Type**: `application/x-protobuf` (Protocol Buffers形式)

| パラメータ | 必須 | 説明 |
|-----------|------|------|
| (リクエストボディ) | はい | OTLP Protocol Buffers形式のメトリクスデータ |

**curlの例:**

```bash
# OpenTelemetry Collector の設定例（otlp exporter設定）
# prometheus の endpoint として以下を指定:
# endpoint: http://localhost:9090/api/v1/otlp/v1/metrics

# OTLP受信の有効化確認（フラグの確認）
curl -s 'http://localhost:9090/api/v1/status/flags' | jq '.data["web.enable-otlp-receiver"]'

# テスト用: OTel Collectorからの送信を確認した後、メトリクスが存在するか確認
curl -s 'http://localhost:9090/api/v1/query?query=up{job="otel-collector"}'
```

**OpenTelemetry Collector 設定例:**

```bash
# otel-collector-config.yaml の exporters セクション例:
# exporters:
#   otlphttp/prometheus:
#     endpoint: http://localhost:9090/api/v1/otlp
#     tls:
#       insecure: true
```

**ユースケース:**
- OpenTelemetry SDK / Collector からPrometheusへのメトリクス送信
- プッシュ型メトリクス収集（Pull型のスクレイプではなく）
- OpenTelemetryベースのアプリケーションとの統合
- マルチシグナル (traces, metrics, logs) 統合環境でのメトリクスパイプライン構築

---

## よくある使い方パターン (Common Usage Patterns)

実運用で頻繁に使われるAPIの組み合わせパターンを紹介します。

### パターン1: サービス死活監視スクリプト

全サービスの稼働状態を一括確認するスクリプトです。メトリクス取得とターゲット一覧を組み合わせます。

```bash
# 全ターゲットの稼働状態をテーブル形式で表示
curl -s 'http://localhost:9090/api/v1/query?query=up' | \
  jq -r '.data.result[] | [.metric.job, .metric.instance, .value[1]] | @tsv' | \
  column -t -s $'\t' -N "JOB,INSTANCE,STATUS"

# ダウンしているターゲットのみ表示
curl -s 'http://localhost:9090/api/v1/query?query=up==0' | \
  jq -r '.data.result[] | "DOWN: \(.metric.job) / \(.metric.instance)"'
```

### パターン2: リソース使用率ダッシュボード

CPU、メモリ、ディスクの使用率をまとめて取得するパターンです。

```bash
# CPU使用率
echo "=== CPU Usage ==="
curl -s 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)' | \
  jq -r '.data.result[] | "\(.metric.instance): \(.value[1])%"'

# メモリ使用率
echo "=== Memory Usage ==="
curl -s 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100' | \
  jq -r '.data.result[] | "\(.metric.instance): \(.value[1])%"'

# ディスク使用率
echo "=== Disk Usage ==="
curl -s 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100' | \
  jq -r '.data.result[] | "\(.metric.instance): \(.value[1])%"'
```

### パターン3: アラート状況の定期レポート

アラートの状態を定期的に確認して通知するパターンです。アラート確認APIを使用します。

```bash
# 発火中アラートの件数とサマリを取得
ALERTS=$(curl -s 'http://localhost:9090/api/v1/alerts')
FIRING=$(echo "$ALERTS" | jq '[.data.alerts[] | select(.state == "firing")] | length')
PENDING=$(echo "$ALERTS" | jq '[.data.alerts[] | select(.state == "pending")] | length')

echo "Firing: ${FIRING}, Pending: ${PENDING}"

# 発火中アラートの詳細を表示
echo "$ALERTS" | jq -r '.data.alerts[] | select(.state == "firing") | "[\(.labels.severity)] \(.labels.alertname): \(.annotations.summary)"'
```

### パターン4: メトリクス探索・調査

どのようなメトリクスがあるかを調べ、利用方法を確認するパターンです。

```bash
# 全メトリクス名を取得
curl -s 'http://localhost:9090/api/v1/label/__name__/values' | jq -r '.data[]'

# 特定キーワードを含むメトリクスを検索
curl -s 'http://localhost:9090/api/v1/label/__name__/values' | \
  jq -r '.data[] | select(contains("cpu"))'

# メトリクスのメタデータ（型と説明）を確認
curl -s 'http://localhost:9090/api/v1/metadata?metric=node_cpu_seconds_total' | \
  jq '.data | to_entries[] | {name: .key, type: .value[0].type, help: .value[0].help}'

# メトリクスの時系列を確認（ラベルの組み合わせ）
curl -s 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]={__name__="node_cpu_seconds_total"}' | \
  jq '.data | length'
```

### パターン5: カーディナリティ調査

時系列数の急増（カーディナリティ爆発）を調査するパターンです。

```bash
# TSDB統計から高カーディナリティなメトリクスを特定
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.seriesCountByMetricName[:10]'

# ラベル値が多いラベルを特定
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.labelValueCountByLabelName[:10]'

# 総時系列数を確認
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.headStats.numSeries'

# 特定メトリクスの時系列数を確認
curl -s 'http://localhost:9090/api/v1/series' \
  --data-urlencode 'match[]={__name__=~"http_request.*"}' | \
  jq '.data | length'
```

### パターン6: 設定変更のワークフロー

設定ファイルを変更してリロードし、正しく反映されたことを確認する一連の流れです。

```bash
# 1. 現在の設定を保存
curl -s 'http://localhost:9090/api/v1/status/config' | jq -r '.data.yaml' > prometheus_backup.yml

# 2. 設定ファイルを編集（別途エディタで実施）

# 3. 設定をリロード
curl -s -X POST 'http://localhost:9090/-/reload'

# 4. リロードの成否を確認
sleep 2
curl -s 'http://localhost:9090/api/v1/status/runtimeinfo' | \
  jq '{reloadConfigSuccess: .data.reloadConfigSuccess, lastConfigTime: .data.lastConfigTime}'

# 5. 新しい設定内容を確認
curl -s 'http://localhost:9090/api/v1/status/config' | jq -r '.data.yaml'
```

### パターン7: Prometheusサーバーの状態一括確認

運用チェック用にPrometheusサーバー自身の状態をまとめて確認するパターンです。

```bash
# ヘルスチェック
echo "=== Health Check ==="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' 'http://localhost:9090/-/healthy'

# レディネス確認
echo "=== Readiness Check ==="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' 'http://localhost:9090/-/ready'

# バージョン情報
echo "=== Version ==="
curl -s 'http://localhost:9090/api/v1/status/buildinfo' | jq -r '.data.version'

# ターゲット状態の要約
echo "=== Targets Summary ==="
curl -s 'http://localhost:9090/api/v1/targets?state=active' | \
  jq '{total: (.data.activeTargets | length), up: ([.data.activeTargets[] | select(.health == "up")] | length), down: ([.data.activeTargets[] | select(.health == "down")] | length)}'

# アラート状態の要約
echo "=== Alerts Summary ==="
curl -s 'http://localhost:9090/api/v1/alerts' | \
  jq '{total: (.data.alerts | length), firing: ([.data.alerts[] | select(.state == "firing")] | length), pending: ([.data.alerts[] | select(.state == "pending")] | length)}'

# TSDB統計
echo "=== TSDB Stats ==="
curl -s 'http://localhost:9090/api/v1/status/tsdb' | \
  jq '.data.headStats'
```

---

## エラーハンドリング (Error Handling)

Prometheus HTTP APIで発生する一般的なエラーとその対処方法を解説します。

### HTTPステータスコード一覧

| ステータスコード | 意味 | 説明 |
|-----------------|------|------|
| `200 OK` | 成功 | リクエストが正常に処理された |
| `400 Bad Request` | 不正なリクエスト | クエリ構文エラー、パラメータ不足など |
| `404 Not Found` | 未検出 | 存在しないエンドポイントへのアクセス |
| `422 Unprocessable Entity` | 処理不可 | クエリが評価できない場合（タイムアウトなど） |
| `500 Internal Server Error` | サーバーエラー | Prometheus内部エラー |
| `503 Service Unavailable` | サービス利用不可 | サーバー準備中（`/-/ready` が503の場合）やクエリが実行不可能 |

### エラーレスポンスの形式

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "invalid parameter 'query': 1:1: parse error: unexpected end of input"
}
```

### エラー種別 (errorType) の一覧

| errorType | 説明 | 主な原因 |
|-----------|------|---------|
| `bad_data` | 不正なデータ | PromQLの構文エラー、パラメータの型不正 |
| `timeout` | タイムアウト | クエリの実行時間がタイムアウトを超過 |
| `canceled` | キャンセル | クライアントがリクエストを中断 |
| `execution` | 実行エラー | クエリの実行中にエラーが発生 |
| `internal` | 内部エラー | Prometheus内部の予期しないエラー |
| `not_found` | 未検出 | 指定されたリソースが存在しない |

### よくあるエラーと対処法

#### エラー1: PromQL構文エラー (bad_data)

```bash
# エラーの例: 括弧の閉じ忘れ
curl -s 'http://localhost:9090/api/v1/query?query=rate(http_requests_total[5m]'
```

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "invalid parameter 'query': 1:37: parse error: unclosed left parenthesis"
}
```

**対処法:** PromQLクエリの構文を確認してください。括弧、ブラケット、クォートの対応関係を見直します。

#### エラー2: クエリタイムアウト (timeout)

```bash
# タイムアウトが発生しやすい重いクエリの例
curl -s 'http://localhost:9090/api/v1/query?query=count({__name__=~".%2B"})&timeout=5s'
```

```json
{
  "status": "error",
  "errorType": "timeout",
  "error": "query timed out in expression evaluation"
}
```

**対処法:**
- `timeout` パラメータを増やす
- クエリの対象範囲を絞り込む（ラベルフィルタを追加）
- 範囲クエリの `step` を大きくする
- レコーディングルールを使って事前集計する

#### エラー3: match[] パラメータの不足

```bash
# エラーの例: /api/v1/series に match[] なしでアクセス
curl -s 'http://localhost:9090/api/v1/series'
```

```json
{
  "status": "error",
  "errorType": "bad_data",
  "error": "no match[] parameter provided"
}
```

**対処法:** `/api/v1/series` エンドポイントには最低1つの `match[]` パラメータが必須です。

#### エラー4: ライフサイクルAPIが無効 (not_found / 403)

```bash
# --web.enable-lifecycle フラグなしでリロードを試行
curl -s -X POST 'http://localhost:9090/-/reload'
```

**対処法:** Prometheus起動時に `--web.enable-lifecycle` フラグを追加してください。Docker環境では以下のように設定します：

```bash
# Docker Compose での設定例
# command:
#   - '--web.enable-lifecycle'
#   - '--config.file=/etc/prometheus/prometheus.yml'
```

#### エラー5: URLエンコーディングの問題

```bash
# 間違い: 特殊文字をエンコードせずにGETパラメータに含める
curl -s 'http://localhost:9090/api/v1/query?query=rate(http_requests_total{job="api"}[5m])'

# 正しい: --data-urlencode を使用する
curl -s -G 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=rate(http_requests_total{job="api"}[5m])'

# 正しい: POSTメソッドで --data-urlencode を使用する
curl -s -X POST 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=rate(http_requests_total{job="api"}[5m])'
```

**対処法:** `{}`, `[]`, `"` などの特殊文字を含むクエリは、`--data-urlencode` オプションを使用するか、手動でURLエンコードしてください。POSTメソッドの利用も有効です。

### デバッグのヒント

```bash
# リクエストの詳細を表示する（デバッグ用）
curl -v 'http://localhost:9090/api/v1/query?query=up' 2>&1

# レスポンスヘッダを確認する
curl -s -D - 'http://localhost:9090/api/v1/query?query=up' -o /dev/null

# jq でレスポンスを見やすく整形
curl -s 'http://localhost:9090/api/v1/query?query=up' | jq .

# エラー時にステータスコードと本文の両方を確認
curl -s -w '\nHTTP Status: %{http_code}\n' 'http://localhost:9090/api/v1/query?query=invalid('
```

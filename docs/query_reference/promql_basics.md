# PromQL 基本文法

## 概要

PromQL (Prometheus Query Language) はPrometheusの時系列データを検索・集約するためのクエリ言語です。

## 基本構文

### メトリクス名の指定

最もシンプルなクエリはメトリクス名のみです：

```promql
up
node_cpu_seconds_total
http_requests_total
```

### ラベルセレクタ

ラベルで絞り込む場合は中括弧 `{}` を使用します：

```promql
http_requests_total{job="api-server"}
http_requests_total{job="api-server", status="200"}
node_cpu_seconds_total{mode="idle", instance="localhost:9090"}
```

### ラベルマッチング演算子

| 演算子 | 説明 | 例 |
|--------|------|-----|
| `=` | 完全一致 | `{job="prometheus"}` |
| `!=` | 不一致 | `{job!="prometheus"}` |
| `=~` | 正規表現一致 | `{job=~"api.*"}` |
| `!~` | 正規表現不一致 | `{job!~"test.*"}` |

### 範囲ベクトル

時間範囲を指定して過去のデータを取得します：

```promql
http_requests_total{job="api"}[5m]    # 過去5分間
http_requests_total{job="api"}[1h]    # 過去1時間
http_requests_total{job="api"}[1d]    # 過去1日
```

時間単位：
- `s` - 秒
- `m` - 分
- `h` - 時間
- `d` - 日
- `w` - 週
- `y` - 年

### オフセット修飾子

過去の時点のデータを参照：

```promql
http_requests_total offset 5m    # 5分前の値
http_requests_total[5m] offset 1h  # 1時間前から5分間のデータ
```

## 算術演算

```promql
node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100
```

## 比較演算

```promql
http_requests_total > 100
node_cpu_seconds_total{mode="idle"} < 50
process_open_fds / process_max_fds > 0.8
```

## よくある間違い

### 間違い1: SQLスタイルの記述

```
# 間違い
SELECT * FROM metrics WHERE job = 'api'

# 正しい
{job="api"}
```

### 間違い2: ANDの使用

```
# 間違い
http_requests_total{job="api"} AND {status="500"}

# 正しい
http_requests_total{job="api", status="500"}
```

### 間違い3: シングルクォートの使用

```
# 間違い（動作はするが非推奨）
http_requests_total{job='api'}

# 正しい（ダブルクォート推奨）
http_requests_total{job="api"}
```

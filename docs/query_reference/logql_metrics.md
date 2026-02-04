# LogQL メトリクスクエリ

## 概要

LogQLはログからメトリクスを計算することができます。これを「メトリクスクエリ」と呼びます。

## ログ範囲集約

### count_over_time()

指定期間内のログエントリ数をカウント：

```logql
count_over_time({job="varlogs"}[5m])
count_over_time({job="varlogs"} |= "error" [1h])
count_over_time({namespace="production"} |~ "error|warn" [24h])
```

### rate()

1秒あたりのログエントリ数：

```logql
rate({job="varlogs"}[5m])
rate({job="varlogs"} |= "error" [5m])
```

### bytes_over_time()

指定期間内のログのバイト数：

```logql
bytes_over_time({job="varlogs"}[1h])
```

### bytes_rate()

1秒あたりのログバイト数：

```logql
bytes_rate({job="varlogs"}[5m])
```

## アンラップ式

パースしたフィールドの値を数値として集約：

```logql
# JSONログからdurationフィールドを抽出して平均
avg_over_time({job="app"} | json | unwrap duration [5m])

# レスポンスタイムの最大値
max_over_time({job="nginx"} | pattern `<_> <_> <_> <duration>` | unwrap duration [5m])
```

### unwrap関数

- `avg_over_time()` - 平均
- `min_over_time()` - 最小
- `max_over_time()` - 最大
- `sum_over_time()` - 合計
- `stdvar_over_time()` - 分散
- `stddev_over_time()` - 標準偏差
- `quantile_over_time()` - パーセンタイル
- `first_over_time()` - 最初の値
- `last_over_time()` - 最後の値

## 集約演算子

### sum

```logql
sum(count_over_time({job="varlogs"}[5m]))
sum by (level) (count_over_time({job="app"} | json [5m]))
```

### avg

```logql
avg(rate({job="app"}[5m]))
```

### min / max

```logql
max(count_over_time({namespace="production"} |= "error" [1h]))
```

### count

```logql
count(rate({job="app"} |= "error" [5m]))
```

### topk / bottomk

```logql
topk(5, sum by (pod) (rate({namespace="default"} |= "error" [5m])))
```

## by / without句

### by

指定ラベルでグループ化：

```logql
sum by (level) (count_over_time({job="app"} | json [5m]))
sum by (namespace, pod) (rate({job="containers"} |= "error" [5m]))
```

### without

指定ラベル以外でグループ化：

```logql
sum without (instance) (count_over_time({job="app"}[5m]))
```

## 実践例

### エラー率の計算

```logql
# 5分間のエラーログの数
count_over_time({job="app"} |= "error" [5m])

# namespace別のエラーレート
sum by (namespace) (rate({job="containers"} |= "error" [5m]))
```

### ログボリュームの監視

```logql
# アプリケーション別のログレート
sum by (app) (rate({job="containers"}[5m]))

# ログ量が多いPodトップ10
topk(10, sum by (pod) (bytes_rate({namespace="production"}[5m])))
```

### レスポンスタイム分析

```logql
# 平均レスポンスタイム
avg_over_time(
  {job="nginx"}
  | pattern `<_> - - [<_>] "<_>" <status> <_> "<_>" "<_>" <duration>`
  | unwrap duration [5m]
)

# 99パーセンタイルレスポンスタイム
quantile_over_time(0.99,
  {job="app"} | json | unwrap response_time [5m]
)
```

### エラーパターン分析

```logql
# エラータイプ別のカウント
sum by (error_type) (
  count_over_time({job="app"} | json | error_type != "" [1h])
)
```

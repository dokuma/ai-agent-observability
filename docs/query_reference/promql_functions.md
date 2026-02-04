# PromQL 関数リファレンス

## レート関数

### rate()

カウンタの1秒あたりの平均増加率を計算します。最も一般的に使用される関数です。

```promql
rate(http_requests_total[5m])
rate(node_cpu_seconds_total{mode="idle"}[5m])
```

**注意**: range vectorが必要です（`[5m]`など）

### irate()

直近2点間の瞬間的な増加率を計算します。より敏感ですがノイズが多い。

```promql
irate(http_requests_total[5m])
```

### increase()

指定期間内の増加量を計算します。

```promql
increase(http_requests_total[1h])  # 1時間での増加数
```

### delta()

ゲージの変化量を計算します。

```promql
delta(node_memory_MemAvailable_bytes[1h])
```

## 集約関数

### sum()

値の合計を計算します。

```promql
sum(http_requests_total)
sum by (job) (http_requests_total)
sum without (instance) (http_requests_total)
```

### avg()

平均値を計算します。

```promql
avg(node_cpu_seconds_total{mode="idle"})
avg by (instance) (rate(http_requests_total[5m]))
```

### min() / max()

```promql
min(node_memory_MemAvailable_bytes)
max by (job) (http_requests_total)
```

### count()

時系列の数をカウントします。

```promql
count(up)
count by (job) (up)
```

### topk() / bottomk()

上位/下位N件を取得します。

```promql
topk(5, rate(http_requests_total[5m]))
bottomk(3, node_memory_MemAvailable_bytes)
```

### quantile()

パーセンタイルを計算します。

```promql
quantile(0.95, rate(http_request_duration_seconds[5m]))
```

## 時間範囲関数

### avg_over_time()

期間内の平均値を計算します。

```promql
avg_over_time(node_cpu_seconds_total{mode="idle"}[5m])
```

### max_over_time() / min_over_time()

```promql
max_over_time(node_memory_MemUsed_bytes[1h])
min_over_time(process_open_fds[30m])
```

### sum_over_time()

```promql
sum_over_time(http_requests_total[1h])
```

### count_over_time()

```promql
count_over_time(up[1d])
```

## ヒストグラム関数

### histogram_quantile()

ヒストグラムからパーセンタイルを計算します。

```promql
histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```

**注意**: `_bucket`サフィックスのメトリクスと`le`ラベルが必要です。

## その他の関数

### absent()

時系列が存在しない場合に1を返します。

```promql
absent(up{job="critical-service"})
```

### changes()

指定期間内での値の変更回数を返します。

```promql
changes(process_start_time_seconds[1h])
```

### deriv()

線形回帰による1秒あたりの変化量を計算します。

```promql
deriv(node_memory_MemAvailable_bytes[1h])
```

### predict_linear()

線形回帰による将来予測を行います。

```promql
predict_linear(node_filesystem_avail_bytes[1h], 4*3600)  # 4時間後を予測
```

### label_replace()

ラベルを操作します。

```promql
label_replace(up, "host", "$1", "instance", "(.*):.*")
```

### vector()

スカラー値をベクトルに変換します。

```promql
vector(1)
```

### scalar()

単一の時系列をスカラーに変換します。

```promql
scalar(sum(up))
```

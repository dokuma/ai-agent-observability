# PromQL 実践例

## CPU メトリクス

### CPU使用率

```promql
# 全体のCPU使用率（idle以外の合計）
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# 特定モードのCPU使用率
rate(node_cpu_seconds_total{mode="user"}[5m]) * 100

# システムCPU使用率
rate(node_cpu_seconds_total{mode="system"}[5m]) * 100

# iowait
rate(node_cpu_seconds_total{mode="iowait"}[5m]) * 100
```

### CPU使用率が高いインスタンス

```promql
topk(5, 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))
```

## メモリ メトリクス

### メモリ使用量

```promql
# 使用中メモリ（バイト）
node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes

# メモリ使用率（パーセント）
(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100

# バッファ/キャッシュを除いた実使用量
node_memory_MemTotal_bytes - node_memory_MemFree_bytes - node_memory_Buffers_bytes - node_memory_Cached_bytes
```

### メモリ使用率が高いインスタンス

```promql
topk(5, (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100)
```

## ディスク メトリクス

### ディスク使用量

```promql
# ディスク使用量（バイト）
node_filesystem_size_bytes - node_filesystem_avail_bytes

# ディスク使用率（パーセント）
(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100

# 特定マウントポイント
(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100
```

### ディスク空き容量が少ないファイルシステム

```promql
node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / 1024 / 1024 / 1024 < 10
```

### ディスクI/O

```promql
# 読み取りスループット
rate(node_disk_read_bytes_total[5m])

# 書き込みスループット
rate(node_disk_written_bytes_total[5m])

# IOPS
rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m])
```

## ネットワーク メトリクス

### ネットワークトラフィック

```promql
# 受信トラフィック（バイト/秒）
rate(node_network_receive_bytes_total{device!="lo"}[5m])

# 送信トラフィック（バイト/秒）
rate(node_network_transmit_bytes_total{device!="lo"}[5m])
```

### ネットワークエラー

```promql
rate(node_network_receive_errs_total[5m])
rate(node_network_transmit_errs_total[5m])
```

## HTTP メトリクス

### リクエストレート

```promql
# 全体のリクエストレート
sum(rate(http_requests_total[5m]))

# ステータスコード別
sum by (status) (rate(http_requests_total[5m]))

# エンドポイント別
sum by (handler) (rate(http_requests_total[5m]))
```

### エラーレート

```promql
# 5xxエラー率
sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) * 100

# 4xxエラー率
sum(rate(http_requests_total{status=~"4.."}[5m])) / sum(rate(http_requests_total[5m])) * 100
```

### レイテンシ

```promql
# 平均レイテンシ
rate(http_request_duration_seconds_sum[5m]) / rate(http_request_duration_seconds_count[5m])

# 99パーセンタイルレイテンシ
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# 95パーセンタイルレイテンシ
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```

## コンテナ メトリクス

### コンテナCPU使用率

```promql
sum by (container, pod) (rate(container_cpu_usage_seconds_total{container!=""}[5m])) * 100
```

### コンテナメモリ使用量

```promql
sum by (container, pod) (container_memory_usage_bytes{container!=""})
```

### Pod再起動回数

```promql
sum by (pod, namespace) (kube_pod_container_status_restarts_total)
increase(kube_pod_container_status_restarts_total[1h])
```

## アラート関連

### サービスダウン検知

```promql
up == 0
up{job="critical-service"} == 0
```

### ターゲット数の確認

```promql
count(up)
count by (job) (up)
count(up == 1)  # 稼働中のターゲット数
```

### サービス稼働率

```promql
avg(up{job="api"}) * 100
```

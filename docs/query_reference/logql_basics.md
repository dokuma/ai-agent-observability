# LogQL 基本文法

## 概要

LogQL (Loki Query Language) はGrafana Lokiのログデータを検索・集約するためのクエリ言語です。

**重要**: LogQLはSQLではありません。完全に異なる文法です。

## 基本構文

### ログストリームセレクタ

全てのLogQLクエリは**必ず中括弧 `{}` で始まる**ラベルセレクタが必要です：

```logql
{job="varlogs"}
{namespace="default"}
{job="nginx", env="production"}
```

### ラベルマッチング演算子

| 演算子 | 説明 | 例 |
|--------|------|-----|
| `=` | 完全一致 | `{job="varlogs"}` |
| `!=` | 不一致 | `{job!="test"}` |
| `=~` | 正規表現一致 | `{job=~"api.*"}` |
| `!~` | 正規表現不一致 | `{job!~"test.*"}` |

### フィルタ式

ラベルセレクタの後にパイプ `|` でフィルタを追加します：

```logql
{job="varlogs"} |= "error"
{job="varlogs"} != "debug"
{job="varlogs"} |~ "error|warn"
{job="varlogs"} !~ "healthcheck"
```

### フィルタ演算子

| 演算子 | 説明 | 例 |
|--------|------|-----|
| `\|=` | 文字列を含む | `{job="app"} \|= "error"` |
| `!=` | 文字列を含まない | `{job="app"} != "debug"` |
| `\|~` | 正規表現にマッチ | `{job="app"} \|~ "error\|warn"` |
| `!~` | 正規表現にマッチしない | `{job="app"} !~ "health.*"` |

## パーサー

### JSON パーサー

JSONログをパースしてフィールドをラベルとして抽出：

```logql
{job="app"} | json
{job="app"} | json | level="error"
{job="app"} | json | status_code >= 500
```

### logfmt パーサー

key=value形式のログをパース：

```logql
{job="app"} | logfmt
{job="app"} | logfmt | level="error"
```

### pattern パーサー

パターンマッチングで抽出：

```logql
{job="nginx"} | pattern `<ip> - - [<timestamp>] "<method> <path> <_>" <status>`
```

### regexp パーサー

正規表現で抽出（名前付きキャプチャグループを使用）：

```logql
{job="app"} | regexp `(?P<level>\w+): (?P<message>.+)`
```

## ラインフォーマット

ログ行をフォーマット：

```logql
{job="app"} | json | line_format "{{.level}}: {{.message}}"
```

## ラベルフィルタ

パーサー後にラベルでフィルタ：

```logql
{job="app"} | json | level="error"
{job="app"} | json | status_code > 400
{job="app"} | json | duration > 1s
```

### 比較演算子

- `==`, `!=` - 等価比較
- `>`, `>=`, `<`, `<=` - 数値比較
- `=~`, `!~` - 正規表現比較

## よくある間違い

### 間違い1: SQLスタイルの記述

```
# 絶対に間違い - これはSQLでありLogQLではない
SELECT * FROM logs WHERE pod = 'my-pod' AND level = 'error'
kubernetes_pod_name = 'my-pod' AND log_time >= '2024-01-01'

# 正しいLogQL
{pod="my-pod"} |= "error"
```

### 間違い2: ラベルセレクタなしで始める

```
# 間違い - ラベルセレクタが必須
|= "error"
"error"

# 正しい - 必ず {} で始める
{job="app"} |= "error"
```

### 間違い3: ANDの使用

```
# 間違い
{job="app"} AND {level="error"}
{job="app"} |= "error" AND |= "timeout"

# 正しい - 複数ラベルはカンマ、複数フィルタは連結
{job="app", level="error"}
{job="app"} |= "error" |= "timeout"
```

### 間違い4: 時間範囲をクエリに含める

```
# 間違い - 時間はAPIパラメータで指定
{job="app"} |= "error" | timestamp >= "2024-01-01"
{job="app", time > "2024-01-01"}

# 正しい - 時間範囲はクエリ外（API側）で指定
{job="app"} |= "error"
```

### 間違い5: シングルクォートの混在

```
# 非推奨
{job='app'}

# 推奨 - ダブルクォートを使用
{job="app"}
```

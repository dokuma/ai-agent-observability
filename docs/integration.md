# 外部 AI UI からの接続ガイド

本システムの API (`/api/v1/*`) を Open WebUI などの AI チャット UI から利用する方法。

## 方法一覧

| 方式 | 難易度 | 特徴 |
|------|--------|------|
| **Open WebUI Pipe Function** | 低 | チャット UI から直接呼べる。推奨 |
| **Open WebUI MCP 統合** | 中 | MCP サーバとして接続。v0.6.31+ |
| **Open WebUI Tool** | 中 | LLM が判断してツールとして呼び出す |
| **curl / HTTP クライアント** | — | 任意のクライアントから直接呼べる |

---

## 1. Open WebUI Pipe Function (推奨)

Open WebUI のサイドバーにカスタムモデルとして表示され、チャットで直接使える。

### 設定手順

1. Open WebUI の **Workspace > Functions > +** で新規作成
2. 以下の Python コードを貼り付け
3. Valves (設定) で `API_BASE_URL` を調整

### コード

[`integration/pipe_function.py`](../integration/pipe_function.py) を参照。

Open WebUI の **Workspace > Functions > +** でこのファイルの内容を貼り付ける。

> **Note (v0.10.0)**:
> - Search-First + 自動調査移行: レポート検索で `[NEEDS_INVESTIGATION]` マーカーが検出された場合、過去レポートの部分回答を保持しつつ自動的に新規調査を開始
> - `followup_investigation_id` によるシームレスな調査ポーリング遷移
>
> **Note (v0.9.0)**:
> - report_search をバックグラウンドタスク化: LLM回答生成の同期awaitを廃止し、ポーリングで結果を取得する方式に変更。Open WebUIのpipe関数タイムアウトによる`{}`表示を解消
> - ポーリング中に `report_search_answer` を検出して即座に回答を返す
>
> **Note (v0.8.0)**:
> - 調査キャンセル対応: ユーザが「中止」「キャンセル」「停止」「cancel」「stop」と入力すると実行中の調査をキャンセル
> - Search-First 対応: バックエンド側で LLM インテント分類を廃止し、まず検索 → 不足なら調査のフローに変更
>
> **Note (v0.7.0)**:
> - `stage_detail` 対応: ポーリング中にサブエージェント内のReActステップ（ツール名、推論/要約フェーズ）をリアルタイム表示
> - `report_search` 即時完了対応（v0.9.0でバックグラウンド化に置き換え）
>
> **Note (v0.6.0)**:
> - `waiting_for_input` (interrupt) 対応: Orchestrator がユーザ入力を要求した場合、チャットメッセージとして問い合わせを表示し、次の発話で自動再開
> - 調査開始前に `GET /health` でMCPヘルスチェックを実行し、ステータスバーに表示（例: `MCP: ✅ prometheus / ❌ loki / ✅ grafana / ✅ kubernetes`）
> - ポーリング中も `InvestigationStatus.mcp_status` からMCP状態を参照可能
> - `AsyncGenerator` (yield) の代わりに `__event_emitter__` を使用し、[UIスタック問題](https://github.com/open-webui/open-webui/issues/20196) を回避
> - `__task__` パラメータでタイトル生成等のバックグラウンドタスクをスキップし、[重複実行問題](https://github.com/open-webui/open-webui/discussions/11309) を回避
> - 進捗はステータスバーに表示され、最終結果はチャットに表示されます

### 使い方

Open WebUI のモデル選択で **System Monitoring Agent** を選び、チャットで質問するだけ:

```
直近1時間でCPU使用率が高いインスタンスを調べてください
```

### Docker Compose での接続

Open WebUI と本システムを同じ Docker ネットワークに置く場合:

```yaml
# docker-compose.yaml に追加
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3080:8080"
    environment:
      - OLLAMA_BASE_URL=http://ollama:11434
    networks:
      - monitoring
```

Valves の `API_BASE_URL` を `http://agent:8000/api/v1` に設定する。

---

## 2. Open WebUI MCP 統合

本システムの MCP サーバ群を Open WebUI に直接登録する方法。
LLM がチャット中に Prometheus / Loki / Grafana のツールを直接呼び出せるようになる。

### 設定手順

1. Open WebUI v0.6.31+ を使用
2. **Admin Settings > External Tools > + (Add Server)**
3. 各 MCP サーバを登録:

| Name | URL | Type |
|------|-----|------|
| Prometheus MCP | `http://prometheus-mcp:9090` | MCP (Streamable HTTP) |
| Loki MCP | `http://loki-mcp:8080` | MCP (Streamable HTTP) |
| Grafana MCP | `http://grafana-mcp:8080` | MCP (Streamable HTTP) |
| Kubernetes MCP | `http://kubernetes-mcp:8080` | MCP (SSE) |

> **注意:** この方式は Orchestrator Agent を経由せず、LLM が直接各ツールを呼ぶ。
> 自律的な調査ワークフロー (計画→調査→RCA) が不要な場合に適している。

---

## 3. Open WebUI Tool

LLM が会話の文脈に応じて本システムの API をツールとして呼び出す方式。

### 設定手順

1. **Workspace > Tools > +** で新規作成
2. 以下のコードを登録

```python
"""
title: System Investigation
description: システム監視 AI Agent に調査を依頼する
version: 0.1.0
"""

import time

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        API_BASE_URL: str = Field(default="http://agent:8000/api/v1")

    def __init__(self):
        self.valves = self.Valves()

    def investigate_system(self, query: str) -> str:
        """
        システムの異常を調査する。CPU、メモリ、ディスク、ネットワーク等の
        問題について自然言語で質問すると、AI Agent が Prometheus / Loki から
        データを取得し根本原因分析レポートを返す。

        :param query: 調査内容を自然言語で記述
        :return: RCA レポート (Markdown)
        """
        base = self.valves.API_BASE_URL.rstrip("/")

        res = requests.post(
            f"{base}/query", json={"query": query}, timeout=30
        )
        res.raise_for_status()
        inv_id = res.json()["investigation_id"]

        for _ in range(60):
            time.sleep(5)
            s = requests.get(
                f"{base}/investigations/{inv_id}", timeout=10
            ).json()
            if s["status"] == "completed":
                report = requests.get(
                    f"{base}/investigations/{inv_id}/report", timeout=10
                ).json()
                return report.get("markdown", "レポートなし")
            if s["status"] == "failed":
                return f"調査失敗 (ID: {inv_id})"

        return f"タイムアウト (ID: {inv_id})"
```

任意のモデルで会話中に「サーバーの状態を確認して」と言うと、LLM がこのツールを呼び出す。

---

## 4. curl / HTTP クライアント

```bash
# ヘルスチェック
curl http://localhost:8000/api/v1/health

# クエリ送信
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "直近1時間のCPU使用率を調査"}'

# ステータス確認
curl http://localhost:8000/api/v1/investigations/{id}

# レポート取得
curl http://localhost:8000/api/v1/investigations/{id}/report
```

## API エンドポイント一覧

| Method | Path | 説明 |
|--------|------|------|
| GET | `/api/v1/health` | ヘルスチェック |
| POST | `/api/v1/query` | 自然言語クエリで調査開始 |
| POST | `/api/v1/webhook/alertmanager` | AlertManager Webhook |
| GET | `/api/v1/investigations/{id}` | 調査ステータス取得 |
| POST | `/api/v1/investigations/{id}/input` | 中断した調査にユーザ入力を送信 |
| POST | `/api/v1/investigations/{id}/cancel` | 実行中の調査をキャンセル |
| GET | `/api/v1/investigations/{id}/report` | RCA レポート取得 |
| GET | `/docs` | OpenAPI (Swagger UI) |

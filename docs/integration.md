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

```python
"""
title: AI Agent Monitoring
description: システム監視 AI Agent にクエリを送信し RCA レポートを取得する
version: 0.7.0

Note:
    - Open WebUI v0.6.43+ では AsyncGenerator を返すとUIがスタックする
      既知の問題があるため、__event_emitter__ で進捗を通知し、
      最終結果は文字列で返す方式を採用。
      https://github.com/open-webui/open-webui/issues/20196
    - __task__ パラメータでタイトル生成等のバックグラウンドタスクを
      スキップし、不要な重複実行を防止。
      https://github.com/open-webui/open-webui/discussions/11309
    - v0.6.0: waiting_for_input (interrupt) に対応。Orchestrator が
      ユーザ入力を要求した場合（データソース選択、時間範囲指定等）、
      チャットメッセージとして問い合わせを表示し、次のユーザ発話で
      自動的に調査を再開する。
"""

import asyncio
from typing import Awaitable, Callable, Optional

import requests
from pydantic import BaseModel, Field


class Pipe:
    """Open WebUI Pipe Function for AI Agent Monitoring.

    __event_emitter__ を使用して進捗をステータスバーに表示し、
    最終結果は文字列として返す。これによりUIがスタックする問題を回避。
    """

    class Valves(BaseModel):
        API_BASE_URL: str = Field(
            default="http://agent:8000/api/v1",
            description="AI Agent Monitoring API のベース URL",
        )
        POLL_INTERVAL: int = Field(
            default=3, description="ポーリング間隔 (秒)"
        )
        POLL_TIMEOUT: int = Field(
            default=300, description="ポーリングタイムアウト (秒)"
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "agent-monitoring", "name": "System Monitoring Agent"}]

    # Investigation ID を埋め込むマーカー（アシスタントメッセージに含める）
    _RESUME_MARKER = "<!-- investigation_id:"

    async def _emit_status(
        self,
        emitter: Optional[Callable[[dict], Awaitable[None]]],
        description: str,
        done: bool = False,
    ) -> None:
        """ステータスイベントを送信."""
        if emitter:
            await emitter({
                "type": "status",
                "data": {"description": description, "done": done},
            })

    def _format_input_request(self, inv_id: str, pending_input: Optional[dict]) -> str:
        """interrupt 時のユーザ入力要求をチャットメッセージとして整形.

        返却されるメッセージには非表示の investigation_id マーカーを含み、
        次のユーザ発話で自動的に resume できるようにする。
        """
        marker = f"{self._RESUME_MARKER} {inv_id} -->"

        if not pending_input:
            return (
                f"調査を続行するために入力が必要です。\n\n"
                f"回答をチャットに入力してください。\n\n{marker}"
            )

        input_type = pending_input.get("type", "")
        message = pending_input.get("message", "")

        if input_type == "datasource_selection":
            # データソース選択
            options = pending_input.get("options", [])
            lines = [message, ""]
            for i, opt in enumerate(options, 1):
                name = opt.get("name", opt.get("uid", f"選択肢{i}"))
                uid = opt.get("uid", "")
                recommended = " ⭐" if opt.get("recommended") else ""
                lines.append(f"  {i}. **{name}** (`{uid}`){recommended}")
            lines.append("")
            lines.append("番号または名前で回答してください。")
            lines.append(f"\n{marker}")
            return "\n".join(lines)
        elif input_type == "datasource_retry":
            # API失敗時のデータソース再選択
            error = pending_input.get("error", "")
            options = pending_input.get("options", [])
            lines = [message, ""]
            if error:
                lines.append(f"> エラー: `{error}`")
                lines.append("")
            for i, opt in enumerate(options, 1):
                name = opt.get("name", opt.get("uid", f"選択肢{i}"))
                uid = opt.get("uid", "")
                lines.append(f"  {i}. **{name}** (`{uid}`)")
            lines.append("")
            lines.append("番号または名前で回答してください。")
            lines.append(f"\n{marker}")
            return "\n".join(lines)
        else:
            # 汎用（時間範囲指定など、valueが文字列の場合）
            prompt = pending_input if isinstance(pending_input, str) else message
            return f"{prompt}\n\n{marker}"

    async def _try_resume(
        self,
        body: dict,
        emitter: Optional[Callable[[dict], Awaitable[None]]],
    ) -> Optional[str]:
        """直前の応答が waiting_for_input なら、ユーザ入力で resume を試行.

        Returns:
            resume 後の最終結果文字列。resume 対象でなければ None。
        """
        messages = body.get("messages", [])
        if len(messages) < 2:
            return None

        # 直前のアシスタントメッセージを検索
        assistant_msg = None
        for msg in reversed(messages[:-1]):
            if msg.get("role") == "assistant":
                assistant_msg = msg
                break

        if not assistant_msg:
            return None

        content = assistant_msg.get("content", "")
        if self._RESUME_MARKER not in content:
            return None

        # investigation_id を抽出
        try:
            start = content.index(self._RESUME_MARKER) + len(self._RESUME_MARKER)
            end = content.index("-->", start)
            inv_id = content[start:end].strip()
        except ValueError:
            return None

        user_input = messages[-1].get("content", "")
        base = self.valves.API_BASE_URL.rstrip("/")

        await self._emit_status(emitter, f"🔄 調査を再開中... (ID: {inv_id})")

        # ユーザ入力を送信して resume
        try:
            res = requests.post(
                f"{base}/investigations/{inv_id}/input",
                json={"value": user_input},
                timeout=30,
            )
            if res.status_code == 409:
                # 既に running/completed — 通常の新規クエリとして扱う
                return None
            res.raise_for_status()
        except Exception as e:
            await self._emit_status(emitter, f"❌ 再開失敗: {e}", done=True)
            return f"❌ 調査の再開に失敗しました: {e}"

        # resume 後のポーリング（同じパターン）
        return await self._poll_until_done(inv_id, base, emitter)

    async def pipe(
        self,
        body: dict,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __task__: Optional[str] = None,
    ) -> str:
        """調査を実行しレポートを返す.

        Args:
            body: リクエストボディ（messages を含む）
            __event_emitter__: Open WebUI のイベントエミッター（進捗通知用）
            __task__: Open WebUI のタスク種別（title_generation等）

        Returns:
            RCA レポート（Markdown形式）または エラーメッセージ
        """
        # タイトル生成等のバックグラウンドタスクはスキップ
        # これにより不要な重複実行を防止
        if __task__ is not None:
            return ""

        messages = body.get("messages", [])
        if not messages:
            return "クエリを入力してください。"

        # waiting_for_input で中断した調査の再開チェック
        # 直前のアシスタントメッセージに investigation_id が含まれている場合、
        # ユーザの最新メッセージを入力として resume する
        resumed = await self._try_resume(body, __event_emitter__)
        if resumed is not None:
            return resumed

        query = messages[-1].get("content", "")
        base = self.valves.API_BASE_URL.rstrip("/")

        # 0. MCP ヘルスチェック — 調査開始前にバックエンドの状態を確認
        await self._emit_status(__event_emitter__, "🩺 MCP ヘルスチェック中...")
        try:
            health_res = requests.get(f"{base}/health", timeout=10)
            if health_res.status_code == 200:
                health = health_res.json()
                mcp_parts = []
                for name, ok in health.get("mcp_servers", {}).items():
                    mcp_parts.append(f"{'✅' if ok else '❌'} {name}")
                if mcp_parts:
                    mcp_line = "MCP: " + " / ".join(mcp_parts)
                    await self._emit_status(__event_emitter__, mcp_line)
        except Exception:
            pass  # ヘルスチェック失敗は調査を妨げない

        # 1. 調査開始
        await self._emit_status(__event_emitter__, "🔍 調査を開始中...")

        try:
            res = requests.post(
                f"{base}/query",
                json={"query": query},
                timeout=30,
            )
            res.raise_for_status()
        except Exception as e:
            await self._emit_status(__event_emitter__, f"❌ エラー: {e}", done=True)
            return f"❌ 調査の開始に失敗しました: {e}"

        data = res.json()
        inv_id = data["investigation_id"]

        # report_search で即時完了した場合はポーリング不要
        if data.get("status") == "completed" and data.get("report_search_answer"):
            await self._emit_status(
                __event_emitter__, "✅ 過去レポートから回答", done=True
            )
            return data["report_search_answer"]

        await self._emit_status(__event_emitter__, f"🔍 調査中... (ID: {inv_id})")

        # 2. 完了までポーリング
        return await self._poll_until_done(inv_id, base, __event_emitter__)

    async def _poll_until_done(
        self,
        inv_id: str,
        base: str,
        emitter: Optional[Callable[[dict], Awaitable[None]]],
    ) -> str:
        """調査完了までポーリングし、レポートまたはユーザ入力要求を返す."""
        elapsed = 0
        last_stage = ""
        status = {}

        while elapsed < self.valves.POLL_TIMEOUT:
            await asyncio.sleep(self.valves.POLL_INTERVAL)
            elapsed += self.valves.POLL_INTERVAL

            try:
                status_res = requests.get(
                    f"{base}/investigations/{inv_id}", timeout=10
                )
                status = status_res.json()
            except Exception:
                continue  # 一時的な通信エラーは無視

            # ステージまたは詳細が変わったらステータスバーを更新
            current_stage = status.get("current_stage", "")
            stage_detail = status.get("stage_detail", "")
            stage_key = f"{current_stage}|{stage_detail}"
            if current_stage and stage_key != last_stage:
                iteration = status.get("iteration_count", 0)
                if iteration > 0:
                    status_msg = f"⏳ {current_stage} (イテレーション {iteration})"
                else:
                    status_msg = f"⏳ {current_stage}"
                if stage_detail:
                    status_msg += f" — {stage_detail}"
                # MCP 状態も表示（ポーリングレスポンスに含まれる場合）
                mcp_st = status.get("mcp_status", {})
                if mcp_st:
                    parts = [f"{'✅' if v else '❌'} {k}" for k, v in mcp_st.items()]
                    status_msg += f" | MCP: {' / '.join(parts)}"
                await self._emit_status(emitter, status_msg)
                last_stage = stage_key

            if status.get("status") == "completed":
                await self._emit_status(
                    emitter, "✅ 調査完了。レポート取得中..."
                )
                break

            if status.get("status") == "failed":
                error_msg = status.get("error", "不明なエラー")
                await self._emit_status(
                    emitter, f"❌ 調査失敗: {error_msg}", done=True
                )
                return f"❌ 調査が失敗しました: {error_msg}\n\n(ID: {inv_id})"

            if status.get("status") == "waiting_for_input":
                # Orchestrator がユーザ入力を要求（データソース選択、時間範囲等）
                await self._emit_status(
                    emitter, "⏸️ ユーザ入力を待機中", done=True
                )
                return self._format_input_request(inv_id, status.get("pending_input"))
        else:
            # タイムアウト
            await self._emit_status(
                emitter, "⏰ タイムアウト", done=True
            )
            return (
                f"⏰ ポーリングがタイムアウトしました (ID: {inv_id})\n\n"
                "調査はバックグラウンドで継続中の可能性があります。"
            )

        # レポート取得
        try:
            report_res = requests.get(
                f"{base}/investigations/{inv_id}/report", timeout=10
            )
        except Exception as e:
            await self._emit_status(
                emitter, "❌ レポート取得失敗", done=True
            )
            return f"❌ レポートの取得に失敗しました: {e}"

        # 完了通知
        await self._emit_status(emitter, "✅ 完了", done=True)

        # レポートが未生成 (404) の場合
        if report_res.status_code != 200:
            return (
                f"## 調査完了 ({inv_id})\n\n"
                f"調査は完了しましたが、詳細レポートを生成できませんでした。\n"
                f"イテレーション: {status.get('iteration_count', '不明')}\n\n"
                "*モデルの応答精度が十分でない可能性があります。"
                "より大きなモデル (llama3, qwen2.5:7b 等) の使用を推奨します。*"
            )

        report = report_res.json()
        marker = f"\n\n{self._RESUME_MARKER} {inv_id} -->"

        # Markdown レポートがあればそのまま返す
        if report.get("markdown"):
            return report["markdown"] + marker

        # フォールバック: 構造化データを整形
        lines = [f"## RCA レポート ({inv_id})\n"]
        for rc in report.get("root_causes", []):
            lines.append(
                f"- **{rc.get('category', '不明')}**: "
                f"{rc.get('description', '')} "
                f"(確信度: {rc.get('confidence', 0):.0%})"
            )
        if report.get("recommendations"):
            lines.append("\n### 推奨アクション")
            for r in report["recommendations"]:
                lines.append(f"- {r}")
        if len(lines) == 1:
            lines.append(
                "\n*レポートの内容が空です。"
                "より大きなモデルの使用を推奨します。*"
            )
        return "\n".join(lines) + marker
```

> **Note (v0.7.0)**:
> - `stage_detail` 対応: ポーリング中にサブエージェント内のReActステップ（ツール名、推論/要約フェーズ）をリアルタイム表示
> - `report_search` 即時完了対応: 過去レポートから回答可能な場合、ポーリングなしで即座に結果を返す
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
| GET | `/api/v1/investigations/{id}/report` | RCA レポート取得 |
| GET | `/docs` | OpenAPI (Swagger UI) |

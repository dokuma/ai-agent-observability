"""
title: AI Agent Monitoring
description: システム監視 AI Agent にクエリを送信し RCA レポートを取得する
version: 0.9.0

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
import re
from collections.abc import Awaitable, Callable

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
        POLL_INTERVAL: int = Field(default=3, description="ポーリング間隔 (秒)")
        POLL_TIMEOUT: int = Field(default=300, description="ポーリングタイムアウト (秒)")

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "agent-monitoring", "name": "System Monitoring Agent"}]

    # Investigation ID を埋め込むマーカー（アシスタントメッセージに含める）
    _RESUME_MARKER = "<!-- investigation_id:"

    # キャンセルキーワード
    _CANCEL_KEYWORDS = ["中止", "キャンセル", "停止", "cancel", "stop"]

    # フォローアップ検出（代名詞・指示語・詳細要求）
    _FOLLOWUP_INDICATORS = re.compile(r"その|それ|別の|他の|同じ|こちら|あの|上記|先ほど|さっき|もっと|詳しく|詳細")

    async def _emit_status(
        self,
        emitter: Callable[[dict], Awaitable[None]] | None,
        description: str,
        done: bool = False,
    ) -> None:
        """ステータスイベントを送信."""
        if emitter:
            await emitter(
                {
                    "type": "status",
                    "data": {"description": description, "done": done},
                }
            )

    def _format_input_request(self, inv_id: str, pending_input: dict | str | None) -> str:
        """interrupt 時のユーザ入力要求をチャットメッセージとして整形.

        返却されるメッセージには非表示の investigation_id マーカーを含み、
        次のユーザ発話で自動的に resume できるようにする。
        """
        marker = f"{self._RESUME_MARKER} {inv_id} -->"

        if not pending_input:
            return f"調査を続行するために入力が必要です。\n\n回答をチャットに入力してください。\n\n{marker}"

        # 文字列の場合は汎用テキスト入力として扱う（時間範囲指定など）
        if isinstance(pending_input, str):
            return f"{pending_input}\n\n{marker}"

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

    def _build_chat_context(self, messages: list) -> str:
        """チャット履歴からフォローアップ用コンテキストを構築.

        最新メッセージがフォローアップ的（指示語・代名詞を含む）場合、
        直近の会話ペアからコンテキストを生成する。

        Returns:
            コンテキスト文字列。フォローアップでなければ空文字。
        """
        if len(messages) < 2:
            return ""

        latest = messages[-1].get("content", "")
        if not self._FOLLOWUP_INDICATORS.search(latest):
            return ""

        # 直近3ペアの会話を収集（新しい順）
        pairs: list[str] = []
        history = messages[:-1]  # 最新メッセージを除く
        for msg in reversed(history):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue

            if role == "assistant":
                # RCAレポートやレポート検索回答の要約を抽出
                content = self._summarize_assistant_message(content)

            pairs.append(f"[{role}]: {content}")
            if len(pairs) >= 6:  # 3ペア = 6メッセージ
                break

        if not pairs:
            return ""

        pairs.reverse()
        return "\n".join(pairs)

    @staticmethod
    def _summarize_assistant_message(content: str) -> str:
        """アシスタントメッセージを要約（推奨事項を優先抽出）."""
        # investigation_id マーカーを除去
        cleaned = re.sub(r"<!-- investigation_id:.*?-->", "", content).strip()

        # 推奨事項セクションがあれば優先抽出
        rec_match = re.search(
            r"(##?\s*推奨事項.*?)(?=\n##?\s|\Z)",
            cleaned,
            re.DOTALL,
        )
        if rec_match and len(rec_match.group(1)) > 50:
            summary = rec_match.group(1).strip()
            if len(summary) > 1500:
                summary = summary[:1500] + "...(省略)"
            return summary

        # 長すぎるメッセージは先頭を抽出
        if len(cleaned) > 1000:
            cleaned = cleaned[:1000] + "...(省略)"

        return cleaned

    def _extract_inv_id_from_messages(self, messages: list) -> str | None:
        """メッセージ履歴から直近の investigation_id を抽出."""
        for msg in reversed(messages[:-1]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if self._RESUME_MARKER in content:
                    try:
                        start = content.index(self._RESUME_MARKER) + len(self._RESUME_MARKER)
                        end = content.index("-->", start)
                        return content[start:end].strip()
                    except ValueError:
                        pass
        return None

    async def _cancel_investigation(
        self,
        inv_id: str,
        emitter: Callable[[dict], Awaitable[None]] | None,
    ) -> str:
        """調査をキャンセルする."""
        base = self.valves.API_BASE_URL.rstrip("/")
        await self._emit_status(emitter, f"🛑 調査をキャンセル中... (ID: {inv_id})")
        try:
            res = requests.post(
                f"{base}/investigations/{inv_id}/cancel",
                timeout=10,
            )
            if res.status_code == 200:
                await self._emit_status(emitter, "🛑 調査をキャンセルしました", done=True)
                return f"🛑 調査をキャンセルしました (ID: {inv_id})"
            elif res.status_code == 409:
                await self._emit_status(emitter, "ℹ️ 調査は既に完了しています", done=True)
                return f"ℹ️ 調査は既に完了または停止しています (ID: {inv_id})"
            else:
                await self._emit_status(emitter, "❌ キャンセル失敗", done=True)
                return f"❌ キャンセルに失敗しました (status: {res.status_code})"
        except Exception as e:
            await self._emit_status(emitter, f"❌ キャンセル失敗: {e}", done=True)
            return f"❌ キャンセルに失敗しました: {e}"

    async def _try_resume(
        self,
        body: dict,
        emitter: Callable[[dict], Awaitable[None]] | None,
    ) -> str | None:
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
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __task__: str | None = None,
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

        # キャンセルキーワード検出
        user_msg = messages[-1].get("content", "").strip().lower()
        if any(kw in user_msg for kw in self._CANCEL_KEYWORDS):
            # 直前のアシスタントメッセージから investigation_id を抽出
            cancel_inv_id = self._extract_inv_id_from_messages(messages)
            if cancel_inv_id:
                return await self._cancel_investigation(cancel_inv_id, __event_emitter__)

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

        # 1. 調査開始（チャットコンテキスト付き）
        chat_context = self._build_chat_context(messages)
        await self._emit_status(__event_emitter__, "🔍 調査を開始中...")

        payload: dict = {"query": query}
        if chat_context:
            payload["chat_context"] = chat_context

        try:
            res = requests.post(
                f"{base}/query",
                json=payload,
                timeout=30,
            )
            res.raise_for_status()
        except Exception as e:
            await self._emit_status(__event_emitter__, f"❌ エラー: {e}", done=True)
            return f"❌ 調査の開始に失敗しました: {e}"

        data = res.json()
        inv_id = data["investigation_id"]

        routed_to = data.get("routed_to", "investigation")

        if routed_to == "report_search":
            await self._emit_status(__event_emitter__, "📚 過去のレポートを検索中...")
        else:
            await self._emit_status(__event_emitter__, f"🔍 調査中... (ID: {inv_id})")

        # 2. 完了までポーリング
        return await self._poll_until_done(inv_id, base, __event_emitter__)

    async def _poll_until_done(
        self,
        inv_id: str,
        base: str,
        emitter: Callable[[dict], Awaitable[None]] | None,
    ) -> str:
        """調査完了までポーリングし、レポートまたはユーザ入力要求を返す."""
        elapsed = 0
        last_stage = ""
        status = {}
        consecutive_errors = 0
        _MAX_CONSECUTIVE_ERRORS = 10

        while elapsed < self.valves.POLL_TIMEOUT:
            await asyncio.sleep(self.valves.POLL_INTERVAL)
            elapsed += self.valves.POLL_INTERVAL

            try:
                status_res = requests.get(f"{base}/investigations/{inv_id}", timeout=10)
                status_res.raise_for_status()
                status = status_res.json()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    await self._emit_status(emitter, f"❌ ポーリングエラー: {e}", done=True)
                    return f"❌ ポーリング中にエラーが続きました (ID: {inv_id}): {e}"
                continue

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
                # report_search → 調査自動移行の場合
                followup_id = status.get("followup_investigation_id")
                if followup_id:
                    rs_answer = status.get("report_search_answer", "")
                    prefix = ""
                    if rs_answer:
                        prefix = (
                            f"📋 **過去レポートからの情報:**\n{rs_answer}\n\n"
                            "---\n\n🔍 さらに詳しく調査を開始します...\n\n"
                        )
                    await self._emit_status(emitter, "🔍 過去レポートでは不十分なため調査を開始...")
                    # フォローアップ調査をポーリング
                    followup_result = await self._poll_until_done(followup_id, base, emitter)
                    return prefix + followup_result

                # report_search の場合は report_search_answer を直接返す
                rs_answer = status.get("report_search_answer")
                if rs_answer:
                    await self._emit_status(emitter, "✅ 過去レポートから回答", done=True)
                    return rs_answer
                await self._emit_status(emitter, "✅ 調査完了。レポート取得中...")
                break

            if status.get("status") == "failed":
                error_msg = status.get("error", "不明なエラー")
                await self._emit_status(emitter, f"❌ 調査失敗: {error_msg}", done=True)
                return f"❌ 調査が失敗しました: {error_msg}\n\n(ID: {inv_id})"

            if status.get("status") == "waiting_for_input":
                # Orchestrator がユーザ入力を要求（データソース選択、時間範囲等）
                await self._emit_status(emitter, "⏸️ ユーザ入力を待機中", done=True)
                return self._format_input_request(inv_id, status.get("pending_input"))
        else:
            # タイムアウト
            await self._emit_status(emitter, "⏰ タイムアウト", done=True)
            return (
                f"⏰ ポーリングがタイムアウトしました (ID: {inv_id})\n\n"
                "調査はバックグラウンドで継続中の可能性があります。"
            )

        # レポート取得
        try:
            report_res = requests.get(f"{base}/investigations/{inv_id}/report", timeout=10)
        except Exception as e:
            await self._emit_status(emitter, "❌ レポート取得失敗", done=True)
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
            lines.append("\n*レポートの内容が空です。より大きなモデルの使用を推奨します。*")
        return "\n".join(lines) + marker

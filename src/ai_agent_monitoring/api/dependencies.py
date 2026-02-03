"""API 依存注入 — アプリケーション全体の共有リソース管理."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ai_agent_monitoring.agents.orchestrator import OrchestratorAgent
from ai_agent_monitoring.core.config import Settings
from ai_agent_monitoring.core.models import RCAReport
from ai_agent_monitoring.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class InvestigationRecord:
    """調査の実行記録."""

    investigation_id: str
    status: str  # "running" | "completed" | "failed"
    trigger_type: str
    iteration_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error: str = ""
    rca_report: RCAReport | None = None


class AppState:
    """アプリケーション全体の共有ステート.

    FastAPI lifespan で初期化し、Depends で各ルーターに注入する。
    """

    def __init__(self) -> None:
        self.settings = Settings()
        self.registry: ToolRegistry | None = None
        self.orchestrator: OrchestratorAgent | None = None
        self.investigations: dict[str, InvestigationRecord] = {}

    async def initialize(self) -> None:
        """アプリケーション起動時の初期化."""
        # MCP クライアント
        self.registry = ToolRegistry.from_settings(self.settings)
        health = await self.registry.health_check()
        logger.info("MCP health check: %s", health)

        # LLM
        llm = ChatOpenAI(
            base_url=self.settings.llm_endpoint,
            model=self.settings.llm_model,
            api_key=SecretStr(self.settings.llm_api_key),
        )

        # Orchestrator（registryを渡してhealthy状態を考慮）
        self.orchestrator = OrchestratorAgent(
            llm=llm,
            registry=self.registry,
            settings=self.settings,
        )
        logger.info("Orchestrator Agent initialized")

    async def shutdown(self) -> None:
        """アプリケーション終了時のクリーンアップ."""
        logger.info("Shutting down application")

    def create_investigation(self, trigger_type: str) -> str:
        """新しい調査レコードを作成しIDを返す."""
        inv_id = uuid4().hex[:12]
        self.investigations[inv_id] = InvestigationRecord(
            investigation_id=inv_id,
            status="running",
            trigger_type=trigger_type,
        )
        return inv_id

    def get_investigation(self, inv_id: str) -> InvestigationRecord | None:
        """調査レコードを取得."""
        return self.investigations.get(inv_id)

    def complete_investigation(self, inv_id: str, rca_report: RCAReport | None = None) -> None:
        """調査を完了としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "completed"
            record.completed_at = datetime.now()
            record.rca_report = rca_report

    def fail_investigation(self, inv_id: str, error: str) -> None:
        """調査を失敗としてマーク."""
        record = self.investigations.get(inv_id)
        if record:
            record.status = "failed"
            record.completed_at = datetime.now()
            record.error = error


# シングルトンインスタンス
app_state = AppState()

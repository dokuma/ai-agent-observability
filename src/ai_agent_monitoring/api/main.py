"""FastAPI アプリケーションエントリポイント."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_agent_monitoring.api.dependencies import app_state
from ai_agent_monitoring.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """アプリケーションのライフサイクル管理."""
    logger.info("Starting AI Agent Monitoring System")
    await app_state.initialize()
    yield
    await app_state.shutdown()
    logger.info("AI Agent Monitoring System stopped")


app = FastAPI(
    title="AI Agent Monitoring System",
    description="AI Agentによる自律型システム監視 — 異常検知からRCAレポート生成まで",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")

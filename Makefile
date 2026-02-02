.PHONY: install dev test test-cov lint format run clean \
       docker-up docker-down docker-logs docker-ps \
       integration-up integration-down integration-test integration-clean

# ===========================================================
# ローカル開発
# ===========================================================
install:
	uv sync

dev:
	uv sync --all-extras

test:
	uv run pytest

test-cov:
	uv run pytest --cov=ai_agent_monitoring --cov-report=term-missing

lint:
	uv run ruff check src/ tests/
	uv run mypy src/

format:
	uv run ruff format src/ tests/

run:
	uv run uvicorn ai_agent_monitoring.api.main:app --reload --port 8000

clean:
	rm -rf .venv __pycache__ .pytest_cache .mypy_cache .coverage htmlcov

# ===========================================================
# Docker — 全サービス起動
# ===========================================================
docker-up:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-ps:
	docker compose ps

# ===========================================================
# 結合テスト環境
# ===========================================================
# インフラのみ起動（agent 除外）— テストはホストから実行
integration-up:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")
	docker compose up -d \
		ollama ollama-pull \
		prometheus loki promtail \
		grafana grafana-renderer dummy-app \
		prometheus-mcp loki-mcp grafana-mcp \
		langfuse-web langfuse-worker \
		langfuse-postgres langfuse-clickhouse langfuse-redis \
		langfuse-minio langfuse-minio-init
	@echo ""
	@echo "=== 結合テスト環境を起動中 ==="
	@echo "  Prometheus   : http://localhost:9090"
	@echo "  Loki         : http://localhost:3100"
	@echo "  Grafana      : http://localhost:3000  (admin/admin)"
	@echo "  Langfuse     : http://localhost:3001  (admin@example.com/admin)"
	@echo "  Ollama       : http://localhost:11434"
	@echo "  Prometheus MCP: http://localhost:9091"
	@echo "  Loki MCP     : http://localhost:9092"
	@echo "  Grafana MCP  : http://localhost:9093"
	@echo ""
	@echo "Ollama モデルのダウンロード完了を待ってからテストを実行してください。"
	@echo "  make integration-wait"
	@echo "  make integration-test"

integration-wait:
	@echo "Ollama モデルダウンロードの完了を確認中..."
	@timeout=120; while [ $$timeout -gt 0 ]; do \
		if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q 'qwen2.5:0.5b'; then \
			echo "✓ qwen2.5:0.5b モデル準備完了"; break; \
		fi; \
		echo "  待機中... (残り $${timeout}s)"; \
		sleep 5; timeout=$$((timeout - 5)); \
	done
	@echo "全サービスのヘルスチェック:"
	@curl -sf http://localhost:9090/-/healthy > /dev/null 2>&1 && echo "  ✓ Prometheus" || echo "  ✗ Prometheus"
	@curl -sf http://localhost:3100/ready > /dev/null 2>&1 && echo "  ✓ Loki" || echo "  ✗ Loki"
	@curl -sf http://localhost:3000/api/health > /dev/null 2>&1 && echo "  ✓ Grafana" || echo "  ✗ Grafana"
	@curl -sf http://localhost:11434/api/tags > /dev/null 2>&1 && echo "  ✓ Ollama" || echo "  ✗ Ollama"

integration-test:
	LLM_ENDPOINT=http://localhost:11434/v1 \
	LLM_MODEL=qwen2.5:0.5b \
	MCP_PROMETHEUS_URL=http://localhost:9091 \
	MCP_LOKI_URL=http://localhost:9092 \
	MCP_GRAFANA_URL=http://localhost:9093 \
	PROMETHEUS_URL=http://localhost:9090 \
	LOKI_URL=http://localhost:3100 \
	GRAFANA_URL=http://localhost:3000 \
	LANGFUSE_ENABLED=true \
	LANGFUSE_PUBLIC_KEY=pk-lf-dev \
	LANGFUSE_SECRET_KEY=sk-lf-dev \
	LANGFUSE_BASE_URL=http://localhost:3001 \
	uv run pytest tests/ -m integration -v

integration-down:
	docker compose down

integration-clean:
	docker compose down -v --remove-orphans
	@echo "全ボリュームを削除しました。"

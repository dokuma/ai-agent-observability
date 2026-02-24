"""RateLimitRetryWrapper のテスト."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import RateLimitError

from ai_agent_monitoring.core.llm_retry import RateLimitRetryWrapper


def _make_rate_limit_error() -> RateLimitError:
    """テスト用の RateLimitError を生成."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"retry-after": "1"}
    mock_response.json.return_value = {"error": {"message": "Rate limit exceeded"}}
    return RateLimitError(
        message="Rate limit exceeded",
        response=mock_response,
        body={"error": {"message": "Rate limit exceeded"}},
    )


class TestRateLimitRetryWrapper:
    """RateLimitRetryWrapper の基本テスト."""

    @pytest.mark.asyncio
    async def test_normal_response_no_retry(self):
        """正常応答時はリトライなし."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value="ok")

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        result = await wrapper.ainvoke(["hello"])

        assert result == "ok"
        assert mock_llm.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit_then_success(self):
        """RateLimitError 後に成功 → リトライされる."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            side_effect=[_make_rate_limit_error(), "ok"],
        )

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        result = await wrapper.ainvoke(["hello"])

        assert result == "ok"
        assert mock_llm.ainvoke.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        """全リトライ消費 → RateLimitError 伝播."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            side_effect=[_make_rate_limit_error() for _ in range(3)],
        )

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        with pytest.raises(RateLimitError):
            await wrapper.ainvoke(["hello"])

        assert mock_llm.ainvoke.call_count == 3

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_no_retry(self):
        """RateLimitError 以外の例外 → リトライしない."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=ValueError("bad input"))

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        with pytest.raises(ValueError, match="bad input"):
            await wrapper.ainvoke(["hello"])

        assert mock_llm.ainvoke.call_count == 1

    def test_bind_tools_returns_wrapper(self):
        """bind_tools() がラッパー付きで返される."""
        mock_llm = MagicMock()
        mock_bound = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        bound_wrapper = wrapper.bind_tools(["tool1"])

        assert isinstance(bound_wrapper, RateLimitRetryWrapper)
        assert bound_wrapper._llm is mock_bound
        mock_llm.bind_tools.assert_called_once_with(["tool1"])

    @pytest.mark.asyncio
    async def test_bind_tools_retry_works(self):
        """bind_tools() 後もリトライが有効."""
        mock_llm = MagicMock()
        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(
            side_effect=[_make_rate_limit_error(), "ok"],
        )
        mock_llm.bind_tools.return_value = mock_bound

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3, wait_min=0.01, wait_max=0.02)
        bound_wrapper = wrapper.bind_tools(["tool1"])
        result = await bound_wrapper.ainvoke(["hello"])

        assert result == "ok"
        assert mock_bound.ainvoke.call_count == 2

    def test_getattr_delegates(self):
        """__getattr__ による属性委譲."""
        mock_llm = MagicMock()
        mock_llm.model_name = "gpt-4"
        mock_llm.temperature = 0.7

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=3)

        assert wrapper.model_name == "gpt-4"
        assert wrapper.temperature == 0.7

    def test_bind_tools_preserves_config(self):
        """bind_tools() がリトライ設定を引き継ぐ."""
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = MagicMock()

        wrapper = RateLimitRetryWrapper(mock_llm, max_attempts=5, wait_min=10, wait_max=60)
        bound = wrapper.bind_tools(["tool1"])

        assert bound._max_attempts == 5
        assert bound._wait_min == 10
        assert bound._wait_max == 60

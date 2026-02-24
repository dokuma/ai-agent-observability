"""LLM RateLimit リトライラッパー.

OpenAI SDK の内蔵リトライが全て失敗した後の RateLimitError に対して、
tenacity による追加リトライを提供する。
"""

from __future__ import annotations

import logging
from typing import Any

from openai import RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class RateLimitRetryWrapper:
    """ChatOpenAI を包み、RateLimitError 時にリトライする透過的ラッパー.

    - ainvoke(): tenacity リトライ付きで内部 LLM に委譲
    - bind_tools(): 内部 LLM に委譲し、結果を再度ラッパーで包む
    - __getattr__(): 未定義属性は内部 LLM に委譲（LangChain 互換性維持）
    """

    def __init__(
        self,
        llm: Any,
        *,
        max_attempts: int = 3,
        wait_min: float = 5,
        wait_max: float = 120,
    ) -> None:
        self._llm = llm
        self._max_attempts = max_attempts
        self._wait_min = wait_min
        self._wait_max = wait_max

        # tenacity リトライャーを動的に構築（インスタンスごとの設定を反映）
        self._retry_decorator = retry(
            retry=retry_if_exception_type(RateLimitError),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=wait_min, max=wait_max),
            reraise=True,
            before_sleep=lambda retry_state: logger.warning(
                "LLM RateLimit retry attempt %d/%d: %s",
                retry_state.attempt_number,
                max_attempts,
                retry_state.outcome.exception() if retry_state.outcome else "unknown",
            ),
        )

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        """内部 LLM の ainvoke をリトライ付きで呼び出す."""

        @self._retry_decorator
        async def _invoke_with_retry() -> Any:
            return await self._llm.ainvoke(*args, **kwargs)

        return await _invoke_with_retry()

    def bind_tools(self, *args: Any, **kwargs: Any) -> RateLimitRetryWrapper:
        """内部 LLM の bind_tools に委譲し、結果をラッパーで再度包む."""
        bound = self._llm.bind_tools(*args, **kwargs)
        return RateLimitRetryWrapper(
            bound,
            max_attempts=self._max_attempts,
            wait_min=self._wait_min,
            wait_max=self._wait_max,
        )

    def __getattr__(self, name: str) -> Any:
        """未定義属性は内部 LLM に委譲."""
        return getattr(self._llm, name)

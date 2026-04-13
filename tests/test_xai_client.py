"""Tests for xai_client retry behaviour."""

from __future__ import annotations

import io
import json
from http.client import HTTPResponse
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

import pytest

from xai_client import ChatCompletionResult, _MAX_RETRIES, chat_completion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_BODY = {
    "choices": [
        {"message": {"role": "assistant", "content": "Hello!", "tool_calls": None}}
    ]
}
_GOOD_BODY_BYTES = json.dumps(_GOOD_BODY).encode()


def _http_error(code: int, body: bytes = b"error") -> HTTPError:
    """Build a minimal HTTPError with a readable fp."""
    return HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(body))  # type: ignore[arg-type]


def _url_open_ok(_req: object, *, timeout: float, context: object) -> MagicMock:
    """Return a context-manager mock that yields a good response."""
    resp = MagicMock()
    resp.read.return_value = _GOOD_BODY_BYTES
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChatCompletionRetry:
    def _call(self) -> ChatCompletionResult:
        return chat_completion("key", "model", [], [])

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=_url_open_ok)
    def test_success_no_retries(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        result = self._call()
        assert result.content == "Hello!"
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_503_then_succeeds(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        """A 503 on attempt 0 should trigger a retry; attempt 1 succeeds."""
        resp = MagicMock()
        resp.read.return_value = _GOOD_BODY_BYTES
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.side_effect = [_http_error(503), resp]

        result = self._call()
        assert result.content == "Hello!"
        assert mock_open.call_count == 2
        mock_sleep.assert_called_once()

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_url_error_then_succeeds(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = _GOOD_BODY_BYTES
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.side_effect = [URLError("conn reset"), resp]

        result = self._call()
        assert result.content == "Hello!"
        assert mock_open.call_count == 2

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=_http_error(500))
    def test_raises_after_max_retries_http(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        """All _MAX_RETRIES attempts exhausted for HTTP 500 → RuntimeError."""
        with pytest.raises(RuntimeError, match="xAI HTTP 500"):
            self._call()
        assert mock_open.call_count == _MAX_RETRIES

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=URLError("network down"))
    def test_raises_after_max_retries_url_error(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        with pytest.raises(RuntimeError, match="xAI connection error"):
            self._call()
        assert mock_open.call_count == _MAX_RETRIES

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=_http_error(401, b"unauthorized"))
    def test_non_retryable_401_raises_immediately(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        """A 401 is not retryable — should raise on the first attempt."""
        with pytest.raises(RuntimeError, match="xAI HTTP 401"):
            self._call()
        assert mock_open.call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=_http_error(429))
    def test_429_is_retried(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        """429 (rate limit) should be treated as retryable."""
        with pytest.raises(RuntimeError, match="xAI HTTP 429"):
            self._call()
        assert mock_open.call_count == _MAX_RETRIES

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_exponential_backoff_delays(self, mock_open: MagicMock, mock_sleep: MagicMock) -> None:
        """Sleep delays should follow 1s, 2s, ... (base * 2^attempt) pattern."""
        resp = MagicMock()
        resp.read.return_value = _GOOD_BODY_BYTES
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        # Fail twice, succeed on third
        mock_open.side_effect = [_http_error(503), _http_error(503), resp]
        self._call()
        assert mock_sleep.call_count == 2
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[1] == delays[0] * 2  # doubling

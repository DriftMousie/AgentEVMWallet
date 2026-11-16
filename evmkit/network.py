"""Bounded JSON transport shared by optional read-only analysis sources."""
import json
import time
import requests
from .common import ToolError, require


class Budget:
    def __init__(self, seconds=60, requests_limit=100):
        self.deadline = time.monotonic() + seconds
        self.requests_limit = requests_limit
        self.requests = 0

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ToolError("ANALYSIS_LIMIT", "Analysis time budget exhausted")
        return left

    def spend(self):
        if self.requests >= self.requests_limit:
            raise ToolError("ANALYSIS_LIMIT", "Analysis request budget exhausted")
        self.requests += 1
        return self.remaining()


def json_request(method, url, *, timeout=10, params=None, payload=None, max_bytes=20*1024*1024):
    try:
        deadline = time.monotonic()+timeout
        with requests.request(method, url, params=params, json=payload, timeout=timeout,
                              stream=True, allow_redirects=False) as response:
            if response.status_code == 429:
                raise ToolError("SOURCE_RATE_LIMITED", "Optional data source rate limited", retryable=True)
            if response.status_code == 404:
                raise ToolError("SOURCE_NOT_FOUND", "Optional data source has no record", retryable=True)
            require(response.status_code == 200, "Optional data source request failed", "SOURCE_UNAVAILABLE")
            parts, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                require(size <= max_bytes, "Optional source response exceeds size limit", "ANALYSIS_LIMIT")
                require(time.monotonic() < deadline, "Optional source response timed out", "ANALYSIS_LIMIT")
                parts.append(chunk)
            return json.loads(b"".join(parts))
    except ToolError:
        raise
    except Exception:
        # Requests exceptions can include API keys, full URLs and response bodies.
        raise ToolError("SOURCE_UNAVAILABLE", "Optional data source unavailable or returned invalid JSON", retryable=True) from None

"""Bounded JSON transport shared by optional read-only analysis sources."""
import json
import time
import requests
from .common import ToolError, require


def provider_error_code(data):
    if not isinstance(data, dict) or not (data.get("status") == "0" or data.get("error")):
        return None
    message = str(data.get("error") or data.get("result") or data.get("message", "")).lower()
    if any(t in message for t in ("rate limit", "too many requests", "quota")): return "SOURCE_RATE_LIMITED"
    if any(t in message for t in ("api key", "unauthor", "forbidden", "not allowed")): return "SOURCE_UNAUTHORIZED"
    return "SOURCE_UNAVAILABLE"


class Budget:
    def __init__(self, seconds=60, requests_limit=300):
        self.deadline = time.monotonic() + seconds
        self.requests_limit = requests_limit
        self.requests = 0
        self.phase = "other"
        self.by_stage = {}
        self.validated_rpcs = set()
        self.reserve_requests = 0
        self.rpc_calls = 0
        self.rpc_by_method = {}
        self.batch_disabled = set()

    def record_rpc(self, method, count=1):
        self.rpc_calls += count
        self.rpc_by_method[method] = self.rpc_by_method.get(method,0) + count

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ToolError("ANALYSIS_LIMIT", "Analysis time budget exhausted")
        return left

    def spend(self, stage=None):
        self.remaining()
        if self.requests >= self.requests_limit - self.reserve_requests:
            raise ToolError("ANALYSIS_LIMIT", "Analysis request budget exhausted")
        self.requests += 1
        category = stage or self.phase
        self.by_stage[category] = self.by_stage.get(category, 0) + 1
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
            if response.status_code in (401, 403):
                raise ToolError("SOURCE_UNAUTHORIZED", "Optional data source requires different credentials or permissions")
            if response.status_code in (400,405,413) and isinstance(payload,list):
                raise ToolError("CAPABILITY_UNAVAILABLE","RPC batch transport rejected; individual reads may still work")
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
    except requests.exceptions.Timeout:
        raise ToolError("SOURCE_TIMEOUT", "Optional data source timed out", retryable=True) from None
    except Exception:
        # Requests exceptions can include API keys, full URLs and response bodies.
        raise ToolError("SOURCE_UNAVAILABLE", "Optional data source unavailable or returned invalid JSON", retryable=True) from None

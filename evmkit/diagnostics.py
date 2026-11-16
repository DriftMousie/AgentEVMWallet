"""Stable, redacted optional-capability diagnostics, separate from core failures."""
REASONS = {
    "SOURCE_UNCONFIGURED": "not_configured", "SOURCE_NOT_FOUND": "not_found",
    "CAPABILITY_UNAVAILABLE": "unsupported", "SOURCE_RATE_LIMITED": "rate_limited",
    "SOURCE_UNAUTHORIZED": "unauthorized", "SOURCE_TIMEOUT": "timeout",
    "HISTORY_UNAVAILABLE": "history_unavailable", "OPTIONAL_RPC_REJECTED": "rejected",
    "SOURCE_UNAVAILABLE": "unavailable", "ANALYSIS_LIMIT": "budget_exhausted",
}
CAPABILITIES = {"CALL_TRACE": "internal_calls", "INTERNAL_CALLS": "internal_calls",
                "INTERNALS": "internal_calls", "STATE_DIFF": "native_state_diff",
                "METADATA": "identity", "COUNTERPART": "identity", "SENDER": "identity",
                "COUNTERPART_CODE": "identity", "COUNTERPART_PROXY": "identity", "ACTIVITY": "activity",
                "ABI": "abi", "ABI_CURRENT_CODE": "abi", "ABI_HISTORY_CHECK": "abi_history",
                "SIGNATURES": "selector_lookup", "CACHE": "cache", "CACHE_UNAVAILABLE": "cache",
                "TOKEN_METADATA_PARTIAL": "token_metadata", "CALL_TRACE_TRUNCATED": "internal_calls",
                "BLOCK_PRE_BALANCE": "block_balances", "BLOCK_POST_BALANCE": "block_balances",
                "BALANCE_ADDRESSES_TRUNCATED": "block_balances", "BALANCE_TOKENS_TRUNCATED": "block_balances",
                "BLOCK_BALANCES": "block_balances", "POOL_ASSETS": "pool_assets", "ROUTE_AMOUNTS": "route_amounts",
                "TOKEN_METADATA":"token_metadata"}
SPECIAL_REASONS = {"TOKEN_METADATA_PARTIAL": "incomplete", "CALL_TRACE_TRUNCATED": "truncated",
                   "BALANCE_ADDRESSES_TRUNCATED": "truncated", "BALANCE_TOKENS_TRUNCATED": "truncated",
                   "CACHE_UNAVAILABLE": "unavailable"}


def missing(code, source="rpc", capability=None):
    stage, sep, suffix = code.partition(":")
    reason = suffix if sep else code
    for prefix in ("BLOCKSCOUT", "ETHERSCAN", "SOURCIFY", "FOURBYTE"):
        if stage.startswith(prefix):
            source = prefix.lower()
            stage = stage[len(prefix):].lstrip("_")
            if not sep:
                for known in sorted(REASONS, key=len, reverse=True):
                    if stage.endswith(known):
                        reason = known; stage = stage[:-len(known)].rstrip("_"); break
            break
    capability = capability or CAPABILITIES.get(stage, stage.lower() or ("selector_lookup" if source == "fourbyte" else "abi"))
    reason = SPECIAL_REASONS.get(code, REASONS.get(reason, reason.lower()))
    return {"capability": capability, "source": source, "reason": reason,
            "message": f"Optional {capability} from {source}: {reason}"}


def combine(*groups):
    records = {}
    for group in groups:
        for record in group:
            records[(record["capability"], record["source"], record["reason"])] = record
    return list(records.values())

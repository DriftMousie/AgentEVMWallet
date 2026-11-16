from __future__ import annotations

import time

from web3 import Web3, HTTPProvider
from web3.exceptions import TransactionNotFound, ContractLogicError, Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from .common import ToolError, require


def optional_error_code(error):
    """Classify internally; never expose provider messages containing credentials."""
    code, message = error.get("code"), str(error.get("message", "")).lower()
    if code == -32601: return "CAPABILITY_UNAVAILABLE"
    if code in (401, 403) or any(s in message for s in ("unauthor", "forbidden", "api key", "access denied", "not allowed", "plan required")): return "SOURCE_UNAUTHORIZED"
    if code in (-32005, 429) or any(s in message for s in ("rate limit", "too many requests")): return "SOURCE_RATE_LIMITED"
    if any(s in message for s in ("timeout", "timed out", "deadline exceeded")): return "SOURCE_TIMEOUT"
    if any(s in message for s in ("missing trie", "historical state", "state is not available", "pruned", "reexec", "header not found")): return "HISTORY_UNAVAILABLE"
    if any(s in message for s in ("unknown tracer", "unsupported tracer", "tracer not found", "only support", "unrecognized tracer")) or code == -32602 and "tracer" in message:
        return "CAPABILITY_UNAVAILABLE"
    return "OPTIONAL_RPC_REJECTED"


class RPC:
    """Retry reads only. A send is deliberately a single RPC attempt."""
    def __init__(self, config, chain):
        self.chain = chain
        self.clients = []
        for endpoint in config.rpc_endpoints(chain["alias"]):
            w3 = Web3(HTTPProvider(endpoint["url"], request_kwargs={"timeout": 15}, exception_retry_configuration=None))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            self.clients.append((endpoint["name"], w3))
        require(self.clients, "No enabled RPC URL for this chain in CSV", "CONFIG_ERROR")
        self.active = None

    def read(self, fn):
        mismatch = False
        last_not_found = False
        for _ in range(2):
            for _, client in self.clients:
                try:
                    if client.eth.chain_id != self.chain["chain_id"]:
                        mismatch = True
                        continue
                    value = fn(client)
                    self.active = client
                    return value
                except TransactionNotFound:
                    last_not_found = True
                except (ContractLogicError, ValueError, Web3RPCError) as exc:
                    # Never echo RPC exceptions: providers may include URLs, requests or credentials.
                    response = getattr(exc, "rpc_response", None)
                    error = response.get("error", {}) if isinstance(response, dict) else {}
                    if isinstance(error, dict) and error.get("code") in (-32005, -32002, -32603, 429):
                        continue
                    raw = getattr(exc, "data", None)
                    if raw is None and isinstance(error, dict):
                        raw = error.get("data")
                    details = None
                    if isinstance(raw, str):
                        from .common import hexdata
                        try:
                            details = {"revert_data": hexdata(raw)}
                            from .abi import decode_revert
                            details.update(decode_revert(raw))
                        except ToolError:
                            pass
                    raise ToolError("CALL_REVERTED", "RPC rejected the call or contract execution reverted", data=details) from None
                except Exception:
                    continue
            time.sleep(0.1)
        if last_not_found:
            raise ToolError("NOT_FOUND", "Transaction or receipt not found", retryable=True)
        raise ToolError("CHAIN_ID_MISMATCH" if mismatch else "RPC_UNAVAILABLE",
                        "No reachable RPC with the expected Chain ID", retryable=not mismatch)

    def send(self, raw):
        # Pick a validated endpoint without accidentally retrying a write.
        self.read(lambda w: w.eth.block_number)
        try:
            return self.active.eth.send_raw_transaction(raw)
        except Exception:
            raise ToolError("BROADCAST_UNKNOWN", "Broadcast outcome unknown; query the recorded hash before replaying", retryable=False) from None

    def optional_request(self, method, params, budget, rpc_name=None):
        """Bounded raw RPC for enrichment; never accepts write methods."""
        allowed = {"eth_call", "eth_getCode", "eth_getBalance", "eth_getStorageAt",
                   "eth_getBlockByNumber", "eth_blockNumber", "debug_traceTransaction", "debug_traceCall"}
        require(method in allowed, "Analysis RPC method is not read-only", "INVALID_INPUT")
        from .network import json_request
        last_code = "SOURCE_UNAVAILABLE"
        for name, client in self.clients:
            if rpc_name is not None and name != rpc_name: continue
            try:
                url = client.provider.endpoint_uri
                validation_key = (id(self), name)
                if validation_key not in budget.validated_rpcs:
                    timeout=min(10,budget.spend("chain_validation"))
                    budget.record_rpc("eth_chainId")
                    response = json_request("POST", url, timeout=timeout,
                                            payload={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
                    if response.get("error"):
                        last_code = optional_error_code(response["error"])
                        continue
                    require(int(response.get("result", "0x0"), 16) == self.chain["chain_id"],
                            "Analysis RPC Chain ID mismatch", "CHAIN_ID_MISMATCH")
                    budget.validated_rpcs.add(validation_key)
                timeout=min(15,budget.spend())
                budget.record_rpc(method)
                response = json_request("POST", url, timeout=timeout,
                                        payload={"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
                if response.get("error"):
                    last_code = optional_error_code(response["error"])
                    continue
                require("result" in response and response["result"] is not None, "Optional RPC returned no result", "SOURCE_NOT_FOUND")
                return response["result"]
            except ToolError as exc:
                if exc.code == "ANALYSIS_LIMIT": raise
                last_code = exc.code
            except Exception:
                last_code = "SOURCE_UNAVAILABLE"
        raise ToolError(last_code, "Optional RPC capability unavailable; base transaction data is preserved")

    def optional_batch(self, calls, budget):
        """At most 20 independent eth_call operations; no Multicall semantics."""
        require(0 < len(calls) <= 20, "Read batch must contain 1..20 calls")
        require(all(method == "eth_call" and isinstance(params,list) and len(params)==2 for method,params in calls),
                "Only eth_call is allowed in metadata batches")
        from .network import json_request
        fallback_allowed=False
        last_code="SOURCE_UNAVAILABLE"
        for name, client in self.clients:
            if (id(self),name) in budget.batch_disabled:
                fallback_allowed=True;continue
            try:
                if (id(self),name) not in budget.validated_rpcs:
                    self.optional_request("eth_blockNumber",[],budget,rpc_name=name)
                timeout=min(15,budget.spend())
                budget.record_rpc("eth_call",len(calls))
                response = json_request("POST",client.provider.endpoint_uri,timeout=timeout,
                    payload=[{"jsonrpc":"2.0","id":i,"method":method,"params":params} for i,(method,params) in enumerate(calls)])
                require(isinstance(response,list) and len(response)==len(calls), "RPC batch unsupported", "CAPABILITY_UNAVAILABLE")
                by_id = {row.get("id"):row for row in response if isinstance(row,dict)}
                require(set(by_id)==set(range(len(calls))), "Invalid batch response IDs", "INVALID_RESPONSE")
                return [{"error":optional_error_code(by_id[i]["error"])} if by_id[i].get("error") else
                        {"result":by_id[i]["result"]} if by_id[i].get("result") is not None else {"error":"SOURCE_NOT_FOUND"}
                        for i in range(len(calls))]
            except ToolError as exc:
                if exc.code == "ANALYSIS_LIMIT": raise
                last_code=exc.code
                if exc.code in ("CAPABILITY_UNAVAILABLE","INVALID_RESPONSE"):
                    fallback_allowed=True;budget.batch_disabled.add((id(self),name))
            except Exception:
                last_code="INVALID_RESPONSE"
        if not fallback_allowed: return [{"error":last_code} for _ in calls]
        results = []
        for method, params in calls:
            try: results.append({"result":self.optional_request(method,params,budget)})
            except ToolError as exc:
                results.append({"error":exc.code})
                if exc.code == "ANALYSIS_LIMIT":
                    results.extend({"error":exc.code} for _ in range(len(calls)-len(results))); break
        return results

    def health(self):
        rows = []
        for name, w3 in self.clients:
            start = time.monotonic()
            try:
                actual = w3.eth.chain_id
                rows.append({"rpc_name": name, "chain_id": actual, "valid": actual == self.chain["chain_id"],
                             "block": w3.eth.block_number if actual == self.chain["chain_id"] else None,
                             "latency_ms": int((time.monotonic() - start) * 1000)})
            except Exception:
                rows.append({"rpc_name": name, "valid": False, "error": "RPC_UNAVAILABLE"})
        return rows

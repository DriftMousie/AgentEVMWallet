from __future__ import annotations

import time

from web3 import Web3, HTTPProvider
from web3.exceptions import TransactionNotFound, ContractLogicError, Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from .common import ToolError, require


def revert_bytes(raw,depth=0):
    if depth>4:return None
    if isinstance(raw,str):
        from .common import hexdata
        if raw.startswith("Reverted "):raw=raw[9:]
        try:return hexdata(raw) if len(raw)<=131074 else None
        except ToolError:return None
    if isinstance(raw,dict):
        for key,value in list(raw.items())[:20]:
            if key in ("data","return","result","originalError") or isinstance(key,str) and len(key)==66 and key.startswith("0x"):
                found=revert_bytes(value,depth+1)
                if found is not None:return found
    return None


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


def optional_failure(error):
    error=error if isinstance(error,dict) else {}
    out={"error":optional_error_code(error)}
    if error.get("code")==3 or "execution reverted" in str(error.get("message","")).lower():
        out["rpc_error_kind"]="reverted"
        raw=revert_bytes(error.get("data"))
        if raw is not None:out["revert_data"]=raw
    return out


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

    def read(self, fn, *, endpoint=None):
        mismatch = False
        last_not_found = False
        rejected = None
        clients = self.clients if endpoint is None else [(name, client) for name, client in self.clients if name == endpoint]
        for _ in range(2 if endpoint is None else 1):
            for name, client in clients:
                try:
                    if client.eth.chain_id != self.chain["chain_id"]:
                        mismatch = True
                        continue
                    value = fn(client)
                    self.active = client
                    return value
                except TransactionNotFound:
                    last_not_found = True
                except ToolError:
                    raise
                except (ContractLogicError, ValueError, Web3RPCError) as exc:
                    # Never echo RPC exceptions: providers may include URLs, requests or credentials.
                    response = getattr(exc, "rpc_response", None)
                    error = response.get("error", {}) if isinstance(response, dict) else {}
                    if not error and exc.args and isinstance(exc.args[0],dict):error=exc.args[0]
                    error=error if isinstance(error,dict) else {}
                    message=str(error.get("message",str(exc))).lower()
                    reverted=isinstance(exc,ContractLogicError) or error.get("code")==3 or "execution reverted" in message
                    if not reverted and (error.get("code") in (-32005, -32002, -32603, 429) or any(t in message for t in ("rate limit","too many requests","timeout","timed out"))):
                        continue
                    if not reverted:
                        rejected={"rpc_error_code":error.get("code") if isinstance(error.get("code"),int) else None}
                        continue
                    raw = getattr(exc, "data", None)
                    if raw is None and isinstance(error, dict):
                        raw = error.get("data")
                    details = {"rpc_name":name}
                    raw=revert_bytes(raw)
                    if raw is not None:
                        from .common import hexdata
                        try:
                            details["revert_data"] = hexdata(raw)
                            from .abi import decode_revert
                            details.update(decode_revert(raw))
                        except ToolError:
                            pass
                    raise ToolError("CALL_REVERTED", "EVM read execution reverted", data=details) from None
                except Exception:
                    continue
            if endpoint is None:
                time.sleep(0.1)
        if rejected is not None:
            raise ToolError("RPC_REQUEST_REJECTED", "RPC rejected a read request; no contract execution failure has been established", data=rejected)
        if last_not_found:
            raise ToolError("NOT_FOUND", "Transaction or receipt not found", retryable=True)
        raise ToolError("CHAIN_ID_MISMATCH" if mismatch else "RPC_UNAVAILABLE",
                        "No reachable RPC with the expected Chain ID", retryable=not mismatch)

    def simulate(self, fn):
        """Try a single alternate RPC after an explicit EVM revert."""
        try:
            return self.read(fn), {}
        except ToolError as original:
            if original.code != "CALL_REVERTED":
                raise
            details = dict(original.data or {})
            primary = details.get("rpc_name")
            primary_client = next((client for name, client in self.clients if name == primary), None)
            primary_url = getattr(getattr(primary_client, "provider", None), "endpoint_uri", None)
            peer = next((name for name, client in self.clients
                         if name != primary and client is not primary_client
                         and (primary_url is None or getattr(getattr(client, "provider", None), "endpoint_uri", None) != primary_url)), None)
            if primary_client is None:
                raise
            if peer is None:
                raise
            retry = {"retried_on":peer, "primary_rpc":primary,
                     "primary_revert":dict(details), "revert_consensus":None}
            try:
                value = self.read(fn, endpoint=peer)
            except ToolError as secondary:
                if secondary.code == "CALL_REVERTED":
                    retry["revert_consensus"] = True
                    retry["secondary_revert"] = secondary.data
                    a, b = details.get("revert_data"), (secondary.data or {}).get("revert_data")
                    retry["revert_payload_agreement"] = a == b if a is not None and b is not None else None
                else:
                    retry["retry_error"] = secondary.code
                details.update(retry)
                if retry["revert_consensus"] is None:
                    raise ToolError("SIMULATION_INCONCLUSIVE", "First RPC reported a revert; the backup could not complete preflight",
                                    retryable=secondary.retryable, data=details) from None
                raise ToolError(original.code, original.message, data=details) from None
            retry["revert_consensus"] = False
            return value, retry

    def send(self, raw, *, endpoint=None):
        # Pick a validated endpoint without accidentally retrying a write.
        def ready(client):
            client.eth.block_number
            return client

        try:
            if endpoint is None:
                client = self.read(ready)
            else:
                client = self.read(ready, endpoint=endpoint)
        except ToolError as exc:
            raise ToolError(exc.code,exc.message,retryable=exc.retryable,data={"broadcast_attempted":False}) from None
        try:
            return client.eth.send_raw_transaction(raw)
        except Exception:
            raise ToolError("BROADCAST_UNKNOWN", "Broadcast outcome unknown; query the recorded hash before replaying", retryable=False) from None

    def optional_request(self, method, params, budget, rpc_name=None):
        """Bounded raw RPC for enrichment; never accepts write methods."""
        allowed = {"eth_call", "eth_getCode", "eth_getBalance", "eth_getStorageAt",
                   "eth_getBlockByNumber", "eth_blockNumber", "debug_traceTransaction", "debug_traceCall"}
        require(method in allowed, "Analysis RPC method is not read-only", "INVALID_INPUT")
        from .network import json_request
        last_code = "SOURCE_UNAVAILABLE"
        last_details = None
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
                        last_details = None
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
                    last_details = optional_failure(response["error"])
                    continue
                require("result" in response and response["result"] is not None, "Optional RPC returned no result", "SOURCE_NOT_FOUND")
                budget.last_rpc_name = name
                return response["result"]
            except ToolError as exc:
                if exc.code == "ANALYSIS_LIMIT": raise
                last_code = exc.code
                last_details = exc.data if isinstance(exc.data,dict) else None
            except Exception:
                last_code = "SOURCE_UNAVAILABLE"
                last_details = None
        raise ToolError(last_code, "Optional RPC capability unavailable; base transaction data is preserved",data=last_details)

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
                return [optional_failure(by_id[i]["error"]) if by_id[i].get("error") else
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
                results.append({"error":exc.code,**(exc.data if isinstance(exc.data,dict) else {})})
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

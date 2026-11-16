from __future__ import annotations

import time

from web3 import Web3, HTTPProvider
from web3.exceptions import TransactionNotFound, ContractLogicError, Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from .common import ToolError, require


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

"""Public execution identity survives exceptions and compound operations."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

from .common import ToolError, jsonable

execution_records = ContextVar("evm_execution_records", default=None)


def preserve_records(response, records):
    if not records:
        return response
    data = response.get("data")
    data = dict(data) if isinstance(data, dict) else {}
    data["transactions"] = deepcopy(records)
    # Include the most recent known identity, including a signed but unsent tx.
    known = next((r for r in reversed(records) if r.get("hash")), None)
    if known:
        for name in ("hash", "sender", "nonce", "broadcast_attempted"):
            data[name] = known.get(name)
        data.setdefault("receipt", known.get("receipt"))
        data.setdefault("status", known["status"])
        if len(records)==1 and response.get("error") and response["error"]["code"] in ("INVALID_INPUT","INTERNAL_ERROR") and known["status"] in ("confirmed","broadcast_unconfirmed"):
            response["error"]={"code":"POST_CONFIRMATION_PROCESSING_FAILED" if known["status"]=="confirmed" else "CONFIRMATION_UNAVAILABLE",
                               "message":"Transaction identity/outcome is preserved; later local processing failed", "retryable":known["status"]!="confirmed"}
            data["status"]=known["status"]
    else:
        for name in ("hash", "status", "phase", "broadcast_attempted", "nonce_check"):
            if name in records[-1]:data.setdefault(name, records[-1][name])
    if len(records)>1 and response.get("error"):
        data["status"] = "partial_execution" if known else "not_broadcast"
    response["data"] = jsonable(data)
    return response


def guarded_execution(method):
    @wraps(method)
    def guarded(self, wallet, *args, **kwargs):
        # Both send/replay have params as their final positional argument.
        params = kwargs.get("params", args[-1] if args else {})
        execute = params.get("execute") is True
        ctx = {"chain_id":self.chain["chain_id"], "sender":wallet.get("address"), "nonce":None,
               "hash":None, "phase":"validation", "status":"not_broadcast", "receipt":None,
               "broadcast_attempted":False}
        records = execution_records.get()
        if execute and records is not None:records.append(ctx)
        try:
            return method(self, wallet, *args, **kwargs, context=ctx)
        except Exception as original:
            exc = original if isinstance(original, ToolError) else ToolError("INTERNAL_ERROR", "Execution stage failed; sensitive exception details are withheld")
            if execute and not ctx["hash"] and ctx.get("sender"):
                try:
                    after=self.rpc.read(lambda w:w.eth.get_transaction_count(ctx["sender"], "pending"))
                    before=ctx.get("pending_nonce_before")
                    ctx["nonce_check"]={"status":"checked", "before":before, "after":after,
                                        "changed":after!=before if before is not None else None,
                                        "proves_this_transaction_was_broadcast":False}
                except Exception:
                    ctx["nonce_check"]={"status":"unavailable", "proves_this_transaction_was_broadcast":False}
            if ctx["broadcast_attempted"] or ctx.get("already_known"):
                if ctx["status"]=="confirmed":
                    exc=ToolError("POST_CONFIRMATION_PROCESSING_FAILED", "Transaction confirmed, but later local processing failed", data=exc.data)
                elif exc.code not in ("EXECUTION_REVERTED", "CONFIRMATION_TIMEOUT", "BROADCAST_UNKNOWN"):
                    exc=ToolError("CONFIRMATION_UNAVAILABLE", "Transaction identity is known, but confirmation could not be checked", retryable=True, data=exc.data)
            elif ctx["phase"] in ("simulation", "gas_estimation") and exc.code=="CALL_REVERTED":
                exc=ToolError("SIMULATION_REVERTED", "Pre-broadcast simulation or gas estimation reverted", data=exc.data)
            payload=dict(exc.data) if isinstance(exc.data,dict) else {}
            payload.update(deepcopy(ctx))
            raise ToolError(exc.code, exc.message, retryable=exc.retryable, data=payload) from None
    return guarded

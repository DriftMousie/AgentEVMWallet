from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hexbytes import HexBytes
from web3 import Web3


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, data=None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.data = code, message, retryable, data


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise ToolError(code, message)


def address(value, field="address"):
    def invalid(reason, message):
        raise ToolError("INVALID_INPUT", f"{field}: {message}", data={"field": field, "reason": reason})
    if not isinstance(value, str):
        invalid("invalid_type", "EVM address must be a string")
    body = value[2:] if value.startswith(("0x", "0X")) else value
    if len(body) != 40:
        invalid("invalid_length", f"Invalid EVM address length: expected 40 hex characters (42 with 0x prefix), got {len(value)} characters")
    if re.fullmatch(r"[0-9a-fA-F]{40}", body) is None:
        invalid("invalid_characters", "Invalid EVM address characters: use only hexadecimal 0-9, a-f after the optional 0x prefix")
    normalized = "0x" + body
    if body != body.lower() and body != body.upper() and not Web3.is_checksum_address(normalized):
        invalid("invalid_checksum", "Invalid EVM address checksum: mixed-case addresses must match EIP-55; verify the source address")
    return Web3.to_checksum_address(normalized)


def wallet_selectors(value):
    """Accept an array or comma-separated IDs/labels, without echoing input."""
    if isinstance(value, str):
        if value.lstrip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                raise ToolError("INVALID_INPUT", 'wallets: invalid JSON array; use ["trading","treasury"] or trading,treasury') from None
        else:
            require(not value.lstrip().startswith("{"), "wallets must be a JSON array or comma-separated wallet IDs")
            value = value.split(",")
    require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
            "wallets must contain non-empty wallet IDs or labels; use a JSON array or comma-separated values")
    return [v.strip() for v in value]


def uint(value, name="integer"):
    require(not isinstance(value, bool), f"Invalid {name}")
    require(isinstance(value, (str, int)), f"{name} must be an integer or integer string")
    require(re.fullmatch(r"[0-9]+", str(value)) is not None, f"Invalid {name}")
    result = int(value)
    require(0 <= result < 2**256, f"{name} outside uint256 range")
    return result


def hexdata(value):
    require(isinstance(value, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value), "Expected even-length 0x hex data")
    return value.lower()


def txhash(value):
    result = hexdata(value)
    require(len(result) == 66, "Transaction hash must be 32 bytes")
    return result


def units(value, decimals):
    decimals = uint(decimals, "decimals")
    require(decimals <= 255, "Invalid decimals")
    require(isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value), "Amount must be a decimal string")
    whole, _, fraction = value.partition(".")
    require(len(fraction) <= decimals, "Amount exceeds token precision")
    return uint(str(int(whole) * 10**decimals + int((fraction + "0" * decimals)[:decimals] or "0")), "amount")


def formatted(value, decimals):
    value, decimals = uint(value), uint(decimals)
    require(decimals <= 255, "Invalid decimals")
    if not decimals:
        return str(value)
    text = str(value).zfill(decimals + 1)
    return (text[:-decimals] + "." + text[-decimals:]).rstrip("0").rstrip(".")


def jsonable(value: Any):
    if isinstance(value, (bytes, bytearray, HexBytes)):
        return "0x" + bytes(value).hex()
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict) or hasattr(value, "items"):
        structural = {"schema_version", "chain_id", "decimals", "confirmations", "data_bytes", "completed_through", "latency_ms", "failed_step",
                      "returned","limit","pages","count","success","failed","unknown","non_call_transactions","token_count","nft_count","risk_count","unknowns_count",
                      "requests_used","current_requests_used","rpc_calls","ttl_seconds","planned_pairs","attempted_pairs","completed_pairs","summary_max_utf8_bytes",
                      "input_bytes","word_index","byte_offset","execution_index","reference_timestamp","refund_count","receipt_position","log_index","candidate_index","auto_selected_candidate","required_confirmations",
                      "code_bytes","functions_count","events_count","errors_count","depth","omitted_probes","line","end_line","blocker_count","unknown_count"}
        out={}
        for k,v in value.items():
            if k in ("path","call_path","field_path") and isinstance(v,list) and all(isinstance(i,int) and not isinstance(i,bool) or isinstance(i,str) and i.isdigit() for i in v):
                out[str(k)]=[int(i) for i in v]
            else:out[str(k)]=v if k in structural and isinstance(v,int) and not isinstance(v,bool) else jsonable(v)
        return out
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ToolError("INVALID_INPUT", "Unable to read valid JSON file") from None


def result(operation, data=None, *, chain_id=None, wallet=None, warnings=None, error=None):
    return {"schema_version": 1, "ok": error is None, "operation": operation,
            "chain_id": chain_id, "wallet": wallet, "data": jsonable(data),
            "warnings": warnings or [], "error": error}

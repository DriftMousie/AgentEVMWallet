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


def address(value):
    require(isinstance(value, str) and Web3.is_address(value), "Invalid EVM address")
    if value != value.lower() and value != value.upper():
        require(Web3.is_checksum_address(value), "Invalid mixed-case address checksum")
    return Web3.to_checksum_address(value)


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
        structural = {"schema_version", "chain_id", "decimals", "confirmations", "data_bytes", "completed_through", "latency_ms", "failed_step"}
        return {str(k): v if k in structural and isinstance(v, int) and not isinstance(v, bool) else jsonable(v) for k, v in value.items()}
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

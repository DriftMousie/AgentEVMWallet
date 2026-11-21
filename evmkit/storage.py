"""Explicit EVM storage addresses and packed integer decoding."""

import re
from web3 import Web3

from .common import ToolError, address, hexdata, require, uint
from .introspect import Inspector


def storage_slot(params):
    mapping = "mapping_key" in params or "base_slot" in params
    if mapping:
        require("mapping_key" in params and "base_slot" in params and not {"slot", "slot_hex"} & params.keys(),
                "Use mapping_key and base_slot together, without a direct slot")
        key = bytes.fromhex(hexdata(params["mapping_key"])[2:])
        require(len(key) == 32, "mapping_key must be one ABI-encoded 32-byte static key")
        base = uint(params["base_slot"], "base_slot")
        return Web3.to_hex(Web3.keccak(key + base.to_bytes(32, "big")))
    if "slot_hex" in params:
        require(params.get("slot", "raw") == "raw", "Choose slot or slot_hex")
        slot = hexdata(params["slot_hex"])
        require(len(slot) == 66, "slot_hex must be exactly 32 bytes")
        return slot
    return f"0x{uint(params.get('slot'), 'slot'):064x}"


def parse_layout(layout):
    require(isinstance(layout, str) and 0 < len(layout) <= 2048, "layout must be a comma-separated string")
    fields = []
    names = set()
    offset = 0
    for spec in layout.split(","):
        match = re.fullmatch(r"(u?int)([0-9]{1,3}):([A-Za-z_][A-Za-z0-9_]*)", spec.strip())
        require(match is not None, "Use layout syntax uint160:sqrtPriceX96,int24:tick")
        kind, width, name = match.groups()
        width = int(width)
        require(8 <= width <= 256 and width % 8 == 0 and offset + width <= 256, "Packed fields must fit in 256 bits")
        require(name not in names, "Duplicate storage field name")
        names.add(name)
        fields.append((kind == "int", width, name, offset))
        offset += width
    return fields


def read_storage(config, chain, rpc, params):
    target = address(params["contract"], "contract")
    slot = storage_slot(params)
    slot_quantity = hex(int(slot, 16))
    layout = parse_layout(params["layout"]) if "layout" in params else []
    inspector = Inspector(config, chain, rpc, {**params, "external":False})
    primary = inspector.budget.last_rpc_name
    endpoints = [primary]
    if primary is not None:
        endpoints.extend(name for name, _ in getattr(rpc, "clients", []) if name != primary)
    first_error = None
    out = None
    block = {"number":str(int(inspector.block, 16)), "hash":inspector.block_hash}
    for endpoint in endpoints:
        inspector.rpc_name = endpoint
        try:
            if endpoint != primary:
                inspector.budget.reserve_requests = 0
                other = inspector.query("eth_getBlockByNumber", [inspector.block, False])
                require(isinstance(other, dict) and other.get("hash") == inspector.block_hash,
                        "Backup RPC does not have the pinned storage block", "CANONICALITY_UNAVAILABLE")
            inspector.budget.reserve_requests = 1
            word = inspector.query("eth_getStorageAt", [target, slot_quantity, inspector.block])
            require(isinstance(word, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", word),
                    "RPC returned an invalid storage word", "INVALID_RESPONSE")
            inspector.canonical_check()
            candidate = {"contract":target, "slot":slot, "raw":word.lower(), "rpc_name":endpoint,
                         "block":{**block, "canonicality":inspector.canonicality}}
            if inspector.canonicality != "verified":
                raise ToolError("CANONICALITY_UNAVAILABLE", "Pinned storage block could not be rechecked", data=candidate)
            out = candidate
            break
        except ToolError as exc:
            if first_error is None:
                first_error = exc
            if exc.code == "ANALYSIS_LIMIT":
                break
    if out is None:
        raise first_error
    if layout:
        value = int(word, 16)
        fields = {}
        for signed, width, name, offset in layout:
            field = (value >> offset) & ((1 << width) - 1)
            if signed and field >= 1 << (width - 1):
                field -= 1 << width
            fields[name] = str(field)
        out["fields"] = fields
    return out

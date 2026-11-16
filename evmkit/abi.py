from __future__ import annotations

import re
from copy import deepcopy
from eth_abi import encode, decode
from eth_abi.grammar import parse
from web3 import Web3

from .common import ToolError, require, hexdata, read_json, jsonable


def method(name, inputs=(), outputs=(), mutability="view"):
    return {"type": "function", "name": name, "stateMutability": mutability,
            "inputs": [{"name": f"arg{i}", "type": t} for i, t in enumerate(inputs)],
            "outputs": [{"name": f"out{i}", "type": t} for i, t in enumerate(outputs)]}


ERC20 = [method("name", outputs=["string"]), method("symbol", outputs=["string"]),
         method("decimals", outputs=["uint8"]), method("totalSupply", outputs=["uint256"]),
         method("balanceOf", ["address"], ["uint256"]), method("allowance", ["address", "address"], ["uint256"]),
         method("transfer", ["address", "uint256"], ["bool"], "nonpayable"),
         method("approve", ["address", "uint256"], ["bool"], "nonpayable"),
         method("transferFrom", ["address", "address", "uint256"], ["bool"], "nonpayable"),
         {"type": "event", "name": "Transfer", "anonymous": False, "inputs": [
             {"name": "from", "type": "address", "indexed": True},
             {"name": "to", "type": "address", "indexed": True},
             {"name": "value", "type": "uint256", "indexed": False}]},
         {"type": "event", "name": "Approval", "anonymous": False, "inputs": [
             {"name": "owner", "type": "address", "indexed": True},
             {"name": "spender", "type": "address", "indexed": True},
             {"name": "value", "type": "uint256", "indexed": False}]}]
NFT = [method("supportsInterface", ["bytes4"], ["bool"]), method("balanceOf", ["address"], ["uint256"]),
       method("balanceOf", ["address", "uint256"], ["uint256"]), method("ownerOf", ["uint256"], ["address"]),
       method("tokenURI", ["uint256"], ["string"]), method("uri", ["uint256"], ["string"]),
       method("getApproved", ["uint256"], ["address"]), method("isApprovedForAll", ["address", "address"], ["bool"]),
       method("approve", ["address", "uint256"], [], "nonpayable"),
       method("setApprovalForAll", ["address", "bool"], [], "nonpayable"),
       method("safeTransferFrom", ["address", "address", "uint256"], [], "nonpayable"),
       method("safeTransferFrom", ["address", "address", "uint256", "uint256", "bytes"], [], "nonpayable"),
       method("safeBatchTransferFrom", ["address", "address", "uint256[]", "uint256[]", "bytes"], [], "nonpayable"),
       method("balanceOfBatch", ["address[]", "uint256[]"], ["uint256[]"])]


def split_types(text):
    if not text:
        return []
    depth, start, out = 0, 0, []
    for i, char in enumerate(text):
        if char in "([": depth += 1
        if char in ")]": depth -= 1
        require(depth >= 0, "Unbalanced signature")
        if char == "," and depth == 0:
            out.append(text[start:i]); start = i + 1
    require(depth == 0, "Unbalanced signature")
    out.append(text[start:])
    return out


def signature_parts(signature):
    require(isinstance(signature, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(.*\)", signature), "Expected full function signature")
    name, rest = signature.split("(", 1)
    types = split_types(rest[:-1])
    try:
        for typ in types:
            parse(typ).validate()
    except Exception:
        raise ToolError("INVALID_INPUT", "Invalid canonical ABI type") from None
    # Aliases change selectors, so demand canonical spelling.
    require(not any(re.search(r"\b(?:uint|int|fixed|ufixed)\b", t) for t in types), "Use canonical types such as uint256")
    return name, types


def coerce(typ, value):
    parsed = parse(typ)
    if parsed.is_array:
        require(isinstance(value, (list, tuple)), "Expected array argument")
        return [coerce(parsed.item_type.to_type_str(), x) for x in value]
    if hasattr(parsed, "components"):
        require(isinstance(value, (list, tuple)) and len(value) == len(parsed.components), "Expected tuple argument")
        return tuple(coerce(t.to_type_str(), v) for t, v in zip(parsed.components, value))
    if typ.startswith(("uint", "int")):
        require(not isinstance(value, bool) and isinstance(value, (int, str)) and re.fullmatch(r"-?[0-9]+", str(value)), "Invalid integer argument")
        return int(value)
    if typ.startswith("bytes"):
        return bytes.fromhex(hexdata(value)[2:]) if isinstance(value, str) else value
    if typ == "address":
        from .common import address
        return address(value)
    if typ == "bool":
        require(isinstance(value, bool), "Boolean argument must be true or false")
    return value


def canonical(item):
    def typ(i):
        return "(" + ",".join(typ(c) for c in i["components"]) + ")" + i["type"][5:] if i["type"].startswith("tuple") else i["type"]
    return item["name"] + "(" + ",".join(typ(i) for i in item.get("inputs", [])) + ")"


def load_abi(value):
    if isinstance(value, str) and value in ("erc20", "erc721", "erc1155", "weth", "erc4626"):
        from .standards import preset
        return preset(value)
    data = read_json(value) if isinstance(value, str) else value
    if isinstance(data, dict): data = data.get("abi")
    require(isinstance(data, list) and all(isinstance(i, dict) and "type" in i for i in data), "Invalid ABI")
    return data


def read_abi(value, function):
    """Only known ERC-20 reads can omit ABI; never infer an unknown interface."""
    if value is not None:
        return load_abi(value)
    abi = [item for item in ERC20 if item.get("stateMutability") in ("view", "pure")]
    try:
        resolve(abi, function)
    except ToolError:
        raise ToolError("ABI_REQUIRED", "Without --abi only standard ERC-20 reads are supported; provide --abi erc20, a file or inline ABI for this function") from None
    return deepcopy(abi)


def resolve(abi, function):
    matches = [i for i in abi if i.get("type") == "function" and (canonical(i) == function if "(" in function else i.get("name") == function)]
    require(len(matches) == 1, "Function missing or overloaded; provide full signature")
    return matches[0]


def encode_call(signature, args):
    _, types = signature_parts(signature)
    require(isinstance(args, list) and len(args) == len(types), "Argument count differs from signature")
    try:
        return "0x" + (Web3.keccak(text=signature)[:4] + encode(types, [coerce(t, v) for t, v in zip(types, args)])).hex()
    except ToolError:
        raise
    except Exception:
        raise ToolError("INVALID_INPUT", "Arguments cannot be ABI encoded") from None


def decode_call(data, signature=None, abi=None):
    data = hexdata(data)
    if abi is not None:
        choices = [canonical(i) for i in abi if i.get("type") == "function" and "0x" + Web3.keccak(text=canonical(i))[:4].hex() == data[:10]]
        require(len(choices) == 1, "ABI does not identify one matching function", "DECODE_ERROR")
        signature = choices[0]
    if not signature:
        return {"status": "unknown", "selector": data[:10] if len(data) >= 10 else None}
    _, types = signature_parts(signature)
    require(len(data) >= 10 and data[:10] == "0x" + Web3.keccak(text=signature)[:4].hex(), "Function selector mismatch", "DECODE_ERROR")
    try:
        args = decode(types, bytes.fromhex(data[10:]))
        require(encode_call(signature, list(jsonable(args))) == data, "Noncanonical or trailing calldata", "DECODE_ERROR")
        return {"status": "decoded", "signature": signature, "types": types, "args": jsonable(args)}
    except ToolError:
        raise
    except Exception:
        raise ToolError("DECODE_ERROR", "Calldata does not match supplied types") from None


def decode_revert(data, abi=None):
    data = hexdata(data)
    candidates = [("Error(string)", ["string"]), ("Panic(uint256)", ["uint256"])]
    for item in abi or []:
        if item.get("type") == "error":
            sig = canonical(item)
            candidates.append((sig, signature_parts(sig)[1]))
    for signature, types in candidates:
        if data[:10] == "0x" + Web3.keccak(text=signature)[:4].hex():
            try:
                values = decode(types, bytes.fromhex(data[10:]))
                return {"error_signature": signature, "error_args": jsonable(values)}
            except Exception:
                break
    return {"error_signature": None}

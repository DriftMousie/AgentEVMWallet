"""Read-only transaction enrichment with explicit provenance and uncertainty."""
from __future__ import annotations
import json
import re
from eth_abi import decode, encode
from web3 import Web3

from .abi import canonical, decode_call, encode_call, load_abi
from .common import ToolError, address, formatted, hexdata, jsonable, require, uint
from .explorers import Sources
from .network import Budget
from .standards import entries

ZERO = "0x" + "0"*40
IMPLEMENTATION_SLOT = hex(int.from_bytes(Web3.keccak(text="eip1967.proxy.implementation"), "big")-1)
BEACON_SLOT = hex(int.from_bytes(Web3.keccak(text="eip1967.proxy.beacon"), "big")-1)


def ashex(value):
    return hexdata(value) if isinstance(value, str) else Web3.to_hex(value)


def signed_format(value, decimals):
    return ("-" if value < 0 else "") + formatted(abs(value), decimals)


def settings(config, params):
    defaults = {"time_budget_seconds": 60, "max_requests": 100, "max_frames": 1000,
                "max_depth": 64, "max_pages": 10, "max_tokens": 20, "max_addresses": 20}
    raw = config.raw.get("analysis", {})
    for key, default in defaults.items():
        defaults[key] = uint(params.get(key, raw.get(key, default)), key)
        require(defaults[key] > 0, f"{key} must be positive")
    for key in ("external", "trace"):
        defaults[key] = params.get(key, raw.get(key, True))
        require(isinstance(defaults[key], bool), f"{key} must be boolean")
    return defaults


class Analyzer:
    def __init__(self, config, chain, rpc, params, inspection=None):
        self.config, self.chain, self.rpc, self.params = config, chain, rpc, params
        self.options = settings(config, params)
        self.budget = Budget(self.options["time_budget_seconds"], self.options["max_requests"])
        self.sources = Sources(config, chain, self.budget, self.options["external"], self.options["max_pages"])
        self.inspection = inspection
        self.block = "latest"
        if inspection:
            tx = inspection["transaction"]
            self.block = hex(int(tx["blockNumber"])) if tx.get("blockNumber") is not None else "pending"
        self.memo, self.issues, self.evidence = {}, [], {}

    def issue(self, code):
        if code not in self.issues: self.issues.append(code)

    def query(self, method, params):
        return self.rpc.optional_request(method, params, self.budget)

    def attempt(self, fn, code):
        try: return fn()
        except ToolError as exc: self.issue(code+":"+exc.code)
        except Exception: self.issue(code+":INVALID_SOURCE_DATA")
        return None

    def call(self, target, signature, args, outputs, block=None):
        data = self.query("eth_call", [{"to": address(target), "data": encode_call(signature, args)}, block or self.block])
        result = decode(outputs, bytes.fromhex(ashex(data)[2:]))
        return result[0] if len(result) == 1 else result

    def quiet_call(self, *args, **kwargs):
        try: return self.call(*args, **kwargs)
        except ToolError as exc:
            if exc.code == "ANALYSIS_LIMIT": self.issue(exc.code)
        except Exception: pass
        return None

    def code(self, target, block=None):
        key = ("code", address(target), block or self.block)
        if key not in self.memo:
            self.memo[key] = ashex(self.query("eth_getCode", [key[1], key[2]]))
        return self.memo[key]

    def metadata(self, target):
        target = address(target)
        if ("metadata", target) in self.memo: return self.memo["metadata", target]
        decimals = self.quiet_call(target, "decimals()", [], ["uint8"])
        symbol = self.quiet_call(target, "symbol()", [], ["string"])
        out = {"address": target, "decimals": decimals, "symbol": symbol[:128] if isinstance(symbol, str) else None,
               "source": "rpc", "block": self.block, "status": "complete" if decimals is not None and symbol is not None else "partial"}
        self.memo["metadata", target] = out
        return out

    def kind(self, target, vault=False):
        target = address(target)
        key = ("kind", target, vault)
        if key in self.memo: return self.memo[key]
        previous = self.memo.get(("kind", target, False))
        if previous in ("erc721", "erc1155"):
            return previous
        # ERC-165 must reject the invalid interface; a selector match alone is insufficient.
        valid165 = self.quiet_call(target, "supportsInterface(bytes4)", ["0x01ffc9a7"], ["bool"])
        invalid165 = self.quiet_call(target, "supportsInterface(bytes4)", ["0xffffffff"], ["bool"])
        kind = None
        if valid165 is True and invalid165 is False:
            for name, interface in [("erc721", "0x80ac58cd"), ("erc1155", "0xd9b67a26")]:
                if self.quiet_call(target, "supportsInterface(bytes4)", [interface], ["bool"]) is True:
                    kind = name; break
        if kind is None and vault:
            asset = self.quiet_call(target, "asset()", [], ["address"])
            assets = self.quiet_call(target, "totalAssets()", [], ["uint256"])
            if asset and address(asset) != address(ZERO) and assets is not None:
                kind = "erc4626"; self.memo["asset", target] = address(asset)
        if kind is None:
            meta = self.metadata(target)
            if meta["decimals"] is not None and meta["symbol"] is not None and self.quiet_call(target, "totalSupply()", [], ["uint256"]) is not None:
                kind = "erc20"
        self.memo[key] = kind
        return kind

    def implementation(self, target):
        target = address(target)
        key = ("implementation", target)
        if key in self.memo: return self.memo[key]
        code = self.code(target)
        out = {"address": None, "type": None, "scope": "block_end" if self.block.startswith("0x") else self.block}
        if code.startswith("0xef0100") and len(code) == 48:
            out.update(address=address("0x"+code[8:]), type="eip7702")
        else:
            minimal = re.fullmatch(r"0x363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3", code)
            if minimal: out.update(address=address("0x"+minimal[1]), type="eip1167")
            elif code != "0x":
                for slot, kind in [(IMPLEMENTATION_SLOT, "eip1967"), (BEACON_SLOT, "eip1967_beacon")]:
                    try:
                        raw = ashex(self.query("eth_getStorageAt", [target, slot, self.block]))
                        if len(raw) != 66 or int(raw, 16) == 0: continue
                        implementation = address("0x"+raw[-40:])
                        if kind == "eip1967_beacon":
                            implementation = self.quiet_call(implementation, "implementation()", [], ["address"])
                        if implementation and self.code(implementation) != "0x":
                            out.update(address=address(implementation), type=kind); break
                    except ToolError as exc:
                        if exc.code == "ANALYSIS_LIMIT": self.issue(exc.code)
        self.memo[key] = out
        return out

    def verified_abis(self, target):
        target = address(target)
        if ("abis", target) in self.memo: return self.memo["abis", target]
        result = []
        impl = self.attempt(lambda: self.implementation(target), "PROXY_LOOKUP")
        locations = [target] + ([impl["address"]] if impl and impl["address"] and impl["address"] != target else [])
        for location in locations:
            latest = self.attempt(lambda: self.code(location, "latest"), "ABI_CURRENT_CODE")
            code_hash = Web3.to_hex(Web3.keccak(hexstr=latest)) if latest is not None else None
            info = self.sources.contract(location, code_hash=code_hash)
            if not info.get("abi"): continue
            # Explorer ABI describes current code. Check code equality before claiming historical correspondence.
            same_code = self.attempt(lambda: self.code(location) == self.code(location, "latest"), "ABI_HISTORY_CHECK")
            result.append({"abi": info["abi"], "source": info["source"], "address": location, "historical_match": same_code is True})
        self.memo["abis", target] = result
        return result

    @staticmethod
    def candidates(data, records):
        values = {}
        selector = data[:10]
        for record in records:
            item = record["abi"]
            if item.get("type") != "function": continue
            try:
                signature = canonical(item)
                if Web3.to_hex(Web3.keccak(text=signature)[:4]) != selector: continue
                decoded = decode_call(data, signature)
                names = [i.get("name") or f"arg{j}" for j, i in enumerate(item["inputs"])]
                if len(names) != len(set(names)): names = [f"arg{j}" for j in range(len(names))]
                key = (signature, tuple(names))
                if key not in values:
                    values[key] = {**decoded, "named_args": dict(zip(names, decoded["args"])), "standards": [],
                                   "source": record.get("source", "builtin"), "historical_match": False, "sources": [], "abi_addresses": []}
                values[key]["standards"] = sorted(set(values[key]["standards"]+record.get("standards", [])))
                values[key]["historical_match"] |= record.get("historical_match", False)
                values[key]["sources"] = sorted(set(values[key]["sources"]+[record.get("source", "builtin")]))
                if record.get("address") and record["address"] not in values[key]["abi_addresses"]:
                    values[key]["abi_addresses"].append(record["address"])
            except (ToolError, KeyError, ValueError, TypeError): continue
        return list(values.values())

    def decode_input(self, data, target=None, abi=None, signature=None, context=None):
        data = ashex(data)
        if signature is not None or abi is not None:
            provided = load_abi(abi) if abi is not None else None
            out = decode_call(data, signature, provided)
            record = next((i for i in provided or [] if i.get("type") == "function" and canonical(i) == out["signature"]), None)
            names = [i.get("name") or f"arg{j}" for j, i in enumerate(record["inputs"])] if record else [f"arg{j}" for j in range(len(out["args"]))]
            if len(names) != len(set(names)): names = [f"arg{j}" for j in range(len(out["args"]))]
            out.update(named_args=dict(zip(names, out["args"])), source="explicit", confidence="user_supplied", template_safe=True)
            return out
        if len(data) < 10:
            return {"status": "unknown", "selector": None, "source": "none", "candidates": []}
        builtin = self.candidates(data, entries("function"))
        if builtin:
            if target:
                vault = any("erc4626" in c["standards"] and c["signature"].split("(")[0] in ("deposit", "mint", "withdraw", "redeem") for c in builtin)
                kind = self.kind(context or target, vault)
                selected = [c for c in builtin if kind and kind in c["standards"]]
                if selected: builtin = selected
            else: kind = None
            if len(builtin) == 1:
                return {**builtin[0], "confidence": "interface_evidence" if kind else "standard_pattern", "template_safe": kind is not None}
        verified_records = []
        if target and self.options["external"]:
            for record in self.verified_abis(target):
                verified_records.extend({**record, "abi": item} for item in record["abi"])
        verified = self.candidates(data, verified_records)
        confirmed = [c for c in verified if c["historical_match"]]
        if len(confirmed) == 1:
            return {**confirmed[0], "confidence": "verified_abi", "template_safe": True}
        if builtin or verified:
            return {"status": "ambiguous", "selector": data[:10], "source": "builtin_or_verified",
                    "candidates": builtin+verified, "complete": True, "template_safe": False}
        lookup = self.sources.signatures(data[:10])
        candidates = []
        for signature in lookup["candidates"]:
            item = {"signature": signature, "source": "4byte", "decodable": False}
            try:
                decoded = decode_call(data, signature)
                item.update(args=decoded["args"], types=decoded["types"], decodable=True)
            except ToolError: pass
            candidates.append(item)
        return {"status": "ambiguous" if len(candidates)>1 else "candidate" if candidates else "unknown",
                "selector": data[:10], "source": "4byte" if candidates else "none", "candidates": candidates,
                "complete": lookup["complete"], "template_safe": False}

    def amount(self, raw, target=None, argument=None, native=False):
        raw = uint(raw)
        meta = {"address": None, "decimals": self.chain["decimals"], "symbol": self.chain.get("symbol"), "source": "chain_config", "status": "complete"} if native else self.metadata(target)
        readable = formatted(raw, meta["decimals"]) if meta["decimals"] is not None else None
        if meta["status"] != "complete": self.issue("TOKEN_METADATA_PARTIAL")
        return {"argument": argument, "raw": str(raw), "token_address": meta["address"], "decimals": meta["decimals"],
                "symbol": meta["symbol"], "amount": readable,
                "amount_readable": f"{readable} {meta['symbol']}" if readable is not None and meta["symbol"] else None,
                "source": meta["source"], "block": self.block}

    def input_amounts(self, decoded, target):
        if decoded.get("status") != "decoded" or not target: return []
        name = decoded["signature"].split("(")[0]
        kind = self.kind(target, name in ("deposit", "mint", "withdraw", "redeem"))
        args = decoded["args"]
        if kind == "erc4626" and name in ("deposit", "mint", "withdraw", "redeem"):
            role = "assets" if name in ("deposit", "withdraw") else "shares"
            asset = self.memo.get(("asset", address(target)))
            return [self.amount(args[0], asset if role == "assets" else target, role)] if role == "shares" or asset else []
        if kind == "erc20" and name in ("transfer", "approve", "transferFrom"):
            return [self.amount(args[-1], target, "value")]
        if name == "withdraw" and decoded["signature"] == "withdraw(uint256)":
            return [self.amount(args[0], target, argument="wad")]
        return []

    @staticmethod
    def decode_event_item(log, item):
        from eth_utils.abi import collapse_if_tuple
        topics = [bytes.fromhex(ashex(t)[2:]) for t in log.get("topics", [])]
        indexed = [i for i in item["inputs"] if i.get("indexed")]
        offset = 0 if item.get("anonymous") else 1
        require(len(topics) == len(indexed)+offset, "Event topic count differs", "DECODE_ERROR")
        if offset:
            require(topics[0] == Web3.keccak(text=canonical(item)), "Event topic differs", "DECODE_ERROR")
        plain = [i for i in item["inputs"] if not i.get("indexed")]
        types = [collapse_if_tuple(i) for i in plain]
        raw = bytes.fromhex(ashex(log.get("data", "0x"))[2:])
        values = list(decode(types, raw))
        require(encode(types, values) == raw, "Noncanonical event data", "DECODE_ERROR")
        result, topic_index, plain_index = {}, offset, 0
        for index, field in enumerate(item["inputs"]):
            typ = collapse_if_tuple(field)
            if field.get("indexed"):
                value = {"topic_hash": ashex(topics[topic_index])} if "[" in typ or typ.startswith("(") or typ in ("string", "bytes") else decode([typ], topics[topic_index])[0]
                topic_index += 1
            else:
                value = values[plain_index]; plain_index += 1
            result[field.get("name") or f"arg{index}"] = value
        return jsonable(result)

    def decode_event(self, log, abi=None):
        target = address(log["address"])
        def matches(records):
            result = []
            for record in records:
                try:
                    item = record["abi"]
                    if item.get("type") != "event": continue
                    args = self.decode_event_item(log, item)
                    result.append({"status": "decoded", "event": item["name"], "signature": canonical(item), "args": args,
                                   "standards": record.get("standards", []), "source": record.get("source", "builtin")})
                except Exception: continue
            return result
        selected = matches([{"abi": i, "source": "explicit"} for i in load_abi(abi)]) if abi is not None else matches(entries("event"))
        if not selected and abi is None and self.options["external"]:
            for record in self.verified_abis(target):
                if record["historical_match"]:
                    selected.extend(matches([{"abi": i, "source": record["source"]} for i in record["abi"]]))
        # Same event definition may be published by both proxy and implementation.
        unique = {json.dumps([s["signature"], s["args"]], sort_keys=True): s for s in selected}
        selected = list(unique.values())
        if len(selected) != 1:
            return {"status": "ambiguous" if selected else "unknown", "event": None, "args": None, "candidates": selected}
        out = selected[0]
        args, amounts = out["args"], []
        if out["event"] in ("Transfer", "Approval") and "value" in args:
            amounts.append(self.amount(args["value"], target, "value"))
        elif out["event"] in ("Deposit", "Withdrawal") and "wad" in args:
            amounts.append(self.amount(args["wad"], target, argument="wad"))
        elif out["event"] in ("Deposit", "Withdraw") and "assets" in args and "shares" in args:
            asset = self.quiet_call(target, "asset()", [], ["address"])
            if asset: amounts.append(self.amount(args["assets"], asset, "assets"))
            amounts.append(self.amount(args["shares"], target, "shares"))
        out["amounts"] = amounts
        if len(amounts) == 1: out["amount_readable"] = amounts[0]["amount_readable"]
        return out

    def counterpart(self, target):
        if target is None:
            return {"address": None, "status": "not_applicable", "name": None, "is_verified": None, "is_proxy": None, "implementation": None}
        target = address(target)
        info = self.sources.contract(target)
        impl = self.attempt(lambda: self.implementation(target), "COUNTERPART_PROXY")
        token_name = self.quiet_call(target, "name()", [], ["string"])
        code = self.attempt(lambda: self.code(target), "COUNTERPART_CODE")
        return {"address": target, "name": token_name[:256] if isinstance(token_name, str) else info["contract_name"],
                "contract_name": info["contract_name"], "is_verified": info["is_verified"],
                "is_contract": code != "0x" if code is not None else None,
                "is_proxy": True if impl and impl["address"] else info["is_proxy"],
                "implementation": impl["address"] if impl else None,
                "implementation_type": impl["type"] if impl else None,
                "implementation_scope": impl["scope"] if impl else "unknown",
                "current_explorer_implementations": info["implementations"],
                "source": {"name": "rpc" if token_name is not None else info["source"], "verification": info["source"]},
                "block": self.block, "verification_scope": "current_explorer_record"}

    def internal_calls(self, hash_):
        if not self.options["trace"]:
            return {"status": "disabled", "source": None, "complete": False, "calls": []}
        trace = self.attempt(lambda: self.query("debug_traceTransaction", [hash_, {"tracer": "callTracer", "timeout": "15s"}]), "CALL_TRACE")
        rows, complete = [], True
        if isinstance(trace, dict):
            receipt_failed = bool(self.inspection and self.inspection.get("receipt") and self.inspection["receipt"]["status"] == 0)
            stack = [(trace, [], receipt_failed, None)]
            while stack:
                frame, path, ancestor_failed, context = stack.pop()
                if len(rows) >= self.options["max_frames"] or len(path) > self.options["max_depth"]:
                    complete = False; continue
                failed = ancestor_failed or bool(frame.get("error"))
                calltype = str(frame.get("type", "CALL")).upper()
                target = address(frame["to"]) if frame.get("to") else None
                execution_context = context if calltype in ("DELEGATECALL", "CALLCODE") else target
                item = {"path": path, "type": calltype, "from": address(frame["from"]) if frame.get("from") else None, "to": target,
                        "execution_context": execution_context, "value_wei": str(int(frame.get("value") or "0x0", 16)),
                        "input": frame.get("input", "0x"), "output": frame.get("output", "0x"),
                        "error": str(frame["error"])[:256] if frame.get("error") else None,
                        "committed": not failed, "is_value_transfer": not failed and calltype in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT")}
                decoded = self.attempt(lambda: self.decode_input(item["input"], target, context=execution_context), "INTERNAL_DECODE")
                item["decoded"] = decoded
                rows.append(item)
                children = frame.get("calls") or []
                for index in range(len(children)-1, -1, -1):
                    stack.append((children[index], path+[index], failed, execution_context))
            if not complete: self.issue("CALL_TRACE_TRUNCATED")
            return {"status": "complete" if complete else "partial", "source": "rpc.callTracer", "complete": complete, "calls": rows}
        summary = self.attempt(lambda: self.sources.internals(hash_), "BLOCKSCOUT_INTERNALS") if self.options["external"] else None
        if summary is None:
            return {"status": "unavailable", "source": None, "complete": False, "calls": []}
        # An explorer's internal-transactions list is not a complete EVM call tree.
        for raw in summary["items"][:self.options["max_frames"]]:
            def loc(value): return value.get("hash") if isinstance(value, dict) else value
            row = {"from": loc(raw.get("from")), "to": loc(raw.get("to")), "type": raw.get("type"), "call_type": raw.get("call_type"),
                   "value_wei": str(raw.get("value", "0")), "index": raw.get("index"),
                   "input": raw.get("input"), "error": raw.get("error"), "committed": None,
                   "decoded": None, "path": None}
            if row["input"]:
                row["decoded"] = self.attempt(lambda: self.decode_input(row["input"], row["to"]), "INTERNAL_DECODE")
            rows.append(row)
        return {"status": "partial", "source": "blockscout.internal-transactions", "complete": False,
                "pagination_complete": summary["complete"], "calls": rows,
                "limitations": ["Explorer summary may omit calldata, reverted frames, delegatecalls or call hierarchy"]}

    def classify(self, decoded, target, committed=None):
        if not decoded or decoded.get("status") != "decoded":
            return {"type": "unknown", "evidence": [], "committed": committed}, []
        signature = decoded["signature"]
        name = signature.split("(")[0]
        args = decoded.get("args", [])
        kind = self.kind(target, name in ("deposit", "mint", "withdraw", "redeem")) if target else None
        risks, business = [], "contract_call"
        evidence = [signature, decoded.get("source", "unknown")]
        if kind in ("erc20", "erc4626") and name in ("transfer", "transferFrom"):
            business = "token_transfer"
        elif kind == "erc20" and signature == "approve(address,uint256)":
            value = int(args[1]); business = "approval_revoke" if value == 0 else "token_approval"
            if value == 2**256-1:
                risks.append({"code": "UNLIMITED_APPROVAL", "severity": "warning", "message": "ERC-20 allowance is uint256 maximum", "evidence": signature})
            if value > 0:
                spender = address(args[0]); spender_code = self.attempt(lambda: self.code(spender), "SPENDER_CODE")
                info = self.sources.contract(spender)
                if spender_code and spender_code != "0x" and info["is_verified"] is False:
                    risks.append({"code": "UNVERIFIED_SPENDER", "severity": "warning", "message": "Approval spender is a contract explicitly reported unverified", "address": spender})
                elif spender_code and spender_code != "0x" and info["is_verified"] is None:
                    risks.append({"code": "SPENDER_VERIFICATION_UNKNOWN", "severity": "info", "message": "Spender verification could not be determined", "address": spender})
        elif kind in ("erc721", "erc1155"):
            business = "nft_transfer" if "Transfer" in name or name == "transferFrom" else "nft_approval" if name in ("approve", "setApprovalForAll") else "nft_call"
            if name == "setApprovalForAll" and args[1] is True:
                risks.append({"code": "OPERATOR_APPROVAL_ALL", "severity": "warning", "message": "Operator may manage all tokens in this collection"})
        if kind == "erc4626" and name in ("deposit", "mint", "withdraw", "redeem"):
            business = "vault_"+name; evidence.append("asset() and totalAssets() available")
        elif signature in ("deposit()", "withdraw(uint256)"):
            business = "wrapped_native_"+("deposit" if name == "deposit" else "withdraw")
            evidence.append("standard function pattern; not proof of implementation")
        return {"type": business, "standard": kind, "confidence": decoded.get("confidence", "pattern"),
                "evidence": evidence, "committed": committed}, risks

    def subjects(self, transaction, events):
        result = []
        requested = self.params.get("addresses", [])
        require(isinstance(requested, list), "addresses must be a JSON array")
        for candidate in [transaction.get("from"), transaction.get("to")]+requested:
            if candidate and address(candidate) not in result: result.append(address(candidate))
        for event in events:
            args = (event.get("decoded") or {}).get("args") or {}
            for role in ("from", "to", "owner", "receiver", "src", "dst"):
                value = args.get(role)
                if isinstance(value, str) and Web3.is_address(value) and address(value) not in result and address(value) != address(ZERO):
                    result.append(address(value))
        if len(result) > self.options["max_addresses"]: self.issue("BALANCE_ADDRESSES_TRUNCATED")
        return result[:self.options["max_addresses"]]

    def balance_changes(self, transaction, receipt, events):
        if not receipt or self.inspection["status"] == "reorged":
            return {"status": "unavailable", "reason": "No canonical mined receipt", "native_transaction": [], "token_event_flows": [], "block_level": []}
        subjects = self.subjects(transaction, events)
        native, flows, block_rows, tokens = [], {}, [], []
        supplied_tokens = self.params.get("tokens", [])
        require(isinstance(supplied_tokens, list), "tokens must be a JSON array")
        tokens.extend(address(t) for t in supplied_tokens)
        target = address(transaction["to"]) if transaction.get("to") else None
        if target and (self.memo.get(("kind", target, False)) == "erc20" or self.memo.get(("kind", target, True)) == "erc4626"):
            tokens.append(target)
        if target and self.memo.get(("asset", target)):
            tokens.append(self.memo["asset", target])
        for event in events:
            for annotation in event["decoded"].get("amounts", []):
                if annotation.get("token_address"):
                    tokens.append(address(annotation["token_address"]))
        native_status = "disabled" if not self.options["trace"] else "unavailable"
        if self.options["trace"]:
            difference = self.attempt(lambda: self.query("debug_traceTransaction", [ashex(transaction["hash"]),
                    {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True, "disableCode": True, "disableStorage": True}, "timeout": "15s"}]), "STATE_DIFF")
            if isinstance(difference, dict) and isinstance(difference.get("pre"), dict) and isinstance(difference.get("post"), dict):
                before = {address(k): v for k, v in difference["pre"].items()}
                after = {address(k): v for k, v in difference["post"].items()}
                for who in subjects:
                    pre, post = before.get(who), after.get(who)
                    if pre is None and post is None:
                        a, b, delta = None, None, 0  # Omitted unchanged account; exact balance not supplied.
                    else:
                        a = int(pre.get("balance", "0x0"), 16) if pre else 0
                        b = int(post.get("balance", hex(a)), 16) if post else 0
                        delta = b-a
                    native.append({"address": who, "pre_raw": str(a) if a is not None else None,
                                   "post_raw": str(b) if b is not None else None, "delta_raw": str(delta),
                                   "delta": signed_format(delta, self.chain["decimals"]), "symbol": self.chain.get("symbol"),
                                   "scope": "transaction", "source": "rpc.prestateTracer", "includes_transaction_fees": True})
                native_status = "complete"
        if receipt["status"] == 1:
            for event in events:
                decoded = event["decoded"]
                if decoded.get("event") != "Transfer" or decoded.get("status") != "decoded": continue
                args = decoded["args"]
                if "value" not in args: continue  # ERC-721 token IDs are not fungible amounts.
                token = address(event["address"])
                if token not in tokens: tokens.append(token)
                for role, sign in (("from", -1), ("to", 1)):
                    who = address(args[role])
                    if who in subjects:
                        flows[token, who] = flows.get((token, who), 0)+sign*int(args["value"])
        token_rows = []
        for (token, who), delta in flows.items():
            meta = self.metadata(token)
            token_rows.append({"token_address": token, "address": who, "delta_raw": str(delta),
                               "delta": signed_format(delta, meta["decimals"]) if meta["decimals"] is not None else None,
                               "symbol": meta["symbol"], "scope": "transaction_events", "source": "receipt.Transfer",
                               "is_exact_balance_change": False})
        tokens = list(dict.fromkeys(tokens))
        if len(tokens)>self.options["max_tokens"]: self.issue("BALANCE_TOKENS_TRUNCATED")
        tokens = tokens[:self.options["max_tokens"]]
        number = int(receipt["blockNumber"])
        if number == 0:
            self.issue("GENESIS_HAS_NO_PREVIOUS_BLOCK")
        else:
            for who in subjects:
                for token in [None]+tokens:
                    def read_balance(block):
                        return int(self.query("eth_getBalance", [who, hex(block)]), 16) if token is None else self.call(token, "balanceOf(address)", [who], ["uint256"], hex(block))
                    a = self.attempt(lambda: read_balance(number-1), "BLOCK_PRE_BALANCE")
                    b = self.attempt(lambda: read_balance(number), "BLOCK_POST_BALANCE")
                    meta = self.metadata(token) if token else {"decimals": self.chain["decimals"], "symbol": self.chain.get("symbol")}
                    block_rows.append({"address": who, "token_address": token, "pre_block": number-1, "post_block": number,
                                       "pre_raw": str(a) if a is not None else None, "post_raw": str(b) if b is not None else None,
                                       "delta_raw": str(b-a) if a is not None and b is not None else None,
                                       "delta": signed_format(b-a, meta["decimals"]) if a is not None and b is not None and meta["decimals"] is not None else None,
                                       "symbol": meta["symbol"], "scope": "block", "source": "historical_rpc",
                                       "status": "complete" if a is not None and b is not None else "unavailable",
                                       "is_transaction_attribution": False})
        return {"status": "complete" if native_status == "complete" and all(r["status"] == "complete" for r in block_rows) else "partial",
                "native_transaction_status": native_status, "native_transaction": native,
                "token_event_flows_status": "not_applicable" if receipt["status"] != 1 else "partial" if "EVENTS_TRUNCATED" in self.issues or any(e["decoded"].get("status") != "decoded" for e in events) else "complete",
                "token_event_flows": token_rows, "block_level": block_rows,
                "execution_fee_paid_wei": self.inspection.get("execution_fee_paid_wei"),
                "limitations": ["Event flows are not guaranteed token balance changes", "Block differences may include other transactions"]}

    def run(self):
        tx, receipt = self.inspection["transaction"], self.inspection["receipt"]
        if self.params.get("abi") is not None or self.params.get("signature") is not None:
            decoded = self.decode_input(tx["data"], tx.get("to"), self.params.get("abi"), self.params.get("signature"))
        else:
            decoded = self.attempt(lambda: self.decode_input(tx["data"], tx.get("to")), "INPUT_DECODE") or {"status": "unknown", "candidates": []}
        amounts = self.attempt(lambda: self.input_amounts(decoded, tx.get("to")), "INPUT_AMOUNTS") or []
        if int(tx.get("value", 0)) > 0: amounts.append(self.amount(tx["value"], native=True, argument="transaction.value"))
        events = []
        raw_logs = receipt.get("logs", []) if receipt else []
        for log in raw_logs[:self.options["max_frames"]]:
            provided = self.params.get("abi") if tx.get("to") and address(log["address"]) == address(tx["to"]) else None
            event = self.attempt(lambda: self.decode_event(log, provided), "EVENT_DECODE")
            events.append({"address": log["address"], "log_index": log.get("logIndex"),
                           "decoded": event or {"status": "unknown", "event": None, "args": None}})
        if len(raw_logs)>len(events): self.issue("EVENTS_TRUNCATED")
        counterpart = self.attempt(lambda: self.counterpart(tx.get("to")), "COUNTERPART")
        classification, risks = self.attempt(lambda: self.classify(decoded, tx.get("to"), bool(receipt["status"]) if receipt else None), "CLASSIFICATION") or ({"type": "unknown"}, [])
        if tx.get("to") and tx["data"] == "0x" and int(tx.get("value", 0))>0:
            classification = {"type": "native_value_call" if counterpart and counterpart.get("is_contract") else "native_transfer",
                              "evidence": ["empty input and native value"], "committed": bool(receipt["status"]) if receipt else None}
        internals = (self.attempt(lambda: self.internal_calls(ashex(tx["hash"])), "INTERNAL_CALLS") or {"status": "unavailable", "complete": False, "calls": []}) if receipt else {"status": "not_applicable", "complete": False, "calls": []}
        if counterpart:
            executed = list(dict.fromkeys(row["to"] for row in internals["calls"]
                            if row.get("type") == "DELEGATECALL" and row.get("execution_context") == tx.get("to") and len(row.get("path") or []) == 1))
            if len(executed) == 1:
                counterpart.update(implementation=executed[0], implementation_scope="transaction_trace", is_proxy=True)
            elif len(executed)>1:
                counterpart.update(implementation=None, executed_implementations=executed, implementation_scope="transaction_trace", is_proxy=True)
        for row in internals["calls"]:
            if not row.get("to") or not row.get("decoded"): continue
            context = row.get("execution_context") or row["to"]
            typed = self.attempt(lambda: self.classify(row["decoded"], context, row.get("committed")), "INTERNAL_CLASSIFY")
            if typed:
                row["classification"], flags = typed
                if row["path"] != []:
                    risks.extend({**flag, "call_path": row["path"]} for flag in flags)
            row["amounts"] = self.attempt(lambda: self.input_amounts(row["decoded"], context), "INTERNAL_AMOUNTS") or []
        changes = self.attempt(lambda: self.balance_changes(tx, receipt, events), "BALANCE_CHANGES") or {"status": "unavailable"}
        if receipt:
            canonical_block = self.attempt(lambda: self.query("eth_getBlockByNumber", [hex(int(receipt["blockNumber"])), False]), "CANONICAL_BLOCK_CHECK")
            if canonical_block and ashex(canonical_block["hash"]) != ashex(receipt["blockHash"]):
                changes = {"status": "unavailable", "reason": "Chain reorganization detected"}
                self.issue("REORG_DETECTED")
            elif canonical_block:
                changes["canonicality"] = "verified"
            else:
                changes["canonicality"] = "unverified"
                changes["status"] = "partial" if changes.get("status") == "complete" else changes.get("status", "unavailable")
        warnings = list(dict.fromkeys(self.issues+self.sources.warnings))
        complete = (not warnings and decoded.get("status") == "decoded" and changes.get("status") == "complete"
                    and internals.get("complete", False) and counterpart is not None
                    and all(event["decoded"].get("status") == "decoded" for event in events))
        return {"inspection": self.inspection, "input_decode": decoded, "amounts": amounts,
                "decoded_events": events, "counterpart": counterpart, "balance_changes": changes,
                "internal_calls": internals, "classification": classification, "risks": risks,
                "analysis_status": "complete" if complete else "partial", "warnings": warnings,
                "capabilities": {"external_enabled": self.options["external"], "trace_enabled": self.options["trace"],
                                 "requests_used": self.budget.requests}}

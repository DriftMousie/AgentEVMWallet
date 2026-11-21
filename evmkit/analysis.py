"""Read-only transaction enrichment with explicit provenance and uncertainty."""
from __future__ import annotations
import json
import re
import hashlib
from eth_abi import decode, encode
from web3 import Web3

from .abi import canonical, decode_call, encode_call, load_abi
from .common import ToolError, address, formatted, hexdata, jsonable, require, uint
from .explorers import Sources
from .network import Budget
from .standards import entries, V2_ROUTER
from .diagnostics import missing, combine
from .cache import SnapshotCache, analysis_key

ZERO = "0x" + "0"*40
IMPLEMENTATION_SLOT = hex(int.from_bytes(Web3.keccak(text="eip1967.proxy.implementation"), "big")-1)
BEACON_SLOT = hex(int.from_bytes(Web3.keccak(text="eip1967.proxy.beacon"), "big")-1)


def ashex(value):
    return hexdata(value) if isinstance(value, str) else Web3.to_hex(value)


def signed_format(value, decimals):
    return ("-" if value < 0 else "") + formatted(abs(value), decimals)


def text_input(data):
    """Decode the whole payload, never just a printable four-byte prefix."""
    try:
        text = bytes.fromhex(data[2:]).decode("utf-8", errors="strict")
    except (ValueError, UnicodeError): return None
    if not text.strip() or not all(c.isprintable() or c in "\n\r\t\u200c\u200d" for c in text): return None
    return {"text": text[:4096], "text_truncated": len(text)>4096, "text_length": len(text), "encoding": "utf-8"}


def settings(config, params):
    defaults = {"time_budget_seconds": 60, "max_requests": 300, "max_frames": 1000,
                "max_depth": 64, "max_pages": 10, "max_tokens": 20, "max_addresses": 20}
    raw = config.raw.get("analysis", {})
    for key, default in defaults.items():
        defaults[key] = uint(params.get(key, raw.get(key, default)), key)
        require(defaults[key] > 0, f"{key} must be positive")
    for key in ("external", "trace"):
        defaults[key] = params.get(key, raw.get(key, True))
        require(isinstance(defaults[key], bool), f"{key} must be boolean")
    defaults["require_trace"] = params.get("require_trace", False)
    require(isinstance(defaults["require_trace"], bool), "require_trace must be boolean")
    if defaults["require_trace"]:
        require(params.get("trace") is not False, "--require-trace cannot be combined with --no-trace")
        defaults["trace"] = True
    return defaults


class Analyzer:
    def __init__(self, config, chain, rpc, params, inspection=None):
        self.config, self.chain, self.rpc, self.params = config, chain, rpc, dict(params)
        for key in ("addresses", "tokens"):
            if key in params:
                require(isinstance(params[key], list), f"{key} must be a JSON array")
                self.params[key] = [address(value, field=key) for value in params[key]]
        if "focus_address" in params: self.params["focus_address"]=address(params["focus_address"],field="focus_address")
        self.options = settings(config, params)
        self.budget = Budget(self.options["time_budget_seconds"], self.options["max_requests"])
        self.sources = Sources(config, chain, self.budget, self.options["external"], self.options["max_pages"], params.get("no_cache",False))
        self.inspection = inspection
        self.block = "latest"
        if inspection:
            tx = inspection["transaction"]
            self.block = hex(int(tx["blockNumber"])) if tx.get("blockNumber") is not None else "pending"
        self.memo, self.issues, self.evidence = {}, [], {}
        self.enhancements_missing = []
        self.persistent = SnapshotCache(config)
        self.cache_scope = analysis_key(config,{"chain":chain["alias"]})
        self.metadata_addresses = set()

    def issue(self, code, required=False):
        if required:
            if code not in self.issues: self.issues.append(code)
        else:
            self.enhancements_missing = combine(self.enhancements_missing, [missing(code)])

    def diagnostics(self):
        optional = getattr(self.sources, "enhancements_missing", [])
        return list(self.issues), combine(self.enhancements_missing, optional)

    def query(self, method, params):
        cacheable = method in ("eth_call", "eth_getCode", "eth_getBalance", "eth_getStorageAt")
        key = ("rpc", method, json.dumps(params, sort_keys=True))
        if cacheable and key in self.memo: return self.memo[key]
        value = self.rpc.optional_request(method, params, self.budget)
        if cacheable: self.memo[key] = value
        return value

    def attempt(self, fn, code, required=False):
        try: return fn()
        except ToolError as exc: self.issue(code+":"+exc.code, required)
        except Exception: self.issue(code+":INVALID_SOURCE_DATA", required)
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
        self.prefetch_metadata([target])
        return self.memo["metadata",target]

    def prefetch_metadata(self, targets):
        pending = []
        block_hash = self.memo.get("block_hash")
        if not block_hash and self.inspection and self.inspection.get("receipt"):
            block_hash = ashex(self.inspection["receipt"]["blockHash"])
        for target in dict.fromkeys(address(t) for t in targets):
            self.metadata_addresses.add(target)
            if ("metadata",target) in self.memo: continue
            key = hashlib.sha256(json.dumps(["metadata-v1",self.cache_scope,target,self.block,block_hash]).encode()).hexdigest()
            hit = self.persistent.get(key) if block_hash and not self.params.get("no_cache") else None
            if hit: self.memo["metadata",target] = hit; continue
            pending.append((target,key))
        for offset in range(0,len(pending),10):
            group = pending[offset:offset+10]
            calls = [("eth_call",[{"to":target,"data":encode_call(signature,[])},self.block])
                     for target,_ in group for signature in ("decimals()","symbol()")]
            if hasattr(self.rpc,"optional_batch"):
                rows = self.attempt(lambda:self.rpc.optional_batch(calls,self.budget),"TOKEN_METADATA") or [{} for _ in calls]
            else:
                rows = [{"result":self.attempt(lambda:self.query(method,params),"TOKEN_METADATA")} for method,params in calls]
            for row in rows:
                if row.get("error"): self.issue("TOKEN_METADATA:"+row["error"])
            for i,(target,key) in enumerate(group):
                decimals,symbol = None,None
                try: decimals = decode(["uint8"],bytes.fromhex(ashex(rows[i*2]["result"])[2:]))[0]
                except Exception: pass
                try:
                    raw = bytes.fromhex(ashex(rows[i*2+1]["result"])[2:])
                    try: symbol = decode(["string"],raw)[0]
                    except Exception:
                        if len(raw)==32: symbol=raw.rstrip(b"\0").decode("utf-8")
                    if not isinstance(symbol,str) or not symbol or not all(c.isprintable() for c in symbol): symbol=None
                except Exception: pass
                out={"address":target,"decimals":decimals,"symbol":symbol[:128] if symbol else None,
                     "source":"rpc","block":self.block,"status":"complete" if decimals is not None and symbol else "partial"}
                self.memo["metadata",target]=out
                if out["status"]=="complete" and block_hash: self.persistent.put(key,out,3600)
                if out["status"]!="complete": self.issue("TOKEN_METADATA_PARTIAL")

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
                                   "parameter_schema":item["inputs"],
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
            if record:out["parameter_schema"]=record["inputs"]
            return out
        if data == "0x":
            return {"status": "empty", "input_kind": "empty", "selector": None, "source": "none", "candidates": []}
        builtin = self.candidates(data, entries("function"))
        if builtin:
            token_standards = {"erc20", "erc721", "erc1155", "weth", "erc4626"}
            if target and any(token_standards.intersection(c["standards"]) for c in builtin):
                vault = any("erc4626" in c["standards"] and c["signature"].split("(")[0] in ("deposit", "mint", "withdraw", "redeem") for c in builtin)
                kind = self.kind(context or target, vault)
                selected = [c for c in builtin if kind and kind in c["standards"]]
                if selected: builtin = selected
            else: kind = None
            if len(builtin) == 1:
                evidenced = kind is not None and kind in builtin[0]["standards"]
                return {**builtin[0], "confidence": "interface_evidence" if evidenced else "standard_pattern", "template_safe": evidenced}
        text = text_input(data)
        if target and int(address(target), 16) > 0x11:
            code = self.attempt(lambda: self.code(context or target), "INPUT_ACCOUNT_CODE")
            if code == "0x" and not builtin:
                return {"status": "onchain_message" if text else "data_only", "input_kind": "utf8_text" if text else "binary_data",
                        "source": "rpc_code_and_input", "confidence": "block_end_eoa", "is_function_call": False,
                        "candidates": [], "template_safe": False, **(text or {})}
        if len(data) < 10:
            return {"status": "unknown", "selector": None, "source": "none", "candidates": [],
                    "input_kind": "utf8_text_candidate" if text else "opaque_data", **(text or {})}
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
                "complete": lookup["complete"], "template_safe": False,
                "input_kind": "utf8_text_candidate" if text else "opaque_data", **(text or {})}

    def amount(self, raw, target=None, argument=None, native=False):
        raw = uint(raw)
        meta = {"address": None, "decimals": self.chain["decimals"], "symbol": self.chain.get("symbol"), "source": "chain_config", "status": "complete"} if native else self.metadata(target)
        readable = formatted(raw, meta["decimals"]) if meta["decimals"] is not None else None
        if meta["status"] != "complete": self.issue("TOKEN_METADATA_PARTIAL")
        return {"argument": argument, "raw": str(raw), "token_address": meta["address"], "decimals": meta["decimals"],
                "symbol": meta["symbol"], "amount": readable,
                "amount_readable": f"{readable} {meta['symbol']}" if readable is not None and meta["symbol"] else None,
                "source": meta["source"], "block": self.block}

    def route_amounts(self, decoded, value_wei=None):
        """Known Router layouts, or typed/named ABI parameters; never 4byte guesses."""
        if decoded.get("status") != "decoded": return []
        signature = decoded.get("signature")
        layout = next((item for item in V2_ROUTER if canonical(item) == signature), None)
        names = [field["name"] for field in layout["inputs"]] if layout else list(decoded.get("named_args", {}))
        types, values = decoded.get("types", []), decoded.get("args", [])
        if len(names) != len(types) or len(names) != len(values) or "path" not in names: return []
        index = names.index("path")
        if types[index] != "address[]": return []
        if not isinstance(values[index], (list, tuple)) or len(values[index]) < 2:
            self.issue("ROUTE_AMOUNTS:INVALID_PATH"); return []
        try:
            path = [address(item) for item in values[index]]
            require(all(int(item,16)>0 for item in path), "Zero-address route is not a standard token path")
        except ToolError:
            self.issue("ROUTE_AMOUNTS:INVALID_PATH"); return []
        roles = {"amountIn": (0, "input_exact"), "amountInMax": (0, "input_maximum"),
                 "amountOut": (-1, "output_exact"), "amountOutMin": (-1, "output_minimum")}
        out = []
        name = layout["name"] if layout else ""
        native_output = layout is not None and (name.endswith("ForETH") or "ForETHSupporting" in name or name == "swapTokensForExactETH")
        for argument, (position, role) in roles.items():
            if argument not in names: continue
            param_index = names.index(argument)
            if types[param_index] != "uint256": continue
            native = position == -1 and native_output
            annotation = self.amount(values[param_index], None if native else path[position], argument, native=native)
            annotation.update(role=role, binding={"kind":"path", "parameter":"path", "index":position,
                              "address":path[position], "source":"builtin_router" if layout else "abi_parameter_convention"})
            if native: annotation["wrapped_token_address"] = path[position]
            out.append(annotation)
        if layout and layout["stateMutability"] == "payable" and value_wei is not None:
            annotation = self.amount(value_wei, native=True, argument="transaction.value")
            annotation.update(role="input_maximum" if name == "swapETHForExactTokens" else "input_exact",
                              wrapped_token_address=path[0], binding={"kind":"call_value", "source":"builtin_router", "path_index":0})
            out.insert(0, annotation)
        return out

    def input_amounts(self, decoded, target, value_wei=None):
        if decoded.get("status") != "decoded" or not target: return []
        routed = self.route_amounts(decoded, value_wei)
        if routed: return routed
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

    def pool_amounts(self, decoded, target):
        families = {"dex_v2_pair", "uniswap_v3_pool", "pancake_v3_pool"}
        if not families.intersection(decoded.get("standards", [])): return []
        args = decoded["args"]
        fields = [key for key in args if key in {"amount0", "amount1", "amount0In", "amount1In", "amount0Out", "amount1Out",
                   "reserve0", "reserve1", "paid0", "paid1", "protocolFeesToken0", "protocolFeesToken1"}]
        if not fields: return []
        tokens = [self.quiet_call(target, f"token{i}()", [], ["address"]) for i in (0,1)]
        if not all(tokens) or tokens[0] == tokens[1] or ZERO in tokens:
            self.issue("POOL_ASSETS:UNAVAILABLE")
            return []
        out = []
        for field in fields:
            position = 0 if "0" in field else 1
            raw = int(args[field]); annotation = self.amount(abs(raw), tokens[position], field)
            annotation["raw"] = str(raw)
            if raw < 0:
                annotation["amount"] = signed_format(raw, annotation["decimals"]) if annotation["decimals"] is not None else None
                annotation["amount_readable"] = f"{annotation['amount']} {annotation['symbol']}" if annotation["amount"] is not None and annotation["symbol"] else None
            role = "reserve" if field.startswith("reserve") else "pool_input" if field.endswith("In") else "pool_output" if field.endswith("Out") else "protocol_fee" if field.startswith("protocolFees") else "repaid" if field.startswith("paid") else "pool_delta" if decoded["event"] == "Swap" else "event_amount"
            annotation.update(role=role, scope="pool", binding={"kind":"pool_token", "pool_address":target,
                              "function":f"token{position}()", "block":self.block})
            out.append(annotation)
        return out

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
                    standards = record.get("standards", [])
                    if not standards:
                        layout = lambda value: [(f.get("name"), f["type"], bool(f.get("indexed"))) for f in value["inputs"]]
                        standards = sorted({s for known in entries("event") if canonical(known["abi"]) == canonical(item)
                                            and layout(known["abi"]) == layout(item) for s in known["standards"]})
                    result.append({"status": "decoded", "event": item["name"], "signature": canonical(item), "args": args,
                                   "standards": standards, "source": record.get("source", "builtin")})
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
        out["amounts"].extend(self.pool_amounts(out, target))
        if len(amounts) == 1: out["amount_readable"] = amounts[0]["amount_readable"]
        return out

    def counterpart(self, target):
        if target is None:
            return {"address": None, "status": "not_applicable", "name": None, "is_verified": None, "is_proxy": None, "implementation": None}
        target = address(target)
        if ("identity", target) in self.memo: return self.memo["identity", target]
        code = self.attempt(lambda: self.code(target), "COUNTERPART_CODE")
        impl = self.attempt(lambda: self.implementation(target), "COUNTERPART_PROXY")
        info = self.sources.contract(target, with_activity=True, include_abi=code != "0x")
        if code is None and (not impl or not impl["address"]) and len(info.get("implementations", [])) == 1:
            impl = {"address": info["implementations"][0], "type": info.get("proxy_type"), "scope": "current_explorer"}
        token_name = self.quiet_call(target, "name()", [], ["string"]) if code and code != "0x" else None
        kind = "unknown" if code is None else "eoa" if code == "0x" else "delegated_eoa" if impl and impl["type"] == "eip7702" else "contract"
        if code is None and info.get("proxy_type") == "eip7702": kind = "delegated_eoa"
        implementation_name = info.get("implementation_names", {}).get(impl["address"]) if impl else None
        if impl and impl["address"] and implementation_name is None:
            implementation_info = self.sources.contract(impl["address"])
            implementation_name = implementation_info.get("contract_name")
        out = {"address": target, "name": token_name[:256] if isinstance(token_name, str) else info.get("contract_name"),
                "contract_name": info.get("contract_name"), "is_verified": info.get("is_verified"),
                "account_type": kind, "is_contract": None if kind == "unknown" else kind == "contract",
                "has_executable_code": code != "0x" if code is not None else None,
                "is_proxy": True if impl and impl["address"] else False if code == "0x" else info.get("is_proxy"),
                "proxy_type": impl["type"] if impl and impl["address"] else info.get("proxy_type") if code is None or code != "0x" else None,
                "implementation": impl["address"] if impl else None,
                "implementation_name": implementation_name,
                "implementation_type": impl["type"] if impl else None,
                "implementation_scope": impl["scope"] if impl else "unknown",
                "current_explorer_proxy_type": info.get("proxy_type"),
                "current_explorer_implementations": info.get("implementations", []),
                "creator_address": info.get("creator_address"), "creation_transaction_hash": info.get("creation_transaction_hash"),
                "has_transactions": info.get("has_transactions"), "transactions_count": info.get("transactions_count"),
                "activity_scope": info.get("activity_scope"), "identity_sources": info.get("identity_sources", {}),
                "verification_by_source": info.get("verification_by_source", {}),
                "source": {"name": "rpc" if token_name is not None else info.get("source"), "verification": info.get("source")},
                "block": self.block, "verification_scope": "current_explorer_record"}
        self.memo["identity", target] = out
        return out

    def internal_calls(self, hash_, *, decode_inputs=True):
        if not self.options["trace"]:
            return {"status": "disabled", "source": None, "complete": False, "calls": []}
        trace = self.attempt(lambda: self.query("debug_traceTransaction", [hash_, {"tracer": "callTracer", "timeout": "10s"}]), "CALL_TRACE")
        if trace is not None and (not isinstance(trace, dict) or not isinstance(trace.get("type"), str)):
            self.issue("CALL_TRACE:INVALID_SOURCE_DATA")
            trace = None
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
                item = {"path": path, "depth": len(path), "type": calltype, "from": address(frame["from"]) if frame.get("from") else None, "to": target,
                        "execution_context": execution_context, "value_wei": str(int(frame.get("value") or "0x0", 16)),
                        "input": ashex(frame["input"]) if frame.get("input") is not None else None, "output": frame.get("output", "0x"),
                        "error": str(frame["error"])[:256] if frame.get("error") else None,
                        "committed": not failed, "may_carry_value": calltype in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT")}
                item["is_value_transfer"] = item["may_carry_value"] and not failed and int(item["value_wei"]) > 0
                from .calldata import input_fields
                item.update(input_fields(item))
                item["decoded"] = self.attempt(lambda: self.decode_input(item["input"], target, context=execution_context), "INTERNAL_DECODE") if decode_inputs else None
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
                   "decoded": None, "path": None, "depth": None}
            calltype = str(row.get("call_type") or row.get("type") or "").upper()
            row["may_carry_value"] = calltype in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT")
            row["committed"] = False if row["error"] else None
            value = int(row["value_wei"], 16 if row["value_wei"].startswith("0x") else 10)
            row["is_value_transfer"] = False if not row["may_carry_value"] or value == 0 or row["error"] else None
            from .calldata import input_fields
            row.update(input_fields(row))
            if decode_inputs and row["input"]:
                row["decoded"] = self.attempt(lambda: self.decode_input(row["input"], row["to"]), "INTERNAL_DECODE")
            rows.append(row)
        return {"status": "partial", "source": "blockscout.internal-transactions", "complete": False,
                "pagination_complete": summary["complete"], "calls": rows,
                "limitations": ["Explorer summary may omit calldata, reverted frames, delegatecalls or call hierarchy"]}

    def classify(self, decoded, target, committed=None):
        if decoded and decoded.get("status") in ("onchain_message", "data_only"):
            return {"type": decoded["status"], "evidence": ["no executable code at target block", "whole input inspected"], "confidence": decoded.get("confidence"), "committed": committed}, []
        if not decoded or decoded.get("status") != "decoded":
            return {"type": "unknown", "evidence": [], "committed": committed}, []
        signature = decoded["signature"]
        if any(canonical(item) == signature for item in V2_ROUTER):
            return {"type":"token_swap", "confidence":decoded.get("confidence"), "evidence":[signature, "V2 router function pattern"], "committed":committed}, []
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
                spender = address(args[0]); info = self.counterpart(spender)
                identity = {k: info.get(k) for k in ("address", "account_type", "is_contract", "has_executable_code", "creator_address", "creation_transaction_hash", "has_transactions", "transactions_count", "activity_scope", "identity_sources", "verification_by_source")}
                if info["is_contract"] is True and info["is_verified"] is False:
                    risks.append({"code": "UNVERIFIED_SPENDER", "severity": "warning", "message": "Approval spender is a contract explicitly reported unverified", "address": spender, "identity": identity})
                elif info["has_executable_code"] and info["is_verified"] is None:
                    risks.append({"code": "SPENDER_VERIFICATION_UNKNOWN", "severity": "info", "message": "Spender verification could not be determined", "address": spender, "identity": identity})
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

    def approval_hint(self,decoded):
        candidates=[decoded] if decoded.get("status")=="decoded" else decoded.get("candidates",[])
        match=next((c for c in candidates if c.get("signature")=="approve(address,uint256)" and len(c.get("args",[]))==2),None)
        if not match: return None
        spender=self.counterpart(address(match["args"][0]))
        if spender.get("is_contract") is not True or spender.get("is_verified") is not False: return None
        return ({"value":"approval_like","confidence":"low","evidence":["approve_signature_pattern","spender_explicitly_unverified"],
                 "scope":"ERC-20 allowance vs ERC-721 token approval is unresolved"},
                {"code":"UNVERIFIED_SPENDER_CANDIDATE","severity":"info","address":spender["address"],
                 "message":"Approval-like calldata targets an explicitly unverified spender; interface semantics remain unresolved",
                 "identity":{k:spender.get(k) for k in ("is_contract","creator_address","creation_transaction_hash","has_transactions","verification_by_source")}})

    def subjects(self, transaction, events):
        result = []
        requested = self.params.get("addresses", [])
        require(isinstance(requested, list), "addresses must be a JSON array")
        for candidate in [transaction.get("from"), transaction.get("to"),self.params.get("focus_address")]+requested:
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
        native, flows, block_rows, tokens, omitted = [], {}, [], [], []
        supplied_tokens = self.params.get("tokens", [])
        require(isinstance(supplied_tokens, list), "tokens must be a JSON array")
        tokens.extend(address(t) for t in supplied_tokens)
        tokens.extend(self.memo.get("route_tokens", []))
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
            if "required_state_diff" in self.memo:
                difference = self.memo["required_state_diff"]
            else:
                difference = self.attempt(lambda: self.query("debug_traceTransaction", [ashex(transaction["hash"]),
                        {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True, "disableCode": True, "disableStorage": True}, "timeout": "10s"}]), "STATE_DIFF")
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
            elif difference is not None:
                self.issue("STATE_DIFF:INVALID_SOURCE_DATA")
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
            planned = len(subjects) * (len(tokens)+1)
            for who in subjects:
                for token in [None]+tokens:
                    capacity = self.budget.requests_limit - self.budget.reserve_requests - self.budget.requests
                    try: self.budget.remaining()
                    except ToolError: capacity = 0
                    if capacity < 2:
                        self.issue("BLOCK_BALANCES:ANALYSIS_LIMIT")
                        omitted.append({"address":who, "token_address":token})
                        continue
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
        block_complete = number == 0 or len(block_rows) == planned and all(r["status"] == "complete" for r in block_rows)
        return {"status": "complete" if native_status == "complete" and block_complete else "partial",
                "native_transaction_status": native_status, "native_transaction": native,
                "token_event_flows_status": "not_applicable" if receipt["status"] != 1 else "partial" if "EVENTS_TRUNCATED" in self.issues or any(e["decoded"].get("status") != "decoded" for e in events) else "complete",
                "token_event_flows": token_rows, "block_level": block_rows,
                "block_level_status": "not_applicable" if number == 0 else "complete" if len(block_rows) == planned and all(r["status"] == "complete" for r in block_rows) else "partial",
                "block_level_coverage": {"planned_pairs": 0 if number == 0 else planned, "attempted_pairs":len(block_rows),
                                         "completed_pairs":sum(r["status"] == "complete" for r in block_rows), "omitted_pairs":omitted},
                "execution_fee_paid_wei": self.inspection.get("execution_fee_paid_wei"),
                "limitations": ["Event flows are not guaranteed token balance changes", "Block differences may include other transactions"]}

    def run(self):
        self.budget.reserve_requests = min(2, max(0,self.options["max_requests"]-1))
        self.budget.phase = "input_decode"
        tx, receipt = self.inspection["transaction"], self.inspection["receipt"]
        self.budget.phase="metadata"
        self.prefetch_metadata([row["address"] for row in (receipt.get("logs",[]) if receipt else [])[:self.options["max_frames"]]])
        self.budget.phase="input_decode"
        if self.params.get("abi") is not None or self.params.get("signature") is not None:
            decoded = self.decode_input(tx["data"], tx.get("to"), self.params.get("abi"), self.params.get("signature"))
        else:
            decoded = self.attempt(lambda: self.decode_input(tx["data"], tx.get("to")), "INPUT_DECODE", required=True) or {"status": "unknown", "candidates": []}
        self.memo["root_decode"]=decoded
        from .delegation import declared_calls,classify_delegation
        declared=self.attempt(lambda:declared_calls(tx,self.chain,self.options["max_frames"]),"DECLARED_EXECUTION") or {"status":"unavailable","calls":[],"source":"encoded_execution","manager":None}
        for row in declared["calls"]:
            row["decoded"]=self.attempt(lambda:self.decode_input(row["input"],row["to"]),"DECLARED_DECODE")
        self.budget.phase = "amounts"
        amounts = self.attempt(lambda: self.input_amounts(decoded, tx.get("to"), tx.get("value",0)), "INPUT_AMOUNTS") or []
        if int(tx.get("value", 0)) > 0 and not any(a["argument"] == "transaction.value" for a in amounts):
            amounts.append(self.amount(tx["value"], native=True, argument="transaction.value"))
        self.budget.phase = "events"
        events = []
        raw_logs = receipt.get("logs", []) if receipt else []
        for log in raw_logs[:self.options["max_frames"]]:
            provided = self.params.get("abi") if tx.get("to") and address(log["address"]) == address(tx["to"]) else None
            event = self.attempt(lambda: self.decode_event(log, provided), "EVENT_DECODE", required=True)
            events.append({"address": log["address"], "log_index": log.get("logIndex"),
                           "decoded": event or {"status": "unknown", "event": None, "args": None}})
        if len(raw_logs)>len(events): self.issue("EVENTS_TRUNCATED", required=True)
        self.budget.phase = "trace"
        if self.options["trace"] and receipt:
            self.memo["required_state_diff"] = self.attempt(lambda: self.query("debug_traceTransaction", [ashex(tx["hash"]),
                    {"tracer":"prestateTracer", "tracerConfig":{"diffMode":True,"disableCode":True,"disableStorage":True}, "timeout":"10s"}]), "STATE_DIFF")
        internals = (self.attempt(lambda: self.internal_calls(ashex(tx["hash"])), "INTERNAL_CALLS") or {"status": "unavailable", "complete": False, "calls": []}) if receipt else {"status": "not_applicable", "complete": False, "calls": []}
        self.budget.phase = "identity"
        sender = self.attempt(lambda: self.counterpart(tx.get("from")), "SENDER")
        counterpart = self.attempt(lambda: self.counterpart(tx.get("to")), "COUNTERPART")
        classification, risks = self.attempt(lambda: self.classify(decoded, tx.get("to"), bool(receipt["status"]) if receipt else None), "CLASSIFICATION") or ({"type": "unknown"}, [])
        if tx.get("to") and tx["data"] == "0x" and int(tx.get("value", 0))>0:
            classification = {"type": "native_value_call" if counterpart and counterpart.get("has_executable_code") else "native_transfer",
                              "evidence": ["empty input and native value"], "committed": bool(receipt["status"]) if receipt else None}
        if classification.get("type") in ("unknown","contract_call"):
            hint=self.attempt(lambda:self.approval_hint(decoded),"APPROVAL_HINT")
            if hint:
                classification["inferred_intent"]=hint[0];risks.append(hint[1])
        delegated=self.attempt(lambda:classify_delegation(self,tx,internals,sender,declared,counterpart),"DELEGATION_CONTEXT")
        if delegated:
            delegated["business_classification"]=classification
            classification=delegated
            risks.append({"code":"CONTEXT_DEPENDENT_REPLAY","severity":"warning","message":delegated["note"]})
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
            row["amounts"] = self.attempt(lambda: self.input_amounts(row["decoded"], context, row.get("value_wei")), "INTERNAL_AMOUNTS") or []
        self.memo["route_tokens"] = list(dict.fromkeys(a["token_address"] for a in amounts + [a for row in internals["calls"] for a in row.get("amounts",[])] if a.get("token_address") and a.get("binding",{}).get("kind") == "path"))
        self.budget.phase = "block_balances"
        changes = self.attempt(lambda: self.balance_changes(tx, receipt, events), "BALANCE_CHANGES") or {"status": "unavailable"}
        self.budget.reserve_requests = 0
        self.budget.phase = "canonicality"
        if receipt:
            canonical_block = self.attempt(lambda: self.query("eth_getBlockByNumber", [hex(int(receipt["blockNumber"])), False]), "CANONICAL_BLOCK_CHECK")
            if canonical_block and ashex(canonical_block["hash"]) != ashex(receipt["blockHash"]):
                changes = {"status": "unavailable", "reason": "Chain reorganization detected"}
                self.issue("REORG_DETECTED", required=True)
            elif canonical_block:
                changes["canonicality"] = "verified"
            else:
                changes["canonicality"] = "unverified"
                changes["status"] = "partial" if changes.get("status") == "complete" else changes.get("status", "unavailable")
        input_complete = decoded.get("status") in ("decoded", "onchain_message", "data_only", "empty")
        events_complete = len(raw_logs) == len(events) and all(event["decoded"].get("status") == "decoded" for event in events)
        if not input_complete: self.issue("INPUT_UNRESOLVED", required=True)
        if not events_complete: self.issue("EVENTS_UNRESOLVED", required=True)
        if self.inspection["status"] == "reorged": self.issue("REORG_DETECTED", required=True)
        if tx.get("blockNumber") is not None and receipt is None: self.issue("RECEIPT_UNAVAILABLE", required=True)
        if self.options["require_trace"]:
            if internals.get("source") != "rpc.callTracer" or not internals.get("complete"):
                self.issue("REQUIRED_CALL_TRACE_UNAVAILABLE", required=True)
            if changes.get("native_transaction_status") != "complete":
                self.issue("REQUIRED_STATE_DIFF_UNAVAILABLE", required=True)
        from .flows import fund_flows,infer_intent
        flows=fund_flows(self,tx,receipt,internals,changes)
        inferred=infer_intent(classification,flows,decoded,self.chain)
        if inferred: classification["inferred_intent"]=inferred
        warnings, enhancements = self.diagnostics()
        if self.options["require_trace"]:
            enhancements = [e for e in enhancements if e["capability"] not in ("internal_calls", "native_state_diff")]
        complete = not warnings
        output = {"inspection": self.inspection, "input_decode": decoded, "amounts": amounts,
                "decoded_events": events, "sender": sender, "counterpart": counterpart, "balance_changes": changes,
                "internal_calls": internals, "classification": classification, "risks": risks,
                "declared_calls":declared,
                "analysis_status": "complete" if complete else "partial", "warnings": warnings,
                "analysis_profile": "strict_trace" if self.options["require_trace"] else "standard",
                "core_status": {"input": "complete" if input_complete else "partial", "events": "complete" if events_complete else "partial"},
                "enhancements_missing": enhancements,
                "fund_flows":flows,
                "token_metadata":[self.memo["metadata",target] for target in sorted(self.metadata_addresses) if ("metadata",target) in self.memo],
                "capabilities": {"external_enabled": self.options["external"], "trace_enabled": self.options["trace"],
                                 "requests_used": self.budget.requests, "requests_by_stage":dict(self.budget.by_stage),
                                 "rpc_calls":self.budget.rpc_calls,"rpc_calls_by_method":dict(self.budget.rpc_by_method)}}
        from .agent_output import suggested_next, transaction_conclusion
        output.update(transaction_conclusion(output))
        next_params={**self.params,"chain":self.chain["alias"],"hash":ashex(tx["hash"])}
        output["suggested_next"]=suggested_next(next_params,output) if warnings else []
        output["issues"]=[{"code":code,"suggested_next":output["suggested_next"]} for code in warnings]
        from .calldata import refresh_timing
        return refresh_timing(output)

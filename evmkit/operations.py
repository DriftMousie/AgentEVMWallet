from __future__ import annotations

import json
import time
from pathlib import Path

from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from web3 import Web3

from .abi import ERC20, NFT, canonical, coerce, decode_call, decode_revert, encode_call, load_abi, read_abi, resolve, signature_parts
from .common import ToolError, address, formatted, hexdata, jsonable, read_json, require, result, txhash, uint, units, wallet_selectors
from .engine import Engine, Journal, fees, fee_summary, wait_receipt
from .rpc import RPC
from .error_details import check_dependencies, diagnostic_error

WRITE = {"execute", "gas", "gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei", "skip_simulation", "reason", "wait", "timeout", "confirmations"}
BASE = {"chain", "wallet"}
ANALYSIS = {"external", "trace", "time_budget_seconds", "max_requests", "max_frames", "max_depth", "max_pages", "max_tokens", "max_addresses", "no_cache"}
CONTRACT_DISCOVERY={"chain","block","external","no_cache","time_budget_seconds","max_requests","max_proxy_depth","max_nodes"}
CONTRACT_INSPECTION=CONTRACT_DISCOVERY|{"address","account","functions","auto","view","include_source","no_semantics","max_probes","max_source_files"}
# This registry drives both the Python API and the CLI, including unknown-field rejection.
OPS = {
    "chain": {"list": set(), "health": {"chain"}, "block": {"chain", "block"}, "fees": {"chain"},
              "probe": {"chain", "explorer", "hash", "time_budget_seconds", "max_requests"}},
    "wallet": {"list": set(), "balance": BASE | {"address", "token", "block"},
               "balances": {"chain", "wallets", "tokens", "block"}, "validate": {"address"}},
    "token": {"info": {"chain", "token"}, "convert": {"amount", "amount_raw", "decimals"}},
    "transaction": {"inspect": {"chain", "hash"}, "status": {"chain", "hash"},
                    "nonce": BASE | {"address"}, "gas": {"chain"},
                    "wait": {"chain", "hash", "confirmations", "timeout"},
                    "decode": {"chain", "hash", "abi", "signature"} | ANALYSIS,
                    "analyze": {"chain", "hash", "abi", "signature", "tokens", "addresses", "require_trace", "focus_address", "view", "include_inputs"} | ANALYSIS,
                    "plan": {"chain","hash","wallet","address","abi","signature","override","field_map","take_call","take_candidate","include_inputs","gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"} | ANALYSIS,
                    "export-template": {"chain", "hash", "abi", "signature", "output"} | ANALYSIS,
                    "accelerate": BASE | WRITE | {"hash"}, "cancel": BASE | WRITE | {"hash"},
                    "replay": BASE | {"hash", "execute", "wait", "confirmations", "timeout"}},
    "contract": {"functions": {"abi","contract"}|CONTRACT_DISCOVERY, "encode": {"abi", "function", "args","contract"}|CONTRACT_DISCOVERY,
                 "selector":{"signature"},
                 "introspect":CONTRACT_INSPECTION,
                 "diagnose":CONTRACT_INSPECTION|{"amount","amount_raw","amount_unit","pattern"},
                 "decode": {"abi", "data"},
                 "call": BASE | {"contract", "abi", "function", "args", "address", "block", "value_wei"}|CONTRACT_DISCOVERY,
                 "write": BASE | WRITE | {"contract", "abi", "function", "args", "value_wei"}},
    "transfer": {"native": BASE | WRITE | {"to", "amount", "amount_raw"},
                 "erc20": BASE | WRITE | {"to", "token", "amount", "amount_raw", "decimals"}},
    "approve": {"allowance": BASE | {"address", "token", "spender"},
                "set": BASE | WRITE | {"token", "spender", "amount", "amount_raw", "decimals", "unlimited", "zero_first"},
                "revoke": BASE | WRITE | {"token", "spender"}},
    "nft": {"info": BASE | {"address", "contract", "standard", "token_id", "operator"},
            "balance-batch": {"chain", "contract", "addresses", "token_ids"},
            "transfer": BASE | WRITE | {"contract", "standard", "to", "token_id", "quantity", "data"},
            "transfer-batch": BASE | WRITE | {"contract", "to", "token_ids", "quantities", "data"},
            "approve": BASE | WRITE | {"contract", "operator", "token_id", "all", "approved"},
            "revoke": BASE | WRITE | {"contract", "operator", "token_id", "all"},
            "mint": BASE | WRITE | {"contract", "abi", "function", "signature", "args", "value_wei"}},
    "events": {"read": {"chain", "contract", "from_block", "to_block", "topics", "abi", "chunk_size", "include_unconfirmed"} | ANALYSIS},
    "raw": {"encode": {"signature", "args"}, "decode": {"data", "signature", "abi", "output_types"},
            "patch":{"chain","wallet","address","to","data","word","type","value","value_wei","verify"},
            "call": BASE | {"to", "data", "signature", "args", "value_wei", "address", "block", "output_types", "template"},
            "write": BASE | WRITE | {"to", "data", "signature", "args", "value_wei", "template"},
            "template": {"template"}, "reencode": {"template", "args", "output"}},
    "signing": {"message": {"wallet", "message", "execute"}, "typed": {"wallet", "chain", "typed_data", "execute"},
                "verify-message": {"message", "signature", "address"},
                "verify-typed": {"typed_data", "signature", "address"}},
    "batch": {"run": {"items", "file", "execute", "continue_on_error"}},
    "address": {"history":{"chain","address","limit","direction","to","cursor","no_cache","max_pages","max_requests","time_budget_seconds"},
                "actions":{"chain","address","limit","direction","to","cursor","no_cache","max_pages","max_requests","time_budget_seconds"}},
}


class Toolkit:
    def __init__(self, config, rpc_factory=RPC):
        self.config, self.rpc_factory = config, rpc_factory
        self.warnings = []
        self._rpcs = {}

    def rpc(self, p):
        chain = self.config.chain(p.get("chain"))
        if chain["alias"] not in self._rpcs:
            self._rpcs[chain["alias"]] = self.rpc_factory(self.config, chain)
        return chain, self._rpcs[chain["alias"]]

    def owner(self, p):
        require(not (p.get("address") and p.get("wallet")), "Use address or wallet, not both")
        return address(p["address"]) if p.get("address") else self.config.wallet(p.get("wallet"))["address"]

    def wallet(self, p):
        return self.config.wallet(p.get("wallet"), signing=p.get("execute", False))

    def call(self, p, contract, signature, args, outputs, block="latest", caller=None):
        _, rpc = self.rpc(p)
        target = address(contract)
        require(rpc.read(lambda w: w.eth.get_code(target, block)), "Address has no contract code", "NO_CONTRACT")
        tx = {"to": target, "data": encode_call(signature, args)}
        if caller: tx["from"] = caller
        output = rpc.read(lambda w: w.eth.call(tx, block))
        try:
            values = decode(outputs, output)
            return values[0] if len(values) == 1 else values
        except Exception:
            raise ToolError("DECODE_ERROR", "Contract return data does not match ABI") from None

    def contract_code(self, p, target):
        _, rpc = self.rpc(p)
        require(rpc.read(lambda w: w.eth.get_code(address(target))), "Address has no contract code", "NO_CONTRACT")

    def send(self, p, to, value=0, data="0x", erc20=False):
        chain, rpc = self.rpc(p)
        return Engine(self.config, chain, rpc).send(self.wallet(p), to, value, data, p, erc20=erc20)

    def amount(self, p, decimals):
        require((p.get("amount") is None) != (p.get("amount_raw") is None), "Provide exactly one of amount or amount_raw")
        return uint(p["amount_raw"], "amount") if p.get("amount_raw") is not None else units(p["amount"], decimals)

    def token_decimals(self, p):
        return uint(p["decimals"]) if p.get("decimals") is not None else self.call(p, p["token"], "decimals()", [], ["uint8"])

    def run(self, module, operation, params):
        from .execution import execution_records,preserve_records
        parent_records=execution_records.get()
        records=[];token=execution_records.set(records)
        try:response=preserve_records(self._run(module,operation,params),records)
        finally:
            execution_records.reset(token)
            if parent_records is not None:parent_records.extend(records)
        if isinstance(params,dict) and params.get("execute") is True and response.get("error") and not records and response.get("wallet") and response.get("chain_id"):
            # Pre-engine validation may fail before any nonce baseline was captured.
            payload=dict(response.get("data") or {})
            payload.update(hash=None,status="not_broadcast",broadcast_attempted=False,phase="preflight")
            try:
                _,rpc=self.rpc(params);sender=response["wallet"]["address"]
                current=rpc.read(lambda w:w.eth.get_transaction_count(sender,"pending"))
                payload["nonce_check"]={"status":"no_baseline","before":None,"after":current,"changed":None,"proves_this_transaction_was_broadcast":False}
            except Exception:payload["nonce_check"]={"status":"unavailable","proves_this_transaction_was_broadcast":False}
            response["data"]=jsonable(payload)
        from .agent_output import suggested_next,summary_envelope
        if module=="transaction" and operation=="analyze" and response.get("ok"):
            from .calldata import refresh_timing, input_fields, expanded_input
            analysis = response["data"]
            refresh_timing(analysis)
            transaction = analysis["inspection"]["transaction"]
            include = params.get("include_inputs", False)
            now = int(time.time())
            if include:
                analysis.update(expanded_input({"input":transaction["data"], "decoded":analysis["input_decode"]}, transaction, now))
            for group in (analysis.get("internal_calls",{}),analysis.get("declared_calls",{})):
                for row in group.get("calls",[]):
                    row.update(input_fields(row))
                    if include: row.update(expanded_input(row, transaction, now))
                    else: row.pop("input",None);row.pop("input_raw",None)
        if response.get("error"):
            response["error"]["suggested_next"]=suggested_next(params,response.get("data"),response["error"]["code"])
        if module=="transaction" and operation=="analyze" and isinstance(params,dict) and params.get("view")=="summary":
            return summary_envelope(response)
        if module=="contract" and operation in ("introspect","diagnose") and isinstance(params,dict) and params.get("view","summary")=="summary":
            from .introspect import summary_envelope as contract_summary
            return contract_summary(response)
        return response

    def _run(self, module, operation, params):
        self.warnings = []
        name = f"{module}.{operation}"
        chain_id, wallet = None, None
        try:
            require(module in OPS and operation in OPS[module], "Unknown operation")
            require(isinstance(params, dict), "params must be an object")
            require(not (set(params) - OPS[module][operation]), "Unknown operation parameters")
            check_dependencies(module, operation)
            for key in ("execute", "wait", "skip_simulation", "unlimited", "zero_first", "all", "approved", "continue_on_error", "include_unconfirmed", "external", "trace", "require_trace", "explorer", "no_cache", "include_inputs", "verify","auto","no_semantics","include_source"):
                if key in params: require(isinstance(params[key], bool), f"{key} must be boolean")
            if params.get("chain"):
                chain_id = self.config.chain(params["chain"])["chain_id"]
            if module == "transaction" and operation == "plan":
                require(not (params.get("wallet") is not None and params.get("address") is not None),
                        "Use wallet or address, not both")
            if params.get("wallet"):
                wallet = self.config.public_wallet(self.config.wallet(params["wallet"]))
            method = getattr(self, module + "_" + operation.replace("-", "_"))
            data = method(dict(params))
            return result(name, data, chain_id=chain_id, wallet=wallet, warnings=self.config.warnings + self.warnings)
        except ToolError as exc:
            return result(name, exc.data, chain_id=chain_id, wallet=wallet, warnings=self.config.warnings + self.warnings,
                          error={"code": exc.code, "message": exc.message, "retryable": exc.retryable})
        except Exception as exc:
            # Existing operations may index required, user-supplied parameters.
            missing_param = (isinstance(exc, KeyError) and len(exc.args) == 1
                             and isinstance(exc.args[0], str)
                             and exc.args[0] in OPS.get(module, {}).get(operation, ())
                             and isinstance(params, dict) and exc.args[0] not in params)
            error = diagnostic_error(exc, invalid_input=missing_param)
            return result(name, error.data, chain_id=chain_id, wallet=wallet,
                          warnings=self.config.warnings + self.warnings,
                          error={"code":error.code, "message":error.message, "retryable":error.retryable})

    def chain_list(self, p):
        return [{"alias": k, **v} for k, v in self.config.chains.items()]

    def chain_health(self, p):
        _, rpc = self.rpc(p)
        health = rpc.health()
        require(any(x["valid"] for x in health), "No healthy RPC with correct Chain ID", "RPC_UNAVAILABLE")
        return health

    def chain_block(self, p):
        _, rpc = self.rpc(p)
        return rpc.read(lambda w: w.eth.get_block(block_id(p.get("block", "latest"))))

    def chain_fees(self, p):
        chain, rpc = self.rpc(p)
        return {**fee_summary(fees(rpc, chain, {})), "scope": "EVM execution; excludes chain-specific data fees"}

    transaction_gas = chain_fees

    def chain_probe(self, p):
        from .probe import probe
        from types import SimpleNamespace
        chain = self.config.chain(p.get("chain"))
        error = None
        try: _, rpc = self.rpc(p)
        except ToolError as exc:
            error = {"status": "unavailable", "reason": exc.code, "message": exc.message}
            rpc = SimpleNamespace(clients=[])
        report = probe(self.config, chain, rpc, p)
        if error: report["rpc_configuration"] = error
        return report

    def wallet_list(self, p):
        rows = []
        for ident in self.config.wallets:
            try: rows.append(self.config.public_wallet(self.config.wallet(ident)))
            except ToolError as exc: rows.append({"id": ident, "error": exc.code, "message": exc.message, "details": exc.data})
        return rows

    def wallet_validate(self, p):
        return {"address": address(p["address"])}

    def wallet_balance(self, p):
        chain, rpc = self.rpc(p)
        owner = self.owner(p)
        block = block_id(p.get("block", "latest"))
        if p.get("token"):
            amount = self.call(p, p["token"], "balanceOf(address)", [owner], ["uint256"], block)
            try:
                decimals = self.call(p, p["token"], "decimals()", [], ["uint8"], block)
            except ToolError:
                self.warnings.append("Token decimals unavailable; returning raw balance")
                decimals = None
        else:
            amount = rpc.read(lambda w: w.eth.get_balance(owner, block))
            decimals = chain["decimals"]
        return {"address": owner, "token": address(p["token"]) if p.get("token") else None,
                "balance_raw": str(amount), "decimals": decimals,
                "balance": formatted(amount, decimals) if decimals is not None else None}

    def wallet_balances(self, p):
        wallets = wallet_selectors(p.get("wallets", list(self.config.wallets)))
        tokens = p.get("tokens", [])
        require(isinstance(tokens, list), "tokens must be a JSON array")
        rows = []
        for wallet in wallets:
            for token in [None] + tokens:
                item = self.run("wallet", "balance", {"chain": p["chain"], "wallet": wallet, "token": token, "block": p.get("block", "latest")})
                rows.append(item)
        return rows

    def token_info(self, p):
        out = {"address": address(p["token"])}
        for name, typ in [("name", "string"), ("symbol", "string"), ("decimals", "uint8"), ("totalSupply", "uint256")]:
            try: out[name] = self.call(p, p["token"], name + "()", [], [typ])
            except ToolError:
                out[name] = None
                self.warnings.append(name + " unavailable")
        if out["decimals"] is not None and out["totalSupply"] is not None:
            out["supply"] = formatted(out["totalSupply"], out["decimals"])
        return out

    def token_convert(self, p):
        decimals = uint(p["decimals"])
        raw = self.amount(p, decimals)
        return {"amount_raw": str(raw), "amount": formatted(raw, decimals), "decimals": decimals}

    def transaction_inspect(self, p):
        _, rpc = self.rpc(p)
        hash_ = txhash(p["hash"])
        tx = dict(rpc.read(lambda w: w.eth.get_transaction(hash_)))
        tx["data"] = tx.pop("input", tx.get("data", "0x"))
        tx["data"] = Web3.to_hex(tx["data"]) if not isinstance(tx["data"], str) else hexdata(tx["data"])
        tx["selector"] = tx["data"][:10] if len(tx["data"]) >= 10 else None
        tx["data_bytes"] = (len(tx["data"]) - 2) // 2
        try: receipt = rpc.read(lambda w: w.eth.get_transaction_receipt(hash_))
        except ToolError as exc:
            if exc.code != "NOT_FOUND": raise
            receipt = None
        status, confirmations = "pending" if tx.get("blockNumber") is None else "receipt_unavailable", 0
        if receipt is not None:
            block = rpc.read(lambda w: w.eth.get_block(receipt["blockNumber"]))
            if block["hash"] != receipt["blockHash"]:
                status = "reorged"
            else:
                confirmations = max(0, rpc.read(lambda w: w.eth.block_number) - receipt["blockNumber"] + 1)
                status = "success" if receipt["status"] == 1 else "reverted"
        return {"transaction": tx, "receipt": receipt, "status": status, "confirmations": confirmations,
                "execution_fee_paid_wei": str(receipt["gasUsed"] * receipt.get("effectiveGasPrice", tx.get("gasPrice", 0))) if receipt else None}

    transaction_status = transaction_inspect

    def transaction_nonce(self, p):
        _, rpc = self.rpc(p)
        owner = self.owner(p)
        return {"address": owner, "latest": rpc.read(lambda w: w.eth.get_transaction_count(owner, "latest")),
                "pending": rpc.read(lambda w: w.eth.get_transaction_count(owner, "pending"))}

    def transaction_wait(self, p):
        chain, rpc = self.rpc(p)
        hash_=txhash(p["hash"])
        receipt = wait_receipt(rpc, hash_, p.get("confirmations", chain["confirmations"]), p.get("timeout", 120))
        from .engine import minted_nfts
        out={**dict(receipt),"hash":hash_,"receipt":receipt,"status":"confirmed" if receipt["status"]==1 else "reverted","minted_nfts":minted_nfts(receipt) if receipt["status"]==1 else []}
        if receipt["status"] != 1:
            from .engine import execution_revert_details
            out.update(execution_revert_details(rpc,hash_))
            raise ToolError("EXECUTION_REVERTED", "Canonical confirmed receipt reports execution failure", data=out)
        return out

    def transaction_decode(self, p):
        inspected = self.transaction_inspect(p)
        data = inspected["transaction"]["data"]
        from .analysis import Analyzer
        chain, rpc = self.rpc(p)
        analyzer = Analyzer(self.config, chain, rpc, p, inspected)
        try:
            decoded = analyzer.decode_input(data, inspected["transaction"].get("to"), p.get("abi"), p.get("signature"))
        except ToolError as exc:
            raise ToolError(exc.code, exc.message, data={"data": data, "inspection": inspected}) from None
        if decoded.get("status") not in ("decoded", "onchain_message", "data_only", "empty"):
            analyzer.issue("INPUT_UNRESOLVED", required=True)
        warnings, enhancements = analyzer.diagnostics()
        self.warnings.extend(warnings)
        return {"data": data, "decoded": decoded, "inspection": inspected,
                "analysis_status": "partial" if warnings else "complete", "enhancements_missing": enhancements}

    def transaction_analyze(self, p):
        import time
        from .analysis import Analyzer
        from .cache import SnapshotCache,analysis_key
        require(p.get("view","full") in ("summary","full"),"view must be summary or full")
        require(not(p.get("view")=="summary" and p.get("include_inputs")),"--include-inputs requires --view full; summary cannot carry unbounded calldata")
        self.config.chain(p.get("chain"));txhash(p["hash"])
        if p.get("focus_address"): address(p["focus_address"],field="focus_address")
        cache=SnapshotCache(self.config);key=analysis_key(self.config,p)
        hit=cache.get(key) if not p.get("no_cache") else None
        if hit:
            from .agent_output import transaction_conclusion
            from .calldata import call_depth
            hit["cached"]=True
            hit["cache"]["current_requests_used"]=0
            hit.update(transaction_conclusion(hit))
            for group in (hit.get("internal_calls", {}), hit.get("declared_calls", {})):
                for row in group.get("calls", []):
                    row["depth"] = call_depth(row)
            self.warnings.extend(hit.get("warnings",[]))
            return hit
        inspected = self.transaction_inspect(p)
        chain, rpc = self.rpc(p)
        result_ = Analyzer(self.config, chain, rpc, p, inspected).run()
        result_.update(cached=False,analyzed_at=time.time(),cache={"canonicality_checked_at":time.time() if result_["balance_changes"].get("canonicality")=="verified" else None,"current_requests_used":result_["capabilities"]["requests_used"],"status":"not_eligible"})
        if inspected.get("receipt") and result_["balance_changes"].get("canonicality")=="verified" and inspected["status"]!="reorged":
            ttl=300 if result_["analysis_status"]=="complete" and not result_["enhancements_missing"] and result_["fund_flows"]["status"]=="complete" else 60
            result_["cache"]["ttl_seconds"]=ttl
            result_["cache"]["status"]="stored"
            if not cache.put(key,result_,ttl): result_["cache"]["status"]="unavailable_or_oversize"
        if not cache.available:
            result_["enhancements_missing"].append({"capability":"cache","source":"local","reason":"unavailable","message":"Local analysis snapshot cache is unavailable"})
        self.warnings.extend(result_["warnings"])
        return result_

    def transaction_plan(self,p):
        from .planning import plan
        return plan(self,p)

    def address_history(self,p):
        from .history import history
        return history(self.config,self.config.chain(p.get("chain")),p)

    def address_actions(self,p):
        from .history import actions
        return actions(self.config,self.config.chain(p.get("chain")),p)

    def transaction_export_template(self, p):
        source = self.transaction_decode(p)
        tx = source["inspection"]["transaction"]
        require(tx.get("to"), "Contract creation cannot be exported as a call template")
        decoded = source["decoded"]
        if decoded.get("status") != "decoded" or not decoded.get("template_safe", False):
            self.warnings.append("Unconfirmed or ambiguous signature: exporting raw calldata only; provide explicit ABI/signature for editable parameters")
            decoded = {"status": "unknown", "selector": source["data"][:10] if len(source["data"])>=10 else None}
        template = {"schema_version": 1, "source": {"chain_id": self.config.chain(p["chain"])["chain_id"],
                    "hash": txhash(p["hash"]), "sender": tx["from"], "status": source["inspection"]["status"]},
                    "to": address(tx["to"]), "value_wei": str(tx["value"]), "data": source["data"], "decoded": decoded,
                    "warnings": ["Check recipient, deadline, wallet-bound signature/proof and current contract state before reuse"]}
        export_json(template, p.get("output"))
        return template

    def replacement(self, p, cancel=False):
        chain, rpc = self.rpc(p)
        wallet = self.wallet(p)
        tx = rpc.read(lambda w: w.eth.get_transaction(txhash(p["hash"])))
        require(tx.get("blockNumber") is None, "Only pending transactions can be replaced")
        require(address(tx["from"]) == wallet["address"], "Original sender differs from selected wallet")
        require(tx.get("to"), "Contract creation replacement unsupported")
        latest = rpc.read(lambda w: w.eth.get_transaction_count(wallet["address"], "latest"))
        require(tx["nonce"] >= latest, "Original Nonce already consumed")
        p["nonce"] = tx["nonce"]
        if tx.get("maxFeePerGas") is not None:
            require(p.get("gas_price_wei") is None, "Preserve original transaction fee type")
            for old, key in [("maxFeePerGas", "max_fee_per_gas_wei"), ("maxPriorityFeePerGas", "max_priority_fee_per_gas_wei")]:
                minimum = (tx[old] * 113 + 99) // 100 + 1
                p[key] = max(minimum, uint(p[key])) if p.get(key) is not None else minimum
        else:
            require(p.get("max_fee_per_gas_wei") is None and p.get("max_priority_fee_per_gas_wei") is None, "Preserve original transaction fee type")
            minimum = (tx["gasPrice"] * 113 + 99) // 100 + 1
            p["gas_price_wei"] = max(minimum, uint(p["gas_price_wei"])) if p.get("gas_price_wei") is not None else minimum
        to = wallet["address"] if cancel else address(tx["to"])
        data = "0x" if cancel else Web3.to_hex(tx.get("input", tx.get("data", b"")))
        return Engine(self.config, chain, rpc).send(wallet, to, 0 if cancel else tx["value"], data, p, replacement=True)

    def transaction_accelerate(self, p): return self.replacement(p)
    def transaction_cancel(self, p): return self.replacement(p, cancel=True)

    def transaction_replay(self, p):
        chain, rpc = self.rpc(p)
        return Engine(self.config, chain, rpc).replay(self.wallet(p), p["hash"], p)

    def contract_functions(self, p):
        abi=self.contract_abi(p)
        return [{"signature": canonical(i), "state_mutability": i.get("stateMutability"), "inputs": i.get("inputs", []), "outputs": i.get("outputs", [])}
                for i in abi if i.get("type") == "function"]

    def contract_abi(self,p):
        if p.get("abi") is not None:return load_abi(p["abi"])
        require(p.get("contract") and p.get("chain"),"Provide explicit ABI, or chain and contract for automatic ABI discovery","ABI_REQUIRED")
        from .introspect import discover_abi
        return discover_abi(self,p)[0]

    def contract_selector(self,p):
        from .abi import selector
        return {"signature":p["signature"],"selector":selector(p["signature"])}

    def contract_introspect(self,p):
        from .introspect import inspect_contract
        chain,rpc=self.rpc(p)
        return inspect_contract(self.config,chain,rpc,p)

    def contract_diagnose(self,p):
        from .introspect import inspect_contract
        chain,rpc=self.rpc(p)
        return inspect_contract(self.config,chain,rpc,p,diagnose=True)

    def contract_encode(self, p):
        item = resolve(self.contract_abi(p), p["function"])
        return {"signature": canonical(item), "data": encode_call(canonical(item), p.get("args", []))}

    def contract_decode(self, p):
        return decode_call(p["data"], abi=load_abi(p["abi"]))

    def contract_call(self, p):
        discovered=None
        try:abi = read_abi(p.get("abi"), p["function"])
        except ToolError as exc:
            if exc.code!="ABI_REQUIRED":raise
            from .introspect import discover_abi
            abi,discovered=discover_abi(self,p)
        item = resolve(abi, p["function"])
        if discovered:require(item.get("stateMutability") in ("view","pure"),"Automatic ABI calls are view/pure only; provide explicit ABI for a manually requested simulation","READ_ONLY_REQUIRED")
        data = encode_call(canonical(item), p.get("args", []))
        require(item.get("stateMutability") == "payable" or uint(p.get("value_wei", "0")) == 0, "Nonpayable function cannot receive value")
        _, rpc = self.rpc(p)
        call = {"to": address(p["contract"]), "data": data, "value": uint(p.get("value_wei", "0"))}
        if p.get("address") or p.get("wallet"): call["from"] = self.owner(p)
        if discovered:
            discovered.account=call.get("from")
            row=discovered.read(p["contract"],canonical(item),p.get("args",[]))
            if row.get("status")!="success":raise ToolError("CONTRACT_READ_FAILED","Discovered ABI view call failed",data={"probe":row,"structure":discovered.structure(p["contract"]),"block":{"number":str(int(discovered.block,16)),"hash":discovered.block_hash}})
            discovered.canonical_check();require(discovered.canonicality=="verified","Pinned block could not be rechecked","CANONICALITY_UNAVAILABLE")
            return {"return_data":row["raw"],"values":row["values"],"block":{"number":str(int(discovered.block,16)),"hash":discovered.block_hash},"abi_source":discovered.structure(p["contract"])["effective_abi_source"]}
        require(rpc.read(lambda w: w.eth.get_code(address(p["contract"]), block_id(p.get("block", "latest")))), "Address has no contract code at selected block", "NO_CONTRACT")
        output = rpc.read(lambda w: w.eth.call(call, block_id(p.get("block", "latest"))))
        from eth_utils.abi import collapse_if_tuple
        types = [collapse_if_tuple(o) for o in item.get("outputs", [])]
        try: values = decode(types, output)
        except Exception: raise ToolError("DECODE_ERROR", "Return data cannot be decoded") from None
        return {"return_data": output, "values": values}

    def contract_write(self, p):
        abi = load_abi(p["abi"])
        item = resolve(abi, p["function"])
        value = uint(p.get("value_wei", "0"))
        require(item.get("stateMutability") == "payable" or value == 0, "Nonpayable function cannot receive value")
        self.contract_code(p, p["contract"])
        try:
            return self.send(p, p["contract"], value, encode_call(canonical(item), p.get("args", [])))
        except ToolError as exc:
            if exc.data and exc.data.get("revert_data"):
                exc.data.update(decode_revert(exc.data["revert_data"], abi))
            raise

    def transfer_native(self, p):
        chain, _ = self.rpc(p)
        value = self.amount(p, chain["decimals"])
        require(value > 0, "Transfer amount must be positive")
        return self.send(p, p["to"], value)

    def transfer_erc20(self, p):
        amount = self.amount(p, self.token_decimals(p)) if p.get("amount_raw") is None else self.amount(p, 0)
        require(amount > 0, "Transfer amount must be positive")
        owner = self.wallet(p)["address"]
        balance = self.call(p, p["token"], "balanceOf(address)", [owner], ["uint256"], "pending")
        require(balance >= amount, "Insufficient ERC-20 balance", "INSUFFICIENT_BALANCE")
        return self.send(p, p["token"], data=encode_call("transfer(address,uint256)", [address(p["to"]), amount]), erc20=True)

    def approve_allowance(self, p):
        return {"allowance_raw": str(self.call(p, p["token"], "allowance(address,address)", [self.owner(p), address(p["spender"])], ["uint256"]))}

    def approve_set(self, p):
        require(not p.get("unlimited") or (p.get("amount") is None and p.get("amount_raw") is None), "Unlimited and explicit amount are mutually exclusive")
        amount = 2**256 - 1 if p.get("unlimited") else self.amount(p, self.token_decimals(p) if p.get("amount_raw") is None else 0)
        self.contract_code(p, p["token"])
        spender = address(p["spender"])
        if p.get("zero_first"):
            first = dict(p, wait=True)
            zero = self.send(first, p["token"], data=encode_call("approve(address,uint256)", [spender, 0]), erc20=True)
            # Preview the second step without simulating against pre-reset state.
            if not p.get("execute"):
                return {"steps": [zero, {"status": "requires_prior_confirmation", "to": address(p["token"]), "data": encode_call("approve(address,uint256)", [spender, amount])}]}
            try:
                second = self.send(p, p["token"], data=encode_call("approve(address,uint256)", [spender, amount]), erc20=True)
            except ToolError as exc:
                raise ToolError(exc.code, exc.message, retryable=exc.retryable, data={"completed_steps": [zero], "failed_step": 2, "details": exc.data}) from None
            return {"steps": [zero, second]}
        return self.send(p, p["token"], data=encode_call("approve(address,uint256)", [spender, amount]), erc20=True)

    def approve_revoke(self, p):
        return self.approve_set(dict(p, amount_raw="0"))

    def nft_info(self, p):
        standard = p["standard"]
        require(standard in ("721", "1155"), "standard must be 721 or 1155")
        owner = self.owner(p)
        out = {"standard": standard, "address": owner}
        if standard == "721":
            out["balance"] = self.call(p, p["contract"], "balanceOf(address)", [owner], ["uint256"])
            if p.get("token_id") is not None:
                ident = uint(p["token_id"])
                out["owner"] = self.call(p, p["contract"], "ownerOf(uint256)", [ident], ["address"])
                out["approved"] = self.call(p, p["contract"], "getApproved(uint256)", [ident], ["address"])
                try: out["uri"] = self.call(p, p["contract"], "tokenURI(uint256)", [ident], ["string"])
                except ToolError: self.warnings.append("tokenURI unavailable")
        else:
            ident = uint(p["token_id"])
            out["balance"] = self.call(p, p["contract"], "balanceOf(address,uint256)", [owner, ident], ["uint256"])
            try: out["uri"] = self.call(p, p["contract"], "uri(uint256)", [ident], ["string"])
            except ToolError: self.warnings.append("uri unavailable")
        if p.get("operator"):
            out["operator_approved"] = self.call(p, p["contract"], "isApprovedForAll(address,address)", [owner, address(p["operator"])], ["bool"])
        return out

    def nft_balance_batch(self, p):
        require(len(p["addresses"]) == len(p["token_ids"]), "addresses and token_ids lengths differ")
        return self.call(p, p["contract"], "balanceOfBatch(address[],uint256[])", [p["addresses"], p["token_ids"]], ["uint256[]"])

    def nft_transfer(self, p):
        self.contract_code(p, p["contract"])
        args = [self.wallet(p)["address"], address(p["to"]), uint(p["token_id"])]
        require(p["standard"] in ("721", "1155"), "standard must be 721 or 1155")
        if p["standard"] == "721":
            signature = "safeTransferFrom(address,address,uint256)"
        else:
            signature = "safeTransferFrom(address,address,uint256,uint256,bytes)"
            quantity = uint(p.get("quantity", "1"))
            require(quantity > 0, "Quantity must be positive")
            args.extend([quantity, p.get("data", "0x")])
        return self.send(p, p["contract"], data=encode_call(signature, args))

    def nft_transfer_batch(self, p):
        self.contract_code(p, p["contract"])
        require(len(p["token_ids"]) == len(p["quantities"]) > 0, "IDs and quantities must have equal nonzero lengths")
        require(all(uint(q) > 0 for q in p["quantities"]), "Quantities must be positive")
        args = [self.wallet(p)["address"], address(p["to"]), p["token_ids"], p["quantities"], p.get("data", "0x")]
        return self.send(p, p["contract"], data=encode_call("safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)", args))

    def nft_approve(self, p):
        self.contract_code(p, p["contract"])
        require(not (p.get("all") and p.get("token_id") is not None), "Operator-wide and token-specific approval are mutually exclusive")
        if p.get("all"):
            args = [address(p["operator"]), p.get("approved", True)]
            signature = "setApprovalForAll(address,bool)"
        else:
            require(p.get("approved", True), "Use revoke for token approval revocation")
            args = [address(p["operator"]), uint(p["token_id"])]
            signature = "approve(address,uint256)"
        return self.send(p, p["contract"], data=encode_call(signature, args))

    def nft_revoke(self, p):
        if p.get("all"): return self.nft_approve(dict(p, approved=False))
        return self.nft_approve(dict(p, operator="0x" + "0" * 40))

    def nft_mint(self, p):
        require((p.get("abi") is None) != (p.get("signature") is None), "Provide ABI/function or signature for Mint")
        if p.get("abi") is not None: return self.contract_write(p)
        self.contract_code(p, p["contract"])
        return self.send(p, p["contract"], uint(p.get("value_wei", "0")), encode_call(p["signature"], p.get("args", [])))

    def raw_encode(self, p): return {"data": encode_call(p["signature"], p.get("args", []))}

    def raw_patch(self,p):
        from .calldata import patch_word
        require(p.get("verify") is True,"raw.patch requires --verify (or verify=true); patching without simulation is not supported")
        chain,rpc=self.rpc(p);sender=self.owner(p);target=address(p["to"])
        patched,diff=patch_word(p["data"],p["word"],p["value"],p.get("type","bytes32"))
        value=uint(p.get("value_wei","0"),"value_wei")
        notes=["payload_unverified: whole-word edits do not prove ABI structure or business semantics", "A successful eth_call is not a guarantee of intended effects or future execution"]
        template={"schema_version":1,"source":{"chain_id":chain["chain_id"],"sender":sender,"kind":"raw_patch"},"to":target,"value_wei":str(value),
                  "data":patched,"decoded":{"status":"unknown","selector":patched[:10]},"payload_unverified":True,"warnings":notes}
        out={"status":"verified_preview","data":patched,"payload_unverified":True,"parameter_diff":[diff],"template":template,
             "verification":{"status":"pending","from":sender,"to":target,"value_wei":str(value),"block":"pending"},"warnings":notes}
        try:
            returned=rpc.read(lambda w:w.eth.call({"from":sender,"to":target,"value":value,"data":patched},"pending"))
            out["verification"].update(status="success",return_data=jsonable(returned))
        except ToolError as exc:
            out["status"]="blocked";out["verification"].update(status="failed",reason=exc.code,details=exc.data)
            code="SIMULATION_REVERTED" if exc.code=="CALL_REVERTED" else exc.code
            out["verification"]["reason"]=code
            raise ToolError(code,exc.message,retryable=exc.retryable,data=out) from None
        return out

    def raw_decode(self, p):
        if p.get("output_types") is not None:
            try: return {"values": decode(p["output_types"], bytes.fromhex(hexdata(p["data"])[2:]))}
            except Exception: raise ToolError("DECODE_ERROR", "Cannot decode output types") from None
        return decode_call(p["data"], p.get("signature"), load_abi(p["abi"]) if p.get("abi") is not None else None)

    def raw_template(self, p): return validate_template(p["template"])

    def raw_reencode(self, p):
        template = validate_template(p["template"])
        decoded = template["decoded"]
        require(decoded.get("status") == "decoded", "Template has no known function signature")
        template["data"] = encode_call(decoded["signature"], p["args"])
        template["decoded"] = decode_call(template["data"], decoded["signature"])
        export_json(template, p.get("output"))
        return template

    def raw_input(self, p):
        if p.get("template") is not None:
            template = validate_template(p["template"])
            chain = self.config.chain(p["chain"])
            require(uint(template["source"]["chain_id"]) == chain["chain_id"], "Template belongs to another chain")
            require(p.get("data") is None and p.get("signature") is None and p.get("args") is None, "Reencode template before changing its calldata")
            if template.get("warnings"): self.warnings.extend(template["warnings"])
            if template.get("payload_unverified"):
                self.warnings.append("payload_unverified: calldata structure and intended effects remain unconfirmed; simulate before execution")
            return address(p.get("to", template["to"])), uint(p.get("value_wei", template["value_wei"])), template["data"]
        require((p.get("data") is None) != (p.get("signature") is None), "Provide exactly one of data or signature")
        require(p.get("data") is None or p.get("args") is None, "args requires signature")
        data = hexdata(p["data"]) if p.get("data") is not None else encode_call(p["signature"], p.get("args", []))
        return address(p["to"]), uint(p.get("value_wei", "0")), data

    def raw_call(self, p):
        to, value, data = self.raw_input(p)
        _, rpc = self.rpc(p)
        tx = {"to": to, "value": value, "data": data}
        if p.get("address") or p.get("wallet"): tx["from"] = self.owner(p)
        output = rpc.read(lambda w: w.eth.call(tx, block_id(p.get("block", "latest"))))
        out = {"return_data": output}
        if p.get("output_types") is not None:
            try: out["values"] = decode(p["output_types"], output)
            except Exception: raise ToolError("DECODE_ERROR", "Cannot decode returned data", data=out) from None
        return out

    def raw_write(self, p):
        to, value, data = self.raw_input(p)
        result_=self.send(p, to, value, data)
        if p.get("template") is not None and validate_template(p["template"]).get("payload_unverified"):
            result_["payload_unverified"]=True
        return result_

    def signing_message(self, p): return self.sign(p, encode_defunct(text=p["message"]), p["message"])

    def signing_typed(self, p):
        typed = read_json(p["typed_data"]) if isinstance(p["typed_data"], str) else p["typed_data"]
        chain_id = typed.get("domain", {}).get("chainId")
        if chain_id is not None:
            require(p.get("chain"), "Select a chain when typed data declares chainId")
            require(uint(chain_id) == self.config.chain(p["chain"])["chain_id"], "Typed data Chain ID differs")
        return self.sign(p, encode_typed_data(full_message=typed), typed)

    def sign(self, p, message, content):
        wallet = self.wallet(p)
        out = {"status": "preview", "address": wallet["address"], "content": content,
               "message_hash": Web3.to_hex(Web3.keccak(b"\x19" + message.version + message.header + message.body))}
        if p.get("execute"):
            signed = wallet["account"].sign_message(message)
            out.update(status="signed", signature=Web3.to_hex(signed.signature))
        return out

    def verify(self, p, message):
        try: recovered = Account.recover_message(message, signature=hexdata(p["signature"]))
        except Exception: raise ToolError("INVALID_SIGNATURE", "Cannot recover signer") from None
        matches = recovered == address(p["address"]) if p.get("address") else None
        require(matches is not False, "Signature does not match expected address", "SIGNATURE_MISMATCH")
        return {"address": recovered, "matches": matches}

    def signing_verify_message(self, p): return self.verify(p, encode_defunct(text=p["message"]))
    def signing_verify_typed(self, p):
        typed = read_json(p["typed_data"]) if isinstance(p["typed_data"], str) else p["typed_data"]
        return self.verify(p, encode_typed_data(full_message=typed))

    def events_read(self, p):
        chain, rpc = self.rpc(p)
        target = address(p["contract"])
        latest = rpc.read(lambda w: w.eth.block_number)
        safe = max(0, latest - chain["confirmations"] + 1)
        end = uint(p["to_block"]) if p.get("to_block") is not None else safe
        start = uint(p["from_block"])
        require(start <= end <= latest, "Invalid event block range")
        if end > safe:
            require(p.get("include_unconfirmed"), "Recent blocks require include_unconfirmed")
            self.warnings.append("Recent event logs may be affected by a reorganization")
        size = uint(p.get("chunk_size", 2000))
        require(0 < size <= 10000, "chunk_size must be 1..10000")
        topics = p.get("topics", [])
        require(isinstance(topics, list) and len(topics) <= 4, "topics must be an array of at most four filters")
        for topic in topics:
            for value in topic if isinstance(topic, list) else [topic]:
                if value is not None: txhash(value)
        logs, completed = {}, start - 1
        while start <= end:
            stop = min(start + size - 1, end)
            query = {"address": target, "fromBlock": start, "toBlock": stop, "topics": topics}
            try: rows = rpc.read(lambda w: w.eth.get_logs(query))
            except ToolError as exc:
                if size > 1 and exc.code in ("RPC_UNAVAILABLE", "RPC_REQUEST_REJECTED"):
                    size = max(1, size // 2)
                    continue
                raise ToolError("LOG_QUERY_INCOMPLETE", "Event query did not complete", retryable=True,
                                data={"completed_through": completed, "logs": list(logs.values())}) from None
            for row in rows:
                key = (bytes(row["blockHash"]), bytes(row["transactionHash"]), row["logIndex"])
                logs[key] = dict(row)
            completed, start = stop, stop + 1
        ordered = sorted(logs.values(), key=lambda i: (i["blockNumber"], i["transactionIndex"], i["logIndex"]))
        from .analysis import Analyzer
        analyzer = Analyzer(self.config, chain, rpc, p)
        block_memos = {}
        for index, row in enumerate(ordered):
            if index >= analyzer.options["max_frames"]:
                row["decoded"] = {"status": "unavailable", "reason": "Event enrichment limit"}
                analyzer.issue("EVENTS_TRUNCATED", required=True)
                continue
            analyzer.block = hex(row["blockNumber"])
            analyzer.memo = block_memos.setdefault(analyzer.block, {})
            analyzer.memo["block_hash"]=Web3.to_hex(row["blockHash"]) if not isinstance(row["blockHash"],str) else row["blockHash"]
            row["decoded"] = analyzer.attempt(lambda: analyzer.decode_event(row, p.get("abi")), "EVENT_DECODE", required=True) or {"status": "unknown", "event": None, "args": None}
            if row["decoded"].get("status") != "decoded": analyzer.issue("EVENTS_UNRESOLVED", required=True)
        warnings, enhancements = analyzer.diagnostics()
        self.warnings.extend(warnings)
        return {"logs": ordered, "completed_through": completed, "complete": True,
                "analysis_status": "partial" if warnings else "complete", "enhancements_missing": enhancements,
                "enrichment_status": "partial" if warnings or enhancements else "complete"}

    def batch_run(self, p):
        require((p.get("items") is None) != (p.get("file") is None), "Provide items or file")
        items = read_json(p["file"]) if p.get("file") else p["items"]
        require(isinstance(items, list), "Batch must be an array")
        ids = set()
        # Validate all requests before sending any transaction.
        for item in items:
            require(isinstance(item, dict) and set(item) == {"id", "module", "operation", "params"}, "Invalid batch item shape")
            require(isinstance(item["id"], str) and item["id"] and item["id"] not in ids, "Batch IDs must be unique strings")
            ids.add(item["id"])
            module, op, params = item["module"], item["operation"], item["params"]
            require(module in OPS and module != "batch" and op in OPS[module], "Invalid batch operation")
            require(isinstance(params, dict) and not (set(params) - OPS[module][op]), "Invalid batch params")
            require(not params.get("execute"), "Execution is controlled by the batch, not individual items")
            require(params.get("output") is None, "Batch items cannot export files")
        rows, stopped, uncertain, any_failed = [], False, set(), False
        for item in items:
            q = dict(item["params"])
            lane = None
            if q.get("wallet") and q.get("chain"):
                try: lane = (self.config.chain(q["chain"])["chain_id"], self.config.wallet(q["wallet"])["address"])
                except ToolError: pass
            writing = "execute" in OPS[item["module"]][item["operation"]]
            if stopped or (writing and lane in uncertain):
                rows.append({"id": item["id"], "status": "not_executed"})
                continue
            if writing:
                q["execute"] = p.get("execute", False)
                if "wait" in OPS[item["module"]][item["operation"]]: q["wait"] = True
            response = self.run(item["module"], item["operation"], q)
            rows.append({"id": item["id"], "status": "completed" if response["ok"] else "failed", "result": response})
            if not response["ok"]:
                any_failed = True
                if (response["error"]["code"] in ("BROADCAST_UNKNOWN", "CONFIRMATION_TIMEOUT", "CONFIRMATION_UNAVAILABLE", "RPC_UNAVAILABLE") or (response.get("data") or {}).get("status") in ("broadcast_unknown","broadcast_unconfirmed")) and lane:
                    uncertain.add(lane)
                stopped = not p.get("continue_on_error", False)
        out = {"items": rows, "status": "partial_failure" if any_failed else "completed"}
        if any_failed: raise ToolError("BATCH_FAILED", "One or more batch items failed", data=out)
        return out


def block_id(value):
    if value in ("latest", "pending", "earliest", "safe", "finalized"): return value
    return uint(value, "block")


def export_json(data, path):
    if path:
        try:
            with Path(path).open("x") as handle:
                json.dump(jsonable(data), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except OSError:
            raise ToolError("FILE_ERROR", "Cannot create output file; existing files are not overwritten") from None


def validate_template(value):
    template = read_json(value) if isinstance(value, str) else value
    require(isinstance(template, dict) and template.get("schema_version") == 1, "Unsupported template version")
    require(isinstance(template.get("source"), dict), "Template source missing")
    uint(template["source"]["chain_id"])
    address(template["to"])
    uint(template["value_wei"])
    data = hexdata(template["data"])
    decoded = template.get("decoded", {"status": "unknown"})
    require(isinstance(decoded, dict), "Invalid decoded template data")
    if decoded.get("status") == "decoded":
        require(encode_call(decoded["signature"], decoded["args"]) == data, "Decoded parameters and calldata disagree; reencode the template")
    else:
        require(decoded.get("status") == "unknown", "Invalid decoding status")
    require(isinstance(template.get("warnings", []), list) and all(isinstance(i, str) for i in template.get("warnings", [])), "Invalid template warnings")
    if "payload_unverified" in template:require(isinstance(template["payload_unverified"],bool),"payload_unverified must be boolean")
    return json.loads(json.dumps(template))

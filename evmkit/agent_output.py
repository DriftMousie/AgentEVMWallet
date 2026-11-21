"""Actionable diagnostics and a hard byte-bounded conclusion view."""
import json
from web3 import Web3


def suggested_next(params, data=None, code=None):
    params=params if isinstance(params,dict) else {}
    data=data if isinstance(data,dict) else {}
    chain,hash_=params.get("chain"),data.get("hash") or params.get("hash") or params.get("from_trace")
    steps=[]
    if code == "DEPENDENCY_MISSING":
        steps.append({"action":"install_project_dependencies", "command":"python -m pip install -r requirements.txt",
                      "reason":"Use the same Python interpreter that runs the toolkit"})
    if code == "SENDER_REQUIRED":
        steps.append({"action":"supply_plan_sender", "reason":"Provide a public address or a configured wallet for sender-dependent overrides"})
    if code in ("RPC_UNAVAILABLE","CANONICALITY_UNAVAILABLE") and chain and params.get("address") and not hash_:
        steps.append({"module":"contract","operation":"introspect","params":{"chain":chain,"address":params['address'],"view":"summary","no_cache":True},"reason":"Restore a working RPC, then retry this read-only pinned-block analysis"})
    if code in ("ABI_REQUIRED","ABI_UNAVAILABLE") and chain and (params.get("contract") or params.get("address")):
        steps.append({"action":"check_verified_sources","files":["config/explorers.toml","config/explorers.local.toml"],"reason":"Enable an available verification provider or supply a reliable explicit ABI; a preset is not verification"})
    if code in ("CONFIRMATION_TIMEOUT","CONFIRMATION_UNAVAILABLE","BROADCAST_UNKNOWN") and chain and hash_:
        steps.append({"action":"transaction.wait","module":"transaction","operation":"wait","params":{"chain":chain,"hash":hash_},
                      "reason":"Query the known original hash; a confirmation error does not authorize another send"})
    if code=="BROADCAST_UNKNOWN" and chain and data.get("previous_hash"):
        steps.append({"module":"transaction","operation":"inspect","params":{"chain":chain,"hash":data["previous_hash"]},"reason":"Resolve the earlier recorded transaction before preparing a new one"})
    error_details = data
    for name in ("simulation", "verification"):
        stage = data.get(name)
        if isinstance(stage, dict) and isinstance(stage.get("details"), dict):
            error_details = stage["details"]
            break
    if error_details.get("error_lookup") == "not_found" and error_details.get("error_selector"):
        steps.append({"action":"load_verified_error_abi",
                      "reason":"Get the verified contract ABI, including its error entries, from Etherscan, Blockscout or Sourcify. If the call forwarded the error, check the inner target ABI and retry in preview mode."})
    if code == "SIMULATION_INCONCLUSIVE":
        steps.append({"action":"retry_readonly_preview",
                      "reason":"Check the backup RPC and confirmation of prerequisite transactions, then repeat the preview without executing."})
    if "layout" in params or any(key in params for key in ("slot", "slot_hex", "mapping_key", "base_slot")):
        if code in ("SOURCE_UNAUTHORIZED","SOURCE_UNAVAILABLE","OPTIONAL_RPC_REJECTED","CANONICALITY_UNAVAILABLE"):
            steps.append({"action":"check_storage_rpc", "files":["config/rpc.csv"],
                          "reason":"Use an enabled RPC that supports eth_getStorageAt at the requested height; older blocks may require archive access."})
    if code in ("TOKEN_ID_AUTO_UNSUPPORTED","UNSUPPORTED_OVERRIDE"):
        steps.append({"action":"check_nft_creation_interface","reason":"Use a confirmed creation ABI; contract-assigned IDs are read from the successful receipt. Existing NFT IDs and lock units must be supplied explicitly"})
    if code in ("CONTEXT_DEPENDENT_CALL","CALL_SELECTION_AMBIGUOUS","CALL_NOT_FOUND","CALL_INPUT_UNAVAILABLE","CALL_CONTEXT_UNSUPPORTED"):
        steps.append({"action":"inspect_call_candidates","path":"data.candidates" if data.get("candidates") else "data.source_call",
                      "reason":"Choose an ordinary business CALL using its exact trace path or declared execution index; never automatically choose the first match or copy authorization context"})
    if code in ("CALL_TRACE_UNAVAILABLE","CALL_SELECTION_INCOMPLETE","CALL_INPUT_UNAVAILABLE","CALL_NOT_FOUND","CONTEXT_DEPENDENT_CALL") and chain and hash_:
        steps.append({"module":"transaction","operation":"analyze","params":{"chain":chain,"hash":hash_,"view":"full","include_inputs":True},
                      "reason":"Inspect available raw calls and distinguish observed traces from encoded declarations"})
    if code in ("ABI_REQUIRED","FIELD_SELECTION_AMBIGUOUS"):
        steps.append({"module":"transaction","operation":"plan","params":{"describe":True},
                      "reason":"Provide a confirmed ABI or an explicitly established typed field_map; do not guess offsets, recipients or time units"})
    inspected=data.get("inspection",{})
    tx=inspected.get("transaction",{})
    decoded=data.get("input_decode",data.get("decoded",{})) or {}
    for item in decoded.get("candidates",[])[:3]:
        signature=item.get("signature")
        if signature and tx.get("data") and Web3.to_hex(Web3.keccak(text=signature)[:4])==tx["data"][:10]:
            steps.append({"module":"raw","operation":"decode","params":{"data":tx["data"],"signature":signature},
                          "reason":"Try a selector-matching candidate; decoded arguments do not prove semantics"})
    if tx.get("to") and chain and decoded.get("status") not in ("decoded","empty","onchain_message","data_only"):
        steps.append({"module":"address","operation":"history","params":{"chain":chain,"address":tx["to"],"direction":"in","limit":50},
                      "requires":["configured_explorer"],"reason":"Inspect recent callers/selectors without inventing a function name"})
    if data.get("fund_flows"):
        steps.append({"action":"inspect_fund_flows","path":"data.fund_flows","reason":"Observed asset movements remain useful independently of the unknown function"})
    if code in ("SOURCE_UNCONFIGURED","CONFIG_ERROR","SOURCE_UNAUTHORIZED"):
        steps.append({"action":"check_configuration","files":["config/settings.toml","config/explorers.toml","config/explorers.local.toml"],
                      "reason":"Check enabled provider, required local key and account permissions; never print credential values"})
    if code in ("CALL_REVERTED","SIMULATION_REVERTED","TOKEN_OPERATION_FAILED") and data.get("template"):
        steps.append({"action":"inspect_plan_diff","reason":"Check recipient, owner, deadline, wallet-bound proof/signature and current balances; do not bypass the failed simulation"})
    if code in ("RPC_UNAVAILABLE","SOURCE_TIMEOUT","SOURCE_RATE_LIMITED") and chain and hash_:
        steps.append({"module":"transaction","operation":"analyze","params":{"chain":chain,"hash":hash_,"no_cache":True,"view":"summary"},
                      "reason":"Retry the read after restoring provider availability or waiting for the rate limit"})
    if not steps:
        steps.append({"action":"inspect_parameters","reason":"Check the documented required fields and the exact error; do not guess addresses, signatures or monetary units"})
    return steps[:5]


def _quantity(raw,decimals,symbol,location=None):
    value=int(raw)
    if decimals is None:
        text=str(abs(value))+" raw"
    elif decimals==0:
        text=str(abs(value))
    else:
        digits=str(abs(value)).zfill(decimals+1)
        text=(digits[:-decimals]+"."+digits[-decimals:]).rstrip("0").rstrip(".")
    label=(symbol or "?")[:24]
    if location: label += " @"+location[:8]+"…"+location[-4:]
    return ("+" if value>0 else "-" if value<0 else "")+text+" "+label


def transaction_conclusion(original, default_status="partial"):
    """Build the same user-facing totals for all analysis output views."""
    flows = original.get("fund_flows", {})
    native = flows.get("native", {})
    classification = original.get("classification", {})
    inferred = classification.get("inferred_intent") or {}
    tokens, nfts = flows.get("tokens", []), flows.get("nfts", [])
    net=[];got=[]
    if native.get("net_wei") is not None and int(native["net_wei"])!=0:
        target=net if int(native["net_wei"])>0 else got
        target.append(_quantity(-int(native["net_wei"]),native["decimals"],native.get("symbol")))
    for row in tokens:
        value=int(row["net"])
        if value: (got if value>0 else net).append(_quantity(value,row.get("decimals"),row.get("symbol"),row["address"]))
    for row in nfts:
        value=int(row["net"])
        if value: (got if value>0 else net).append(("+" if value>0 else "-")+str(abs(value))+" NFT #"+row["token_id"]+" @"+row["address"])
    status=original.get("analysis_status", default_status)
    return {"status":status,
          "intent":inferred.get("value") if classification.get("type") in (None,"unknown","contract_call") else classification["type"],
          "intent_confidence":inferred.get("confidence") or classification.get("confidence"),
          "net":"; ".join(net) if net else None,"got":"; ".join(got) if got else None,
          "fee":_quantity(native["fee_wei"],native["decimals"],native.get("symbol")) if native.get("fee_wei") is not None else None}


def summary_envelope(response):
    """Bound the complete serialized CLI envelope, including its trailing newline."""
    original=response.get("data") or {}
    original=original if isinstance(original,dict) else {}
    flows=original.get("fund_flows",{})
    native=flows.get("native",{})
    tokens=flows.get("tokens",[]);nfts=flows.get("nfts",[])
    refunds=flows.get("refunds",[])
    decoded=original.get("input_decode",{})
    unknowns=[]
    if native.get("status")=="partial": unknowns.append("native_flow:"+native.get("reconciliation","incomplete"))
    if decoded.get("status") not in (None,"decoded","empty","onchain_message","data_only"):
        unknowns.append("function_selector_"+str(decoded.get("selector") or "unknown"))
    custom=sum(e.get("decoded",{}).get("status")!="decoded" for e in original.get("decoded_events",[]))
    if custom: unknowns.append(str(custom)+" unresolved events")
    unknowns.extend(dict.fromkeys(str(w) for w in original.get("warnings",[])+response.get("warnings",[])))
    unknowns.extend(dict.fromkeys(e["capability"]+":"+e["reason"] for e in original.get("enhancements_missing",[])))
    risks=sorted(original.get("risks",[]),key=lambda r:{"critical":0,"high":1,"warning":2,"medium":3,"info":4}.get(r.get("severity"),5))
    conclusion = {key: original[key] for key in ("status", "intent", "intent_confidence", "net", "got", "fee")
                  if key in original}
    if len(conclusion) != 6:
        conclusion = transaction_conclusion(original, "failed" if not response["ok"] else "partial")
    body={"view":"summary", **conclusion, "analysis_status":conclusion["status"],
          "fee_scope":"EVM execution only",
          "fund_flows":{"status":flows.get("status","unavailable"),"focus_address":flows.get("focus_address"),
                        "native":{k:native.get(k) for k in ("net_wei","fee_wei","balance_delta_wei")},
                        "tokens":[{k:r.get(k) for k in ("address","decimals","net","role")} for r in tokens],
                        "nfts":[{k:r.get(k) for k in ("address","token_id","net")} for r in nfts]},
          "token_count":len(tokens),"nft_count":len(nfts),"risk_count":len(risks),"unknowns_count":len(unknowns),"refund_count":len(refunds),
          "refunds":[{k:r.get(k) for k in ("address","amount_raw","kind")} for r in refunds],
          "risks":[{"code":r["code"],"severity":r.get("severity")} for r in risks],"unknowns":unknowns,
          "next":"Inspect full fund_flows, risks and unknowns before reusing calldata", "truncated":False,
          "cached":original.get("cached",False),"analyzed_at":original.get("analyzed_at")}
    error=response.get("error")
    if error: error={k:error.get(k) for k in ("code","message","retryable")}
    out={**response,"wallet":None,"data":body,"warnings":[],"error":error}
    def size():return len((json.dumps(out,ensure_ascii=False,separators=(",",":"))+"\n").encode("utf-8"))
    if error: error["message"]=str(error.get("message", ""))[:160];error["suggested_next"]=[{"action":"inspect_error_and_full_view"}]
    buckets=[body["refunds"],body["fund_flows"]["tokens"],body["fund_flows"]["nfts"],body["unknowns"],body["risks"]]
    while size()>2048:
        body["truncated"]=True
        bucket=next((b for b in buckets if b),None)
        if bucket is not None: bucket.pop();continue
        if body.get("net") is not None or body.get("got") is not None:
            body.pop("net", None);body.pop("got", None);continue
        body.pop("fee", None)
        if error: error["message"]="See full view for error details"
        if size()>2048:
            body={k:body[k] for k in ("view","status","analysis_status","intent","risk_count","unknowns_count","token_count","nft_count","refund_count","truncated","next")}
            out["data"]=body
        break
    return out

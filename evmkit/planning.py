"""Reference transaction or declared/observed inner CALL to an unsigned live preview."""
import re
import time
from copy import deepcopy
from .abi import canonical,decode_call,encode_call
from .common import ToolError,address,require,txhash,uint
from .engine import Engine
from .standards import V2_ROUTER
from .calldata import named_fields,patch_word,resolve_time,timing,validate_field_map,input_fields,parameter_schema,call_depth,expanded_input
from .delegation import REDEEM_SELECTOR
from .error_details import diagnostic_error

RECIPIENTS={"transfer(address,uint256)":0,"transferFrom(address,address,uint256)":1,
            "safeTransferFrom(address,address,uint256)":1,"safeTransferFrom(address,address,uint256,bytes)":1,
            "safeTransferFrom(address,address,uint256,uint256,bytes)":1,
            "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)":1,
            "deposit(uint256,address)":1,"mint(uint256,address)":1,
            "withdraw(uint256,address,address)":1,"redeem(uint256,address,address)":1}
LOCK_CREATION={"createLock(uint256,uint256)","create_lock(uint256,uint256)","createLockFor(uint256,uint256,address)","create_lock_for(uint256,uint256,address)"}
AMOUNTS={"transfer(address,uint256)":1,"deposit(uint256,address)":0,"mint(uint256,address)":0,**{sig:0 for sig in LOCK_CREATION}}
RECIPIENTS.update({sig:2 for sig in LOCK_CREATION if sig.endswith(",address)")})
for item in V2_ROUTER:
    names=[p["name"] for p in item["inputs"]]
    RECIPIENTS[canonical(item)]=names.index("to")
    if "amountIn" in names: AMOUNTS[canonical(item)]=names.index("amountIn")


def _candidates(rows,include_inputs=False,transaction=None,now=None):
    out=[]
    for i,row in enumerate(rows):
        item={k:row.get(k) for k in ("path","execution_index","source","type","to","value_wei","committed")}
        item.update(input_fields(row),candidate_index=i,depth=call_depth(row))
        if include_inputs:
            item.update(expanded_input(row, transaction or {}, now if now is not None else int(time.time())))
        out.append(item)
    return out


def candidate_rows(analysis):
    declared=analysis.get("declared_calls",{});internals=analysis.get("internal_calls",{})
    if declared.get("status") in ("complete","partial"):
        return declared.get("calls",[]),declared["status"]=="complete"
    if internals.get("source")=="rpc.callTracer":
        rows=[dict(r,source="rpc.callTracer") for r in internals.get("calls",[]) if r.get("path") and r.get("type")=="CALL" and r.get("to") and int(r["to"],16)>0x11 and r.get("committed") is True and isinstance(r.get("input"),str)]
        return sorted(rows,key=lambda r:r["path"]),internals.get("complete") is True
    return [],False


def candidate_selector(row):
    return "execution:"+str(row["execution_index"]) if row.get("source")=="encoded_execution" else "path:"+".".join(str(i) for i in row["path"])


def select_call(analysis,selector,include_inputs=False):
    require(isinstance(selector,str),"take_call must be an address:selector, path:0.1 or execution:0")
    internals=analysis.get("internal_calls",{});declared=analysis.get("declared_calls",{})
    if selector.startswith("path:"):
        text=selector[5:];require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*",text) is not None,"Invalid zero-based call path")
        path=[int(v) for v in text.split('.')];require(len(path)<=64,"Call path is too deep")
        require(internals.get("source")=="rpc.callTracer","Exact paths require callTracer; a declaration has an execution index, not a trace path","CALL_TRACE_UNAVAILABLE")
        matches=[r for r in internals.get("calls",[]) if r.get("path")==path]
    elif selector.startswith("execution:"):
        index=uint(selector[10:],"execution index")
        matches=[r for r in declared.get("calls",[]) if r.get("execution_index")==index]
    else:
        parts=selector.rsplit(':',1);require(len(parts)==2,"take_call requires a full address and 4-byte selector")
        target=address(parts[0]);sig=parts[1].lower();require(re.fullmatch(r"0x[0-9a-f]{8}",sig) is not None,"Invalid 4-byte selector")
        if internals.get("source")=="rpc.callTracer":
            require(internals.get("complete") is True,"Incomplete trace cannot prove unique address/selector selection; use a precise path or execution index","CALL_SELECTION_INCOMPLETE")
            rows=internals.get("calls",[])
        else:
            require(declared.get("status")=="complete","No complete trace or supported known-manager execution payload is available","CALL_TRACE_UNAVAILABLE")
            rows=declared.get("calls",[])
        matches=[r for r in rows if r.get("to")==target and r.get("selector")==sig]
    require(matches,"No matching observed/declarative call; inspect full analysis with --include-inputs","CALL_NOT_FOUND")
    if len(matches)!=1:raise ToolError("CALL_SELECTION_AMBIGUOUS","Multiple calls match; choose path:... or execution:...",data={"candidates":_candidates(matches,include_inputs,analysis.get("inspection",{}).get("transaction"))})
    row=deepcopy(matches[0]);row.setdefault("source",internals.get("source"))
    row.setdefault("execution_observed",row["source"]=="rpc.callTracer")
    require(row.get("type")=="CALL" and row.get("to") and int(row["to"],16)>0x11,"Only ordinary CALL targets can be replayed; delegate/static/create/precompile frames retain different semantics","CALL_CONTEXT_UNSUPPORTED")
    if row["source"]=="rpc.callTracer":require(row.get("committed") is True,"A reverted call is not an executed reference","CALL_NOT_COMMITTED")
    else:require(int((analysis.get("inspection",{}).get("receipt") or {}).get("status",0))==1,"Failed source transactions do not prove declared execution","CALL_NOT_COMMITTED")
    require(isinstance(row.get("input"),str),"Selected call has no complete raw input","CALL_INPUT_UNAVAILABLE")
    return row


def _set_path(args,path,value):
    cursor=args
    for index in path[:-1]:cursor=cursor[index]
    cursor[path[-1]]=value


def plan(tool,params):
    context={"status":"blocked","template":None,"candidates":[],"warnings":[]}
    try:return _plan(tool,params,context)
    except ToolError as exc:
        payload={**context,**(exc.data if isinstance(exc.data,dict) else {})}
        # Candidate indices must always refer to the complete displayed manifest.
        if context["candidates"]:payload["candidates"]=context["candidates"]
        raise ToolError(exc.code,exc.message,retryable=exc.retryable,data=payload) from None
    except Exception as exc:
        raise diagnostic_error(exc, context) from exc


def _plan(tool,params,context):
    overrides=params.get("override",{})
    require(not (params.get("take_call") is not None and params.get("take_candidate") is not None),"Use take_call or take_candidate, not both")
    require(params.get("chain") and params.get("hash"), "Provide chain and hash")
    require(not (params.get("wallet") is not None and params.get("address") is not None),
            "Use wallet or address, not both")
    wallet = None
    if params.get("wallet") is not None:
        wallet = tool.config.wallet(params["wallet"], signing=False)
    elif params.get("address") is not None:
        wallet = {"address": address(params["address"], field="address"), "account": None}
    sender = wallet["address"] if wallet else None
    chain, rpc = tool.rpc(params)
    omit={"wallet","address","override","field_map","take_call","take_candidate","include_inputs","gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}
    source_params={k:v for k,v in params.items() if k not in omit}
    inspected=tool.transaction_inspect(source_params);tx=inspected["transaction"]
    delegation=(tx.get("to") in chain.get("delegation_managers",[]) or tx["data"][:10]==REDEEM_SELECTOR)
    include=params.get("include_inputs",False)
    now = int(time.time())
    if delegation or params.get("take_call") is not None or params.get("take_candidate") is not None:
        if delegation:
            context["warnings"].append("DELEGATION_OUTER_CONTEXT: the business action is inside candidates; the outer authorization payload is not a replay template")
            tool.warnings.append(context["warnings"][0])
        analysis=tool.transaction_analyze({k:v for k,v in source_params.items() if k not in {"abi","signature"}})
        source_inspection=analysis["inspection"];tx=source_inspection["transaction"]
        rows,complete=candidate_rows(analysis);context["candidates"]=_candidates(rows,include,tx,now)
        if params.get("take_candidate") is not None:
            index=uint(params["take_candidate"],"zero-based candidate index")
            require(index<len(rows),"Candidate index is out of range; inspect candidates","CALL_NOT_FOUND")
            selector=candidate_selector(rows[index])
        elif params.get("take_call") is not None:selector=params["take_call"]
        else:
            require(complete and len(rows)==1,"Choose a business candidate explicitly; no unique complete candidate is available","CONTEXT_DEPENDENT_CALL")
            selector=candidate_selector(rows[0]);context["auto_selected_candidate"]=0
        selected=select_call(analysis,selector,include)
        decoded=deepcopy(selected.get("decoded") or {"status":"unknown","selector":selected.get("selector")})
    else:
        source_inspection=inspected
        require(tx.get("to"),"Contract creation cannot be reused as a call template")
        selected={"source":"top_level","path":[],"type":"CALL","from":tx["from"],"to":tx["to"],"execution_context":tx["to"],"value_wei":str(tx["value"]),"input":tx["data"],"execution_observed":inspected["status"]=="success"}
        decoded={"status":"unknown","selector":tx["data"][:10]}
    source_call={k:selected.get(k) for k in ("source","path","execution_index","type","from","to","execution_context","value_wei","execution_observed")}
    source_call.update(input_fields(selected), depth=call_depth(selected))
    if include:source_call.update(expanded_input({**selected, "decoded":decoded}, tx, now))
    context.update(source_call=source_call,source_status=source_inspection["status"])
    context["template"]={"schema_version":1,"source":{"chain_id":chain["chain_id"],"hash":txhash(params["hash"]),"sender":selected.get("from"),"kind":selected["source"],"call_path":selected.get("path"),"execution_index":selected.get("execution_index")},
                         "to":selected["to"],"value_wei":selected["value_wei"],"data":selected["input"],"decoded":{"status":"unknown","selector":source_call["selector"]},"payload_unverified":True,"warnings":list(context["warnings"])+["Unmodified source payload; parameters have not yet been validated for this wallet"]}
    from .analysis import Analyzer
    if selected["source"]=="top_level" or params.get("abi") is not None or params.get("signature") is not None:
        decoder=Analyzer(tool.config,chain,rpc,source_params,source_inspection)
        decoded=decoder.decode_input(selected["input"],selected["to"],params.get("abi"),params.get("signature"))
    if include and decoded.get("template_safe"):
        source_call.pop("word_hints", None)
    require(isinstance(overrides,dict) and not set(overrides)-{"args","amount","value_wei","recipient","deadline","tokenId"},"override supports args, amount, value_wei, recipient, deadline and tokenId")
    safe=decoded.get("status")=="decoded" and decoded.get("template_safe") is True
    original_data=selected["input"];mapping=validate_field_map(params.get("field_map",{}),original_data)
    require(not(mapping and safe),"Choose confirmed ABI editing or explicit unverified word mapping, not both")
    args=deepcopy(decoded.get("args",[]));old_args=deepcopy(args);diff=[];signature=decoded.get("signature");names=list(decoded.get("named_args",{}))
    time_fields=[f for f in named_fields(decoded) if f["kind"]=="deadline"] if safe else []
    if safe and signature in LOCK_CREATION and "deadline" in overrides:
        require(time_fields,"This creation interface has no transaction deadline; lock duration/unlock time must be provided through explicit args with documented units","UNSUPPORTED_OVERRIDE")
    clock=None;clock_source="local_utc_clock"
    if "deadline" in overrides or time_fields or "deadline" in mapping:
        try:
            clock=int(rpc.read(lambda w:w.eth.get_block("latest"))["timestamp"]);clock_source="latest_block_timestamp"
        except Exception:
            if isinstance(overrides.get("deadline"),str) and overrides["deadline"].startswith("now+"):
                raise ToolError("TIME_REFERENCE_UNAVAILABLE","Relative deadlines require a reachable latest block timestamp") from None
    if safe:
        recipient=RECIPIENTS.get(signature)
        if recipient is not None and sender is not None:
            args[recipient] = sender
    if "args" in overrides or "amount" in overrides:
        require(safe,"Editable parameters require an explicit/confirmed ABI or signature; raw calldata needs an explicit field_map","ABI_REQUIRED")
        supplied=overrides.get("args",{})
        if isinstance(supplied,list):
            require(len(supplied)==len(args),"Override argument count differs");args=deepcopy(supplied)
        else:
            require(isinstance(supplied,dict),"override.args must be an array or named object")
            for name,value in supplied.items():
                require(name in names and names.count(name)==1,"Override argument name is unknown or ambiguous");args[names.index(name)]=value
        if "amount" in overrides:
            position=AMOUNTS.get(signature);require(position is not None,"amount shorthand is ambiguous; use override.args")
            require(not isinstance(supplied,list) and (not names or names[position] not in supplied),"Do not override the same amount twice")
            args[position]=str(uint(overrides["amount"],"amount in smallest units"))
    nft_context=None
    if "tokenId" in overrides:
        token_fields=_token_id_fields(decoded) if safe else []
        if overrides["tokenId"]=="auto":
            require(safe and signature in LOCK_CREATION and not token_fields,
                    "tokenId:auto requires a confirmed/explicit contract-assigned-ID creation interface; do not guess a counter or replace an existing NFT ID","TOKEN_ID_AUTO_UNSUPPORTED")
            nft_context={"id_assignment":"contract","expected_token_id":None,"evidence_source":decoded.get("source"),"confidence":decoded.get("confidence"),"note":"No tokenId is inserted; the actual ID is available only from a successful receipt. Explicit ABI/signature is a user assertion about the interface."}
        else:
            require(safe and len(token_fields)==1,"tokenId needs a unique unsigned tokenId field in a reliable ABI","FIELD_SELECTION_AMBIGUOUS")
            field=token_fields[0];supplied=overrides.get("args",{})
            require(not isinstance(supplied,list) and names[field[0]] not in supplied,"Do not override tokenId twice")
            _set_path(args,field,str(uint(overrides["tokenId"],"tokenId")))
            nft_context={"id_assignment":"explicit","token_id":str(uint(overrides["tokenId"])),"ownership_check":"unavailable"}
            if len(field)==1 and signature in {"approve(address,uint256)","transferFrom(address,address,uint256)","safeTransferFrom(address,address,uint256)","safeTransferFrom(address,address,uint256,bytes)"}:
                try:
                    from eth_abi import decode
                    result_=rpc.read(lambda w:w.eth.call({"to":selected["to"],"data":encode_call("ownerOf(uint256)",[overrides["tokenId"]])},"pending"))
                    owner=address(decode(["address"],result_)[0])
                    nft_context.update(owner=owner, ownership_check=("matches_wallet" if owner == sender else "different_owner") if sender else "sender_unspecified")
                except Exception:pass
    data=original_data
    for kind in ("recipient","deadline"):
        if kind not in overrides:continue
        value=overrides[kind]
        if kind == "recipient":
            if value == "self":
                require(sender is not None, "recipient:self requires --wallet or --address", "SENDER_REQUIRED")
                value = sender
            else:
                value = address(value)
        else:
            value = resolve_time(value, clock if clock is not None else 0)
        if safe:
            candidates=[f for f in named_fields(decoded) if f["kind"]==kind]
            if kind=="recipient" and signature in LOCK_CREATION:
                position=RECIPIENTS.get(signature)
                if position is None:
                    require(sender is not None, "Caller-owned lock creation requires --wallet or --address to check a recipient override", "SENDER_REQUIRED")
                    require(value==sender,"Caller-owned lock creation cannot choose a different recipient; use a confirmed createLockFor interface","UNSUPPORTED_OVERRIDE")
                    diff.append({"parameter":"recipient","before":selected.get("from"),"after":sender,"semantic_source":"caller_of_confirmed_creation_interface","calldata_changed":False})
                else:
                    supplied=overrides.get("args",{})
                    require(not isinstance(supplied,list) and names[position] not in supplied,"Do not override the same recipient twice")
                    _set_path(args,[position],value)
                continue
            require(len(candidates)==1,f"{kind} is missing or ambiguous; use an exact ABI argument replacement","FIELD_SELECTION_AMBIGUOUS")
            path=candidates[0]["path"];supplied=overrides.get("args",{})
            require(not isinstance(supplied,list) and (not names or names[path[0]] not in supplied),"Do not override the same field through args and a semantic alias")
            _set_path(args,path,value)
        else:
            require(kind in mapping,f"Unknown {kind} location; provide a reliable ABI or explicit typed field_map","ABI_REQUIRED")
            data,change=patch_word(data,mapping[kind]["word"],value,mapping[kind]["type"])
            diff.append({"parameter":kind,**change,"semantic_source":"explicit_word_mapping"})
    if safe:
        data=encode_call(signature,args);updated=decode_call(data,signature);args=updated["args"];decoded.update(updated,named_args=dict(zip(names,args)))
        diff.extend({"parameter":names[i] if i<len(names) else f"arg{i}","before":old,"after":new} for i,(old,new) in enumerate(zip(old_args,args)) if old!=new)
    else:decoded={"status":"unknown","selector":data[:10] if len(data)>=10 else None}
    value=str(uint(overrides.get("value_wei",selected["value_wei"]),"value_wei"))
    if selected["value_wei"]!=value:diff.append({"parameter":"value_wei","before":selected["value_wei"],"after":value})
    if sender is not None:
        diff.insert(0, {"parameter":"transaction.from", "before":selected.get("from"), "after":sender})
    unverified=not safe or bool(mapping) or selected.get("execution_observed") is False
    notes=list(context["warnings"])+["Wallet-bound signatures/proofs and owner fields are not rewritten; explicit deadline edits can invalidate signatures", "Preview is not an authorization to broadcast"]
    if unverified:notes.append("payload_unverified: declarations and whole-word mappings do not prove intended contract behavior")
    if not safe and data == original_data:notes.append("Unconfirmed calldata remains unchanged; recipient may still refer to third parties")
    template={"schema_version":1,"source":{"chain_id":chain["chain_id"],"hash":txhash(params["hash"]),"sender":selected.get("from"),"transaction_sender":tx["from"],"status":source_inspection["status"],
              "call_path":selected.get("path"),"execution_index":selected.get("execution_index"),"kind":selected["source"]},
              "to":address(selected["to"]),"value_wei":value,"data":data,"decoded":decoded,"warnings":notes,"payload_unverified":unverified}
    if mapping:template["field_map"]=mapping
    result={**context,"status":"preview","template":template,"parameter_diff":diff,"warnings":notes,"source_status":source_inspection["status"],"template_safe":safe,"payload_unverified":unverified,
            "source_call":source_call,"nft_context":nft_context,
            "timing":timing(decoded,clock,clock_source,mapping,data)}
    result["simulation_context"] = {"from":sender, "to":template["to"], "value_wei":value,
                                    "block":"pending" if sender is not None else None}
    if sender is None:
        warning = "SIMULATION_SKIPPED_NO_SENDER: provide --address or --wallet to simulate and estimate fees"
        notes.append(warning)
        tool.warnings.append(warning)
        result.update(status="template_only", simulation={"status":"skipped", "reason":"SIMULATION_SKIPPED_NO_SENDER"}, preview=None)
        return result
    live={k:v for k,v in params.items() if k in {"gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}};live["execute"]=False
    erc20=safe and signature in ("transfer(address,uint256)","transferFrom(address,address,uint256)","approve(address,uint256)") and "erc721" not in decoded.get("standards",[]) and not _token_id_fields(decoded)
    result["preview"] = None
    context.update(result, status="blocked")
    try:result["preview"]=Engine(tool.config,chain,rpc).send(wallet,template["to"],int(value),data,live,erc20=erc20)
    except ToolError as exc:
        result.update(status="blocked",simulation={"status":"failed","reason":exc.code,"details":exc.data},preview=None)
        raise ToolError(exc.code,exc.message,retryable=exc.retryable,data=result) from None
    result["simulation"] = result["preview"]["simulation"]
    return result


def _token_id_fields(decoded):
    paths=[];visited=0
    def walk(spec,value,path):
        nonlocal visited
        visited+=1
        if len(path)>64 or visited>1000:return
        typ=spec["type"]
        if typ.endswith("]") and isinstance(value,(list,tuple)):
            for i,part in enumerate(value[:1000]):walk({**spec,"type":typ[:typ.rfind("[")]},part,path+[i])
        elif typ.startswith("tuple"):
            for i,(part,entry) in enumerate(zip(spec.get("components",[]),value)):walk(part,entry,path+[i])
        elif spec.get("name","").lstrip("_").lower() in ("tokenid","token_id") and re.fullmatch(r"uint(?:[0-9]+)?",typ):paths.append(path)
    for i,(spec,value) in enumerate(zip(parameter_schema(decoded),decoded.get("args",[]))):walk(spec,value,[i])
    return paths

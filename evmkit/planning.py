"""Reference transaction or declared/observed inner CALL to an unsigned live preview."""
import re
from copy import deepcopy
from .abi import canonical,decode_call,encode_call
from .common import ToolError,address,require,txhash,uint
from .engine import Engine
from .standards import V2_ROUTER
from .calldata import named_fields,patch_word,resolve_time,timing,validate_field_map
from .delegation import declared_calls

RECIPIENTS={"transfer(address,uint256)":0,"transferFrom(address,address,uint256)":1,
            "safeTransferFrom(address,address,uint256)":1,"safeTransferFrom(address,address,uint256,bytes)":1,
            "safeTransferFrom(address,address,uint256,uint256,bytes)":1,
            "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)":1,
            "deposit(uint256,address)":1,"mint(uint256,address)":1,
            "withdraw(uint256,address,address)":1,"redeem(uint256,address,address)":1}
AMOUNTS={"transfer(address,uint256)":1,"deposit(uint256,address)":0,"mint(uint256,address)":0}
for item in V2_ROUTER:
    names=[p["name"] for p in item["inputs"]]
    RECIPIENTS[canonical(item)]=names.index("to")
    if "amountIn" in names: AMOUNTS[canonical(item)]=names.index("amountIn")


def _candidates(rows):
    return [{k:r.get(k) for k in ("path","execution_index","source","type","to","selector","input_bytes","committed")} for r in rows]


def select_call(analysis,selector):
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
    if len(matches)!=1:raise ToolError("CALL_SELECTION_AMBIGUOUS","Multiple calls match; choose path:... or execution:...",data={"candidates":_candidates(matches)})
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
    overrides=params.get("override",{})
    require(isinstance(overrides,dict) and not set(overrides)-{"args","amount","value_wei","recipient","deadline"},"override supports args, amount, value_wei, recipient and deadline")
    chain,rpc=tool.rpc(params);wallet=tool.config.wallet(params["wallet"],signing=False)
    omit={"wallet","override","field_map","take_call","gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}
    source_params={k:v for k,v in params.items() if k not in omit}
    if params.get("take_call"):
        from .analysis import Analyzer
        analysis=tool.transaction_analyze({k:v for k,v in source_params.items() if k not in {"abi","signature"}})
        source_inspection=analysis["inspection"];selected=select_call(analysis,params["take_call"])
        tx=source_inspection["transaction"]
        if params.get("abi") is not None or params.get("signature") is not None:
            decoder=Analyzer(tool.config,chain,rpc,source_params,source_inspection)
            decoded=decoder.decode_input(selected["input"],selected["to"],params.get("abi"),params.get("signature"))
        else:decoded=deepcopy(selected.get("decoded") or {"status":"unknown","selector":selected.get("selector")})
    else:
        source=tool.transaction_decode(source_params);source_inspection=source["inspection"];tx=source_inspection["transaction"]
        require(tx.get("to"),"Contract creation cannot be reused as a call template")
        decoded=deepcopy(source["decoded"])
        selected={"source":"top_level","path":[],"type":"CALL","from":tx["from"],"to":tx["to"],"execution_context":tx["to"],"value_wei":str(tx["value"]),"input":source["data"],"execution_observed":source_inspection["status"]=="success"}
        declaration=declared_calls(tx,chain)
        if declaration["status"] in ("complete","partial"):
            template={"schema_version":1,"source":{"chain_id":chain["chain_id"],"hash":txhash(params["hash"]),"sender":tx["from"]},
                      "to":tx["to"],"value_wei":str(tx["value"]),"data":tx["data"],"decoded":{"status":"unknown","selector":tx["data"][:10]},
                      "payload_unverified":True,"warnings":["Outer delegation calldata requires its original authorization context"]}
            raise ToolError("CONTEXT_DEPENDENT_CALL","Select a business CALL with --take-call; copying the manager payload does not reproduce authorization",
                            data={"status":"blocked","template":template,"parameter_diff":[],"candidates":_candidates(declaration["calls"])})
    safe=decoded.get("status")=="decoded" and decoded.get("template_safe") is True
    original_data=selected["input"];mapping=validate_field_map(params.get("field_map",{}),original_data)
    require(not(mapping and safe),"Choose confirmed ABI editing or explicit unverified word mapping, not both")
    args=deepcopy(decoded.get("args",[]));old_args=deepcopy(args);diff=[];signature=decoded.get("signature");names=list(decoded.get("named_args",{}))
    time_fields=[f for f in named_fields(decoded) if f["kind"]=="deadline"] if safe else []
    clock=None;clock_source="local_utc_clock"
    if "deadline" in overrides or time_fields or "deadline" in mapping:
        try:
            clock=int(rpc.read(lambda w:w.eth.get_block("latest"))["timestamp"]);clock_source="latest_block_timestamp"
        except Exception:
            if isinstance(overrides.get("deadline"),str) and overrides["deadline"].startswith("now+"):
                raise ToolError("TIME_REFERENCE_UNAVAILABLE","Relative deadlines require a reachable latest block timestamp") from None
    if safe:
        recipient=RECIPIENTS.get(signature)
        if recipient is not None:args[recipient]=wallet["address"]
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
    data=original_data
    for kind in ("recipient","deadline"):
        if kind not in overrides:continue
        value=overrides[kind]
        if kind=="recipient":value=wallet["address"] if value=="self" else address(value)
        else:value=resolve_time(value,clock if clock is not None else 0)
        if safe:
            candidates=[f for f in named_fields(decoded) if f["kind"]==kind]
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
    diff.insert(0,{"parameter":"transaction.from","before":selected.get("from"),"after":wallet["address"]})
    unverified=not safe or bool(mapping) or selected.get("execution_observed") is False
    notes=["Wallet-bound signatures/proofs and owner fields are not rewritten; explicit deadline edits can invalidate signatures", "Preview is not an authorization to broadcast"]
    if unverified:notes.append("payload_unverified: declarations and whole-word mappings do not prove intended contract behavior")
    if not safe and not diff[1:]:notes.append("Unconfirmed calldata remains unchanged; recipient may still refer to third parties")
    template={"schema_version":1,"source":{"chain_id":chain["chain_id"],"hash":txhash(params["hash"]),"sender":selected.get("from"),"transaction_sender":tx["from"],"status":source_inspection["status"],
              "call_path":selected.get("path"),"execution_index":selected.get("execution_index"),"kind":selected["source"]},
              "to":address(selected["to"]),"value_wei":value,"data":data,"decoded":decoded,"warnings":notes,"payload_unverified":unverified}
    if mapping:template["field_map"]=mapping
    result={"status":"preview","template":template,"parameter_diff":diff,"warnings":notes,"source_status":source_inspection["status"],"template_safe":safe,"payload_unverified":unverified,
            "source_call":{k:selected.get(k) for k in ("source","path","execution_index","type","from","to","execution_context","value_wei","execution_observed")},
            "timing":timing(decoded,clock,clock_source,mapping,data)}
    result["simulation_context"]={"from":wallet["address"],"to":template["to"],"value_wei":value,"block":"pending"}
    live={k:v for k,v in params.items() if k in {"gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}};live["execute"]=False
    try:result["preview"]=Engine(tool.config,chain,rpc).send(wallet,template["to"],int(value),data,live,erc20=safe and signature in ("transfer(address,uint256)","transferFrom(address,address,uint256)","approve(address,uint256)"))
    except ToolError as exc:
        result.update(status="blocked",simulation={"status":"failed","reason":exc.code,"details":exc.data},preview=None)
        raise ToolError(exc.code,exc.message,retryable=exc.retryable,data=result) from None
    return result

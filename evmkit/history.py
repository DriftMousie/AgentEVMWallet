"""Bounded explorer-backed normal-transaction history; never scans the chain."""
import base64
import hashlib
import json
from .analysis import settings
from .common import ToolError,address,hexdata,require,txhash,uint
from .explorers import Sources
from .network import Budget


def _location(value):
    value=value.get("hash") if isinstance(value,dict) else value
    return address(value) if value else None


def normalize(row, provider):
    source,destination=_location(row.get("from")),_location(row.get("to"))
    raw=row.get("raw_input",row.get("input","0x")) or "0x"
    try: raw=hexdata(raw)
    except ToolError: raw=None
    status=row.get("status")
    if provider=="etherscan": status="reverted" if row.get("isError")=="1" else "success" if row.get("txreceipt_status")=="1" else "unknown"
    elif status=="ok": status="success"
    elif status=="error": status="reverted"
    to_info=row.get("to")
    return {"hash":txhash(row["hash"]),"block_number":str(row.get("block_number",row.get("blockNumber",""))),
            "timestamp":row.get("timestamp",row.get("timeStamp")),"from":source,"to":destination,
            "value_wei":str(uint(row.get("value","0"))),"selector":raw[:10] if raw and len(raw)>=10 else None,
            "data_bytes":(len(raw)-2)//2 if raw is not None else None,"status":status or "unknown",
            "target_is_contract":to_info.get("is_contract") if isinstance(to_info,dict) else None,
            "reported_function":str(row.get("method",row.get("functionName","")))[:160] or None}


def _encode(value): return base64.urlsafe_b64encode(json.dumps(value,separators=(",",":")).encode()).decode()


def history(config,chain,params):
    owner=address(params["address"]);limit=uint(params.get("limit",50))
    require(1<=limit<=200,"limit must be 1..200")
    direction=params.get("direction","both")
    require(direction in ("in","out","both"),"direction must be in, out or both")
    target=address(params["to"]) if params.get("to") else None
    options=settings(config,params);budget=Budget(options["time_budget_seconds"],options["max_requests"])
    sources=Sources(config,chain,budget,True,options["max_pages"],params.get("no_cache",False))
    scope=hashlib.sha256(json.dumps([chain["chain_id"],owner,direction,target]).encode()).hexdigest()
    state=None
    if params.get("cursor"):
        try:
            require(isinstance(params["cursor"],str) and len(params["cursor"])<=4096,"Invalid history cursor")
            state=json.loads(base64.urlsafe_b64decode(params["cursor"]))
            require(state["scope"]==scope and state["provider"] in ("blockscout","etherscan"),"Cursor differs from query")
            require(isinstance(state["query"],dict) and isinstance(state["offset"],int) and 0<=state["offset"]<=1000,"Invalid cursor state")
            allowed={"block_number","index","items_count","hash","inserted_at","fee","value","transaction_index"} if state["provider"]=="blockscout" else {"page"}
            require(not set(state["query"])-allowed and all(isinstance(v,(str,int)) and not isinstance(v,bool) and len(str(v))<=256 for v in state["query"].values()),"Invalid cursor pagination")
            if state["provider"]=="etherscan": require(1<=uint(state["query"].get("page",1))<=10000000,"Invalid cursor page")
        except (ValueError,TypeError,KeyError): raise ToolError("INVALID_INPUT","Invalid history cursor") from None
    errors=[]
    for provider in ([state["provider"]] if state else ["blockscout","etherscan"]):
        items=[];seen=set();pages=0;exhausted=False;failure=None
        query=dict(state["query"]) if state else ({} if provider=="blockscout" else {"page":1})
        offset=state["offset"] if state else 0
        while pages<options["max_pages"] and len(items)<limit:
            try:
                if provider=="blockscout":
                    require(sources.provider.get("blockscout_url"),"Blockscout history not configured","SOURCE_UNCONFIGURED")
                    key=sources.keys().get("chains",{}).get(chain["alias"],{}).get("blockscout_api_key")
                    args=dict(query)
                    if direction!="both": args["filter"]="from" if direction=="out" else "to"
                    page=sources.cached_get(sources.provider["blockscout_url"],"/api/v2/addresses/"+owner+"/transactions",args,key,ttl=60)
                    rows=page["items"];next_query=page.get("next_page_params")
                    require(next_query is None or isinstance(next_query,dict),"Invalid history pagination","INVALID_RESPONSE")
                else:
                    require(sources.provider.get("etherscan"),"Etherscan history not configured","SOURCE_UNCONFIGURED")
                    key=sources.keys().get("etherscan_api_key")
                    require(isinstance(key,str) and key,"Etherscan history requires a local API key","SOURCE_UNCONFIGURED")
                    page=sources.cached_get("https://api.etherscan.io","/v2/api",{"chainid":str(chain["chain_id"]),"module":"account","action":"txlist",
                        "address":owner,"startblock":"0","endblock":"99999999","page":query.get("page",1),"offset":50,"sort":"desc"},key,ttl=60)
                    rows=page["result"];next_query={"page":int(query.get("page",1))+1} if isinstance(rows,list) and len(rows)==50 else None
                require(isinstance(rows,list),"Invalid explorer history page","INVALID_RESPONSE")
                pages+=1
                for index in range(offset,len(rows)):
                    row=normalize(rows[index],provider)
                    offset=index+1
                    matches=direction=="both" or direction=="out" and row["from"]==owner or direction=="in" and row["to"]==owner
                    if matches and (target is None or row["to"]==target) and row["hash"] not in seen:
                        items.append(row);seen.add(row["hash"])
                    if len(items)==limit: break
                if offset<len(rows): break
                if not next_query: exhausted=True;break
                query=next_query;offset=0
            except ToolError as exc: failure=exc.code;errors.append({"source":provider,"reason":exc.code});break
            except (ValueError,TypeError,KeyError,AttributeError): failure="INVALID_RESPONSE";errors.append({"source":provider,"reason":failure});break
        if failure and not items and not state: continue
        cursor=None if exhausted else _encode({"scope":scope,"provider":provider,"query":query,"offset":offset})
        return {"address":owner,"source":provider,"direction":direction,"scope":"normal_transactions_only",
                "items":items,"returned":len(items),"limit":limit,"complete":exhausted,"next_cursor":cursor,
                "status":"partial" if failure or pages>=options["max_pages"] and not exhausted and len(items)<limit else "complete",
                "pages":pages,"requests_used":budget.requests,"sources_missing":errors,
                "limitations":["Window is not all lifetime activity; excludes internal calls and token-event-only history"]}
    reason=next((row["reason"] for row in reversed(errors) if row["reason"]!="SOURCE_UNCONFIGURED"),"SOURCE_UNCONFIGURED")
    raise ToolError(reason,"Address history requires an available configured Blockscout/Etherscan API; no RPC block scan is attempted",data={"sources_missing":errors})


def actions(config,chain,params):
    require(params.get("direction","out")=="out","actions aggregates outgoing direct calls; use history for other directions")
    result=history(config,chain,{**params,"direction":"out"})
    groups={};excluded=0
    for tx in result["items"]:
        if not tx["selector"] or not tx["to"]: excluded+=1;continue
        key=(tx["to"],tx["selector"])
        row=groups.setdefault(key,{"contract":tx["to"],"selector":tx["selector"],"is_contract":tx["target_is_contract"],"count":0,"success":0,"failed":0,"unknown":0})
        row["count"]+=1
        row["success" if tx["status"]=="success" else "failed" if tx["status"] in ("reverted","failed") else "unknown"]+=1
    result.pop("items")
    result.update(actions=sorted(groups.values(),key=lambda r:(-r["count"],r["contract"],r["selector"])),non_call_transactions=excluded,
                  scope="recent_outgoing_direct_call_candidates; target contract identity may be unknown")
    return result

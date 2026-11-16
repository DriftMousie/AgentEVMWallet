"""Live, bounded, read-only capability probes; results are not cached promises."""
import json
import time
from urllib.parse import urlsplit
from .analysis import settings, ZERO
from .common import ToolError, require, txhash
from .diagnostics import REASONS
from .explorers import Sources
from .network import Budget, json_request, provider_error_code


def measure(fn, validate=lambda value: True):
    start = time.monotonic()
    try:
        value = fn()
        require(validate(value), "Probe returned an unexpected response shape", "INVALID_RESPONSE")
        return {"status": "available", "latency_ms": int((time.monotonic()-start)*1000)}, value
    except ToolError as exc:
        return {"status": REASONS.get(exc.code, "invalid_response" if exc.code == "INVALID_RESPONSE" else "unavailable"),
                "reason": exc.code, "latency_ms": int((time.monotonic()-start)*1000)}, None
    except Exception:
        return {"status": "invalid_response", "reason": "INVALID_RESPONSE", "latency_ms": int((time.monotonic()-start)*1000)}, None


def hex_quantity(value):
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 3: return False
    try: return int(value, 16) >= 0
    except ValueError: return False


def probe(config, chain, rpc, params):
    options = settings(config, params)
    budget = Budget(options["time_budget_seconds"], options["max_requests"])
    supplied = txhash(params["hash"]) if params.get("hash") else None
    endpoints = []
    for name, _ in rpc.clients:
        def request(method, args): return rpc.optional_request(method, args, budget, rpc_name=name)
        health, height = measure(lambda: request("eth_blockNumber", []), hex_quantity)
        row = {"rpc_name": name, "health": health, "sample_hash": supplied, "tracers": {}}
        if height is None:
            row["tracers"] = {k:{"status": "not_tested", "reason": "RPC health check failed"} for k in ("callTracer", "prestateTracer_diff", "debug_traceCall")}
            endpoints.append(row); continue
        if not row["sample_hash"]:
            def sample():
                for number in range(int(height,16), max(-1,int(height,16)-5), -1):
                    block = request("eth_getBlockByNumber", [hex(number), False])
                    if block.get("transactions"): return txhash(block["transactions"][0])
                return None
            sample_status, row["sample_hash"] = measure(sample)
            if row["sample_hash"] is None:
                row["sample_status"] = sample_status if sample_status["status"] != "available" else {"status":"not_tested", "reason":"No mined sample found within five blocks"}
        if row["sample_hash"]:
            row["tracers"]["callTracer"], _ = measure(lambda: request("debug_traceTransaction", [row["sample_hash"], {"tracer":"callTracer", "timeout":"5s"}]),
                lambda value:isinstance(value,dict) and ("type" in value or "calls" in value))
            row["tracers"]["prestateTracer_diff"], _ = measure(lambda: request("debug_traceTransaction", [row["sample_hash"], {"tracer":"prestateTracer", "tracerConfig":{"diffMode":True,"disableStorage":True,"disableCode":True}, "timeout":"5s"}]),
                lambda value:isinstance(value,dict) and isinstance(value.get("pre"),dict) and isinstance(value.get("post"),dict))
        else:
            for key in ("callTracer", "prestateTracer_diff"):row["tracers"][key] = {"status":"not_tested", "reason":"Mined transaction hash required"}
        row["tracers"]["debug_traceCall"], _ = measure(lambda: request("debug_traceCall", [{"from":ZERO,"to":ZERO,"data":"0x","value":"0x0","gas":"0x5208"},"latest",{"tracer":"callTracer","timeout":"5s"}]),
                lambda value:isinstance(value,dict) and ("type" in value or "calls" in value))
        endpoints.append(row)
    result = {"chain_id":chain["chain_id"], "rpc":endpoints, "explorers":{}, "requests_used":budget.requests,
              "limitations":["Availability applies only to this sample and endpoint plan; it does not prove unlimited historical coverage", "Probe requests consume provider quota; no transaction is signed or sent"]}
    if params.get("explorer"):
        sources = Sources(config, chain, budget, enabled=True)
        def get(base, route, query=None, key=None):
            parsed=urlsplit(base)
            require(parsed.scheme in ("http","https") and parsed.hostname and not parsed.username and not parsed.query and not parsed.fragment,"Invalid explorer base URL","CONFIG_ERROR")
            query=dict(query or {})
            if key:query["apikey"]=key
            value=json_request("GET",base.rstrip('/')+route,params=query,timeout=min(10,budget.spend()))
            require(not key or key not in json.dumps(value), "Provider reflected credentials", "SOURCE_UNAVAILABLE")
            failure=provider_error_code(value)
            if failure:raise ToolError(failure,'Provider probe rejected')
            return value
        provider=sources.provider
        if provider.get('blockscout_url'):
            def bs():
                key=sources.keys().get('chains',{}).get(chain['alias'],{}).get('blockscout_api_key')
                return get(provider['blockscout_url'],'/api/v2/stats',key=key)
            result['explorers']['blockscout'],_=measure(bs,lambda v:isinstance(v,dict))
        else:result['explorers']['blockscout']={'status':'not_configured'}
        if provider.get('sourcify'):
            base=provider.get('sourcify_url') or sources.raw.get('sourcify',{}).get('base_url','https://sourcify.dev/server')
            result['explorers']['sourcify'],_=measure(lambda:get(base,f"/v2/contracts/{chain['chain_id']}",{'limit':1}),lambda v:isinstance(v,dict) and isinstance(v.get('results'),list))
        else:result['explorers']['sourcify']={'status':'not_configured'}
        if provider.get('etherscan'):
            def es():
                key=sources.keys().get('etherscan_api_key')
                require(key,'Etherscan API key is not configured','SOURCE_UNCONFIGURED')
                return get('https://api.etherscan.io','/v2/api',{'chainid':str(chain['chain_id']),'module':'proxy','action':'eth_blockNumber'},key)
            result['explorers']['etherscan'],_=measure(es,lambda v:isinstance(v,dict) and hex_quantity(v.get('result')))
        else:result['explorers']['etherscan']={'status':'not_configured'}
        result['requests_used']=budget.requests
    result['historical_trace_ready']=all(any(row['tracers'].get(key,{}).get('status')=='available' for row in endpoints) for key in ('callTracer','prestateTracer_diff'))
    return result

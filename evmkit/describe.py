"""Static machine help. Importing this module never reads runtime configuration."""
SUPPORTED={("transaction","analyze"),("transaction","plan"),("address","history"),("address","actions")}


def describe(module,operation):
    from .operations import OPS
    from .common import require
    require((module,operation) in SUPPORTED,"Machine descriptions are available for transaction analyze/plan and address history/actions")
    required={"analyze":["chain","hash"],"plan":["chain","hash","wallet"],"history":["chain","address"],"actions":["chain","address"]}[operation]
    booleans={"external","trace","require_trace","no_cache"}
    numbers={"limit","time_budget_seconds","max_requests","max_frames","max_depth","max_pages","max_tokens","max_addresses"}
    arrays={"tokens","addresses"}
    properties={}
    for field in sorted(OPS[module][operation]):
        row={"type":"boolean" if field in booleans else "integer" if field in numbers else "array" if field in arrays else "object" if field=="override" else "string"}
        if field in arrays: row["items"]={"type":"string"}
        if field=="view": row.update(enum=["summary","full"],default="full")
        if field=="direction": row.update(enum=["out"] if operation=="actions" else ["in","out","both"],default="out" if operation=="actions" else "both")
        if field=="limit": row.update(minimum=1,maximum=200,default=50)
        if field=="max_requests": row["default"]=300
        if field=="time_budget_seconds": row["default"]=60
        if field in ("abi",): row={"oneOf":[{"type":"string"},{"type":"array"},{"type":"object"}]}
        if field=="override":row.update(description="args is a complete array or named argument object; amount and value_wei are smallest-unit integer strings",
            properties={"args":{"oneOf":[{"type":"array"},{"type":"object"}]},"amount":{"type":"string","pattern":"^[0-9]+$"},"value_wei":{"type":"string","pattern":"^[0-9]+$"}},additionalProperties=False)
        properties[field]=row
    use={"analyze":"Explain observed assets, input, logs and risks for a transaction hash", "plan":"Prepare a reference transaction for a selected local wallet and simulate it without broadcasting",
         "history":"Read a bounded explorer window of normal transactions for an address", "actions":"Aggregate recent outgoing direct call targets and selectors"}[operation]
    return {"schema_version":1,"operation":module+"."+operation,"when_to_use":[use],
            "when_not_to_use":["Do not infer chain from hash", "Do not interpret event flows as all token storage changes"] if operation=="analyze" else
                              ["Not for signing or broadcasting"] if operation=="plan" else ["Not for exhaustive lifetime/internal-call history"],
            "input_schema":{"type":"object","properties":properties,"required":required,"additionalProperties":False},
            "cost":{"rpc_requests":"0 on analysis snapshot hit; otherwise bounded by max_requests (HTTP), with batch subcalls counted separately" if operation=="analyze" else
                                     "live simulation, nonce, balance and fees; varies with ABI lookup" if operation=="plan" else "0; uses configured explorer APIs",
                    "external_requests":"provider-dependent; history is bounded by max_pages", "time_s":"depends on RPC, trace support, cache and limits; no fixed latency guarantee"},
            "output_view":"summary|full" if operation=="analyze" else "full",
            "stable_fields":[".ok",".error",".data.analysis_status",".data.fund_flows"] if operation=="analyze" else
                            [".ok",".error",".data.template",".data.parameter_diff",".data.preview"] if operation=="plan" else
                            [".ok",".error",".data.source",".data.complete",".data.next_cursor",".data.actions" if operation=="actions" else ".data.items"],
            "examples":["python transaction.py analyze --chain bsc --hash TX_HASH --view summary"] if operation=="analyze" else
                       ["python transaction.py plan --chain bsc --hash TX_HASH --wallet trading"] if operation=="plan" else
                       [f"python address.py {operation} --chain bsc --address ADDRESS --limit 50"],
            "constraints":{"summary_max_utf8_bytes":2048,"plan_can_broadcast":False,"amount_unit":"smallest units; decimal integer strings"}}

"""Static machine help. Importing this module never reads runtime configuration."""
SUPPORTED={("transaction","analyze"),("transaction","plan"),("address","history"),("address","actions"),("raw","patch")}


def describe(module,operation):
    from .operations import OPS
    from .common import require
    require((module,operation) in SUPPORTED,"Machine descriptions are available for analyze/plan, history/actions and raw patch")
    required={"analyze":["chain","hash"],"plan":["chain","hash","wallet"],"history":["chain","address"],"actions":["chain","address"],"patch":["chain","to","data","word","value","verify"]}[operation]
    booleans={"external","trace","require_trace","no_cache","include_inputs","verify"}
    numbers={"limit","time_budget_seconds","max_requests","max_frames","max_depth","max_pages","max_tokens","max_addresses","word"}
    arrays={"tokens","addresses"}
    properties={}
    for field in sorted(OPS[module][operation]):
        row={"type":"boolean" if field in booleans else "integer" if field in numbers else "array" if field in arrays else "object" if field in ("override","field_map") else "string"}
        if field in arrays: row["items"]={"type":"string"}
        if field=="view": row.update(enum=["summary","full"],default="full")
        if field=="direction": row.update(enum=["out"] if operation=="actions" else ["in","out","both"],default="out" if operation=="actions" else "both")
        if field=="limit": row.update(minimum=1,maximum=200,default=50)
        if field=="max_requests": row["default"]=300
        if field=="time_budget_seconds": row["default"]=60
        if field=="include_inputs":row.update(default=False,description="Full raw inputs and legacy input aliases; incompatible with summary view")
        if field=="verify":row["const"]=True
        if field=="word":row.update(minimum=0,description="Zero-based 32-byte word AFTER the 4-byte selector")
        if field=="type":row.update(enum=["address","uint256","bytes32"],default="bytes32")
        if field=="take_call":row["description"]="Full address:4-byte-selector, path:0.1.2, or execution:0; ambiguous matches are rejected"
        if field=="field_map":row.update(description="Explicit whole-word recipient/deadline locations for unknown ABI; cannot be combined with confirmed ABI editing",
            properties={"recipient":{"type":"object","properties":{"word":{"type":"integer","minimum":0},"type":{"const":"address"}},"required":["word","type"],"additionalProperties":False},
                        "deadline":{"type":"object","properties":{"word":{"type":"integer","minimum":0},"type":{"const":"uint256"},"unit":{"const":"seconds"}},"required":["word","type","unit"],"additionalProperties":False}},additionalProperties=False)
        if field in ("abi",): row={"oneOf":[{"type":"string"},{"type":"array"},{"type":"object"}]}
        if field=="override":row.update(description="args is a complete array or named argument object; amount and value_wei are smallest-unit integer strings",
            properties={"args":{"oneOf":[{"type":"array"},{"type":"object"}]},"amount":{"type":"string","pattern":"^[0-9]+$"},"value_wei":{"type":"string","pattern":"^[0-9]+$"},
                        "recipient":{"type":"string","description":"self or a full address; only unique ABI or explicitly mapped address fields"},"deadline":{"type":"string","pattern":"^(?:[0-9]+|now\\+[0-9]+)$"}},additionalProperties=False)
        properties[field]=row
    use={"analyze":"Explain observed assets, input, logs and risks for a transaction hash", "plan":"Prepare a reference transaction for a selected local wallet and simulate it without broadcasting",
         "history":"Read a bounded explorer window of normal transactions for an address", "actions":"Aggregate recent outgoing direct call targets and selectors",
         "patch":"Replace exactly one whole calldata word and eth_call it from an explicit sender; never broadcasts"}[operation]
    schema={"type":"object","properties":properties,"required":required,"additionalProperties":False}
    if operation=="patch":schema["oneOf"]=[{"required":["wallet"],"not":{"required":["address"]}},{"required":["address"],"not":{"required":["wallet"]}}]
    if operation=="analyze":schema["not"]={"properties":{"view":{"const":"summary"},"include_inputs":{"const":True}},"required":["view","include_inputs"]}
    return {"schema_version":1,"operation":module+"."+operation,"when_to_use":[use],
            "when_not_to_use":["Do not infer chain from hash", "Do not interpret event flows as all token storage changes"] if operation=="analyze" else
                              ["Not for signing or broadcasting", "Simulation does not prove intended semantics"] if operation in ("plan","patch") else ["Not for exhaustive lifetime/internal-call history"],
            "input_schema":schema,
            "cost":{"rpc_requests":"0 on analysis snapshot hit; otherwise bounded by max_requests (HTTP), with batch subcalls counted separately" if operation=="analyze" else
                                     "live simulation, nonce, balance and fees; varies with ABI lookup" if operation=="plan" else "eth_call plus chain validation" if operation=="patch" else "0; uses configured explorer APIs",
                    "external_requests":"provider-dependent; history is bounded by max_pages", "time_s":"depends on RPC, trace support, cache and limits; no fixed latency guarantee"},
            "output_view":"summary|full" if operation=="analyze" else "full",
            "stable_fields":[".ok",".error",".data.analysis_status",".data.fund_flows"] if operation=="analyze" else
                            [".ok",".error",".data.template",".data.parameter_diff",".data.preview"] if operation=="plan" else
                            [".ok",".error",".data.data",".data.payload_unverified",".data.verification",".data.parameter_diff"] if operation=="patch" else
                            [".ok",".error",".data.source",".data.complete",".data.next_cursor",".data.actions" if operation=="actions" else ".data.items"],
            "examples":["python transaction.py analyze --chain bsc --hash TX_HASH --view summary"] if operation=="analyze" else
                       ["python transaction.py plan --chain bsc --hash TX_HASH --wallet trading"] if operation=="plan" else
                       ["python raw.py patch --chain sepolia --to CONTRACT --address SENDER --data CALLDATA --word 6 --type address --value RECIPIENT --verify"] if operation=="patch" else
                       [f"python address.py {operation} --chain bsc --address ADDRESS --limit 50"],
            "constraints":{"summary_max_utf8_bytes":2048,"plan_can_broadcast":False,"amount_unit":"smallest units; decimal integer strings"}}

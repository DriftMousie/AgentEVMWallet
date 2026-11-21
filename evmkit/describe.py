"""Static machine help. Importing this module never reads runtime configuration."""
SUPPORTED={("transaction","analyze"),("transaction","plan"),("address","history"),("address","actions"),("raw","patch"),("contract","introspect"),("contract","diagnose")}


def describe(module,operation):
    from .operations import OPS
    from .common import require
    require(operation in OPS.get(module, {}), "Unknown operation")
    accepted=sorted(OPS[module][operation])
    if module=='contract' and operation in ('introspect','diagnose'):
        return {**contract_describe(operation), "accepted_fields":accepted}
    if (module,operation) not in SUPPORTED:
        return basic_description(module, operation, accepted)
    required={"analyze":["chain","hash"],"plan":["chain","hash"],"history":["chain","address"],"actions":["chain","address"],"patch":["chain","verify"]}[operation]
    booleans={"external","trace","require_trace","no_cache","include_inputs","verify"}
    numbers={"limit","time_budget_seconds","max_requests","max_frames","max_depth","max_pages","max_tokens","max_addresses","word","take_candidate"}
    arrays={"tokens","addresses","set"}
    properties={}
    for field in sorted(OPS[module][operation]):
        row={"type":"boolean" if field in booleans else "integer" if field in numbers else "array" if field in arrays else "object" if field in ("override","field_map") else "string"}
        if field in arrays: row["items"]={"type":"string"}
        if field=="view": row.update(enum=["summary","full"],default="full")
        if field=="direction": row.update(enum=["out"] if operation=="actions" else ["in","out","both"],default="out" if operation=="actions" else "both")
        if field=="limit": row.update(minimum=1,maximum=200,default=50)
        if field=="max_requests": row["default"]=300
        if field=="time_budget_seconds": row["default"]=60
        if field=="include_inputs":row.update(default=False,description="Full raw inputs and low-confidence word hints when no reliable ABI is available; incompatible with summary view")
        if operation=="plan" and field in ("wallet","address"):
            row["description"] = "Optional simulation sender. Choose either wallet or address. With neither, return a template without simulation."
        if field=="verify":row["const"]=True
        if field=="word":row.update(minimum=0,description="Zero-based 32-byte word AFTER the 4-byte selector")
        if field=="type":row.update(enum=["address","uint256","bytes32"],default="bytes32")
        if field=="set":row.update(minItems=1,maxItems=128,description="Array of INDEX[:TYPE]=VALUE strings; set each word at most once")
        if field=="frame":row["description"]="Observed path:0.4 or unique full address:selector"
        if field=="take_call":row["description"]="Full address:4-byte-selector, path:0.1.2, or execution:0; ambiguous matches are rejected"
        if field=="take_candidate":row.update(minimum=0,description="Zero-based displayed candidate index; mutually exclusive with take_call")
        if field=="field_map":row.update(description="Explicit whole-word recipient/deadline locations for unknown ABI; cannot be combined with confirmed ABI editing",
            properties={"recipient":{"type":"object","properties":{"word":{"type":"integer","minimum":0},"type":{"const":"address"}},"required":["word","type"],"additionalProperties":False},
                        "deadline":{"type":"object","properties":{"word":{"type":"integer","minimum":0},"type":{"const":"uint256"},"unit":{"const":"seconds"}},"required":["word","type","unit"],"additionalProperties":False}},additionalProperties=False)
        if field in ("abi",): row={"oneOf":[{"type":"string"},{"type":"array"},{"type":"object"}]}
        if field=="override":row.update(description="args is a complete array or named argument object; amount and value_wei are smallest-unit integer strings",
            properties={"args":{"oneOf":[{"type":"array"},{"type":"object"}]},"amount":{"type":"string","pattern":"^[0-9]+$"},"value_wei":{"type":"string","pattern":"^[0-9]+$"},
                        "recipient":{"type":"string","description":"self or a full address; unique ABI/mapped fields or confirmed caller-owned lock creation"},"deadline":{"type":"string","pattern":"^(?:[0-9]+|now\\+[0-9]+)$"},
                        "tokenId":{"type":"string","pattern":"^(?:[0-9]+|auto)$","description":"Explicit ABI tokenId, or auto only for supported contract-assigned-ID creation interfaces"}},additionalProperties=False)
        properties[field]=row
    use={"analyze":"Explain observed assets, input, logs and risks for a transaction hash", "plan":"Prepare a template from a reference transaction and optionally simulate from a wallet or public address",
         "history":"Read a bounded explorer window of normal transactions for an address", "actions":"Aggregate recent outgoing direct call targets and selectors",
         "patch":"Replace whole calldata words in supplied bytes or an observed CALL, then eth_call once from an explicit sender"}[operation]
    schema={"type":"object","properties":properties,"required":required,"additionalProperties":False}
    if operation=="patch":
        schema["oneOf"]=[{"required":["wallet"],"not":{"required":["address"]}},{"required":["address"],"not":{"required":["wallet"]}}]
        schema["allOf"]=[
            {"oneOf":[{"required":["data","to"],"not":{"anyOf":[{"required":["from_trace"]},{"required":["frame"]}]}},
                      {"required":["from_trace","frame"],"not":{"anyOf":[{"required":["data"]},{"required":["to"]}]}}]},
            {"oneOf":[{"required":["word","value"],"not":{"required":["set"]}},
                      {"required":["set"],"not":{"anyOf":[{"required":["word"]},{"required":["value"]},{"required":["type"]}]}}]},
        ]
    if operation=="analyze":schema["not"]={"properties":{"view":{"const":"summary"},"include_inputs":{"const":True}},"required":["view","include_inputs"]}
    if operation=="plan":
        schema["not"] = {"required":["take_call","take_candidate"]}
        schema["allOf"] = [{"not":{"required":["wallet","address"]}}]
    return {"schema_version":1,"operation":module+"."+operation,"accepted_fields":accepted,"when_to_use":[use],
            "when_not_to_use":["Do not infer chain from hash", "Do not interpret event flows as all token storage changes"] if operation=="analyze" else
                              ["Not for signing or broadcasting", "Simulation does not prove intended semantics"] if operation in ("plan","patch") else ["Not for exhaustive lifetime/internal-call history"],
            "input_schema":schema,
            "cost":{"rpc_requests":"0 on analysis snapshot hit; otherwise bounded by max_requests (HTTP), with batch subcalls counted separately" if operation=="analyze" else
                                     "live simulation, nonce, balance and fees; one alternate on revert when available" if operation=="plan" else "one verification eth_call plus chain validation; trace mode also reads the source transaction and trace" if operation=="patch" else "0; uses configured explorer APIs",
                    "external_requests":"provider-dependent; history is bounded by max_pages", "time_s":"depends on RPC, trace support, cache and limits; no fixed latency guarantee"},
            "output_view":"summary|full" if operation=="analyze" else "full",
            "stable_fields":[".ok",".error",".data.analysis_status",".data.fund_flows"] if operation=="analyze" else
                            [".ok",".error",".data.template",".data.parameter_diff",".data.preview"] if operation=="plan" else
                            [".ok",".error",".data.data",".data.payload_unverified",".data.verification",".data.parameter_diff",".data.patch_proof",".data.tail_unchanged"] if operation=="patch" else
                            [".ok",".error",".data.source",".data.complete",".data.next_cursor",".data.actions" if operation=="actions" else ".data.items"],
            "examples":["python transaction.py analyze --chain bsc --hash TX_HASH --view summary"] if operation=="analyze" else
                       ["python transaction.py plan --chain bsc --hash TX_HASH --wallet trading"] if operation=="plan" else
                       ["python raw.py patch --chain sepolia --to CONTRACT --address SENDER --data CALLDATA --word 6 --type address --value RECIPIENT --verify",
                        "python raw.py patch --chain sepolia --from-trace TX_HASH --frame path:0.4 --address SENDER --set 1=5 --set 2=7 --verify"] if operation=="patch" else
                       [f"python address.py {operation} --chain bsc --address ADDRESS --limit 50"],
            "constraints":{"summary_max_utf8_bytes":2048,"plan_can_broadcast":False,"amount_unit":"smallest units; decimal integer strings"}}


def basic_description(module, operation, accepted):
    result = {"schema_version":1, "operation":f"{module}.{operation}", "accepted_fields":accepted}
    if "args_file" in accepted:
        result["args_file"] = "Local UTF-8 JSON array path. Provide either args or args_file."
    if (module, operation) == ("contract", "write"):
        result.update(description="Uses latest/pending live state. No no_cache field. Preview requires a wallet and explicit ABI; execute broadcasts.",
                      required_fields=["chain","wallet","contract","abi","function"])
    if (module, operation) == ("contract", "math"):
        from .position_math import MATH_FIELDS
        result.update(description="Offline V3/V4 Q64.96 price and principal math. Amounts are raw.",
                      formula_fields={name:sorted(fields) for name,fields in MATH_FIELDS.items()},
                      examples=["python contract.py math tick2sqrt --tick -176040"])
    if (module, operation) == ("contract", "storage"):
        result.update(description="Read one storage word at a pinned mined block. Optional layout lists fields from low to high bits.",
                      required_fields=["chain","contract"],
                      source_options=[["slot"],["slot_hex"],["mapping_key","base_slot"]])
    return result


def contract_describe(operation):
    from .operations import OPS
    from pathlib import Path
    from .introspect import LIMITS
    properties={}
    for field in sorted(OPS['contract'][operation]):
        row={'type':'string'}
        if field in LIMITS:
            default,low,high=LIMITS[field];row={'type':'integer','minimum':low,'maximum':high,'default':default}
        elif field in ('auto','external','include_source','no_semantics','no_cache'):
            row={'type':'boolean','default':field in ('auto','external')}
            if field=='auto':row['description']='Automatic when functions is omitted; explicit auto=true merges automatic and requested probes'
        elif field=='functions':row={'type':'array','maxItems':1000,'items':{'oneOf':[{'type':'string'},{'type':'object','properties':{'function':{'type':'string'},'args':{'type':'array'}},'required':['function'],'additionalProperties':False}]}}
        elif field=='view':row.update(enum=['summary','full'],default='summary')
        elif field=='amount_unit':row.update(enum=['shares','assets'],default='shares')
        elif field=='pattern':
            pattern_dir = Path(__file__).with_name("patterns")
            row.update(enum=['auto']+[p.stem for p in sorted(pattern_dir.glob('*.yaml'))],default='auto')
        properties[field]=row
    constraints=[{'not':{'properties':{'view':{'const':'summary'},'include_source':{'const':True}},'required':['include_source']}}]
    if operation=='diagnose':constraints.append({'not':{'required':['amount','amount_raw']}})
    return {'schema_version':1,'operation':'contract.'+operation,'when_to_use':['Inspect an address and its verified interface at a pinned block' if operation=='introspect' else 'Assess supported read-only exit preconditions and unresolved dependencies'],
        'when_not_to_use':['Not a security audit or a guarantee of withdrawal','No mutating-function simulation, private keys, signing or broadcasting'],
        'input_schema':{'type':'object','properties':properties,'required':['chain','address'],'additionalProperties':False,'allOf':constraints},
        'cost':{'http_requests':'150 by default; batched RPC subcalls reported separately','time_s':'60-second default budget; partial components retained'},
        'output_view':'summary|full','stable_fields':['.ok','.error','.data.structure','.data.verdict' if operation=='diagnose' else '.data.probes','.data.block','.data.trimmed'],
        'constraints':{'summary_max_utf8_bytes':2048,'can_broadcast':False,'state_values_cached':False,'amount_default_unit':'shares'},
        'examples':['python contract.py '+operation+' --chain arbitrum --address CONTRACT --view full']}

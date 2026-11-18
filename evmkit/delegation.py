"""Known manager payloads are declarations, never fabricated trace frames."""
import rlp
from eth_keys import keys
from web3 import Web3
from .abi import decode_call
from .common import address,hexdata,require,uint,ToolError

REDEEM="redeemDelegations(bytes[],bytes32[],bytes[])"
REDEEM_SELECTOR="0xcef6d209"


def _number(value):
    if isinstance(value,(bytes,bytearray)):return int.from_bytes(value,"big")
    return int(value,16) if isinstance(value,str) and value.startswith("0x") else uint(value)


def authorization_intents(transaction,chain_id):
    result=[]
    for auth in (transaction.get("authorizationList") or [])[:1000]:
        try:
            chain=_number(auth.get("chainId",auth.get("chain_id")));nonce=_number(auth["nonce"])
            target=address(auth["address"]);parity=_number(auth.get("yParity",auth.get("y_parity")))
            r,s=_number(auth["r"]),_number(auth["s"])
            require(chain in (0,chain_id) and nonce<2**64-1 and parity in (0,1),"Invalid authorization intent")
            require(0<s<=0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0,"Noncanonical authorization signature")
            digest=Web3.keccak(b"\x05"+rlp.encode([chain,bytes.fromhex(target[2:]),nonce]))
            authority=keys.Signature(vrs=(parity,r,s)).recover_public_key_from_msg_hash(digest).to_checksum_address()
            result.append({"authority":authority,"implementation":target,"clears_delegation":int(target,16)==0,"scope":"signed_authorization_intent","execution_proven":False})
        except Exception: continue
    return result


def declared_calls(transaction,chain,limit=1000):
    data=hexdata(transaction["data"])
    manager=address(transaction["to"]) if transaction.get("to") else None
    trusted=[address(v) for v in chain.get("delegation_managers",[])]
    out={"source":"encoded_execution","status":"not_applicable","manager":manager,"calls":[],"execution_observed":False}
    if data[:10]!=REDEEM_SELECTOR:return out
    if manager not in trusted:
        out.update(status="untrusted_manager",reason="Configure the chain-specific manager address before decoding execution layouts");return out
    decoded=decode_call(data,REDEEM);contexts,modes,executions=decoded["args"]
    require(len(contexts)==len(modes)==len(executions),"Delegation batch array lengths differ","DECODE_ERROR")
    out["status"]="complete";out["unsupported_executions"]=[]
    for i,(mode,payload) in enumerate(zip(modes,executions)):
        if i>=limit:out["status"]="partial";break
        raw=bytes.fromhex(payload[2:])
        if mode!="0x"+"00"*32 or len(raw)<52:
            out["unsupported_executions"].append(i);out["status"]="partial";continue
        target=address("0x"+raw[:20].hex());input_="0x"+raw[52:].hex()
        out["calls"].append({"source":"encoded_execution","execution_index":i,"path":None,"depth":None,"type":"CALL","from":None,
                             "to":target,"execution_context":None,"value_wei":str(int.from_bytes(raw[20:52],"big")),"input":input_,
                             "selector":input_[:10] if len(input_)>=10 else None,"input_bytes":len(raw)-52,
                             "committed":None,"execution_observed":False,"payload_unverified":True})
    return out


def classify_delegation(analyzer,tx,internals,sender,declared,counterpart=None):
    pattern=tx["data"][:10]==REDEEM_SELECTOR
    intents=authorization_intents(tx,analyzer.chain["chain_id"])
    actors={r.get("from") for r in internals.get("calls",[])}|{r.get("to") for r in internals.get("calls",[])}
    executed={r.get("to") for r in internals.get("calls",[])}|{r.get("execution_context") for r in internals.get("calls",[])}|{r.get("from") for r in internals.get("calls",[]) if r.get("path")}
    full_trace=internals.get("source")=="rpc.callTracer" and internals.get("complete") is True
    markers={};marker_scopes={};scopes=[];evidence=[]
    if sender and sender.get("account_type")=="delegated_eoa" and (sender["address"] in executed or pattern and not full_trace or sender["address"]==tx.get("to")):
        markers[sender["address"]]=sender.get("implementation");marker_scopes[sender["address"]]=sender.get("implementation_scope","block_end");scopes.append(marker_scopes[sender["address"]]);evidence.append("delegation_code_marker")
    if counterpart and counterpart.get("account_type")=="delegated_eoa":
        markers[counterpart["address"]]=counterpart.get("implementation");scopes.append(counterpart.get("implementation_scope","block_end"));evidence.append("executed_target_delegation_marker")
        marker_scopes[counterpart["address"]]=counterpart.get("implementation_scope","block_end")
    checks=[r for r in internals.get("calls",[]) if (r.get("input") or "")[:10]=="0x1626ba7e"]
    if (pattern or declared["status"]=="complete") and internals.get("source")=="rpc.callTracer":
        pre=analyzer.attempt(lambda:analyzer.query("debug_traceTransaction",[tx["hash"] if isinstance(tx["hash"],str) else Web3.to_hex(tx["hash"]),
                     {"tracer":"prestateTracer","tracerConfig":{"disableStorage":True},"timeout":"10s"}]),"DELEGATION_PRESTATE")
        if isinstance(pre,dict) and "pre" not in pre:
            for who,info in pre.items():
                if not isinstance(info,dict) or not isinstance(info.get("code"),str):continue
                code=info["code"].lower()
                if len(code)==48 and code.startswith("0xef0100") and address(who) in executed:
                    markers[address(who)]=address("0x"+code[8:]);marker_scopes[address(who)]="transaction_prestate";scopes.append("transaction_prestate");evidence.append("transaction_prestate_delegation_marker")
        candidates=list(dict.fromkeys([r.get("to") for r in checks]+[r.get("from") for r in internals.get("calls",[]) if r.get("type")=="CALL" and r.get("path")]))
        for who in candidates[:analyzer.options["max_addresses"]]:
            if not who or who in markers or int(who,16)<=0x11:continue
            code=analyzer.attempt(lambda:analyzer.code(who),"DELEGATION_ACCOUNT_CODE")
            if code and len(code)==48 and code.startswith("0xef0100"):
                markers[address(who)]=address("0x"+code[8:]);marker_scopes[address(who)]="block_end";scopes.append("block_end");evidence.append("block_end_delegation_marker")
    relevant=[i for i in intents if not i["clears_delegation"] and (i["authority"] in executed if full_trace else i["authority"] in actors or i["authority"]==address(tx["from"]))]
    if relevant:evidence.append("signed_7702_authorization_intent")
    if not (pattern or markers):return None
    if declared["status"] in ("complete","partial"):evidence.append("configured_manager_and_canonical_execution_payload")
    if checks:evidence.append("erc1271_call_pattern")
    if any(r.get("to") and int(r["to"],16)==1 for r in internals.get("calls",[])):evidence.append("ecrecover_precompile_call")
    delegators=list(dict.fromkeys(list(markers)+[i["authority"] for i in relevant]))
    recognized=declared["status"] in ("complete","partial") or bool(markers)
    if not recognized and not relevant:return None
    is7702=bool(markers or relevant)
    # Authorizations can change code between transaction prestate and execution.
    # Without observing authorization acceptance, a prestate pointer is insufficient.
    authorization_affected={i["authority"] for i in intents}&set(markers)
    if authorization_affected:evidence.append("authorization_may_change_prestate_delegation")
    observed=bool(markers and all(s=="transaction_prestate" for s in marker_scopes.values()) and full_trace and not authorization_affected)
    return {"type":"eip7702_delegated_execution" if is7702 else "delegation_execution",
            "delegator":delegators[0] if len(delegators)==1 else None,"delegators":delegators,
            "delegation_accounts":[{"address":who,"implementation":target,"scope":marker_scopes[who]} for who,target in markers.items()],
            "manager":declared["manager"] if pattern else None,"confidence":"high" if observed else "medium" if recognized else "low",
            "evidence":list(dict.fromkeys(evidence)),"evidence_scope":"transaction_prestate_and_trace" if observed else ",".join(sorted(set(scopes))) or "authorization_intent_and_declared_calldata",
            "authorization_intents":relevant,"status":"observed" if observed else "candidate",
            "requires_original_authorization_context":True,
            "committed":bool(analyzer.inspection["receipt"]["status"]) if analyzer.inspection.get("receipt") else None,
            "note":"Copying the outer calldata does not reproduce its authorization context; select and simulate a business CALL from your own wallet"}

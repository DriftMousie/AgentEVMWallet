"""Reference transaction to unsigned live preview; this operation cannot execute."""
from copy import deepcopy
from .abi import canonical,decode_call,encode_call
from .common import ToolError,address,require,txhash,uint
from .engine import Engine
from .standards import V2_ROUTER

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


def plan(tool,params):
    chain,rpc=tool.rpc(params)
    wallet=tool.config.wallet(params["wallet"],signing=False)
    overrides=params.get("override",{})
    require(isinstance(overrides,dict) and not set(overrides)-{"args","amount","value_wei"},"override supports args, amount and value_wei only")
    source=tool.transaction_decode({k:v for k,v in params.items() if k not in {"wallet","override","gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}})
    tx=source["inspection"]["transaction"]
    require(tx.get("to"),"Contract creation cannot be reused as a call template")
    decoded=deepcopy(source["decoded"])
    safe=decoded.get("status")=="decoded" and decoded.get("template_safe") is True
    args=deepcopy(decoded.get("args",[]));old_args=deepcopy(args);diff=[]
    signature=decoded.get("signature")
    names=list(decoded.get("named_args",{}))
    if safe:
        recipient=RECIPIENTS.get(signature)
        if recipient is not None: args[recipient]=wallet["address"]
    if "args" in overrides or "amount" in overrides:
        require(safe,"Editable parameters require an explicit/confirmed ABI or signature; raw calldata can only be previewed unchanged","ABI_REQUIRED")
        supplied=overrides.get("args",{})
        if isinstance(supplied,list):
            require(len(supplied)==len(args),"Override argument count differs")
            args=deepcopy(supplied)
        else:
            require(isinstance(supplied,dict),"override.args must be an array or named object")
            for name,value in supplied.items():
                require(name in names and names.count(name)==1,"Override argument name is unknown or ambiguous")
                args[names.index(name)]=value
        if "amount" in overrides:
            position=AMOUNTS.get(signature)
            require(position is not None,"amount shorthand is ambiguous; use override.args with explicit parameter names")
            require(not isinstance(supplied,list) and (not names or names[position] not in supplied),"Do not override the same amount twice")
            args[position]=str(uint(overrides["amount"],"amount in smallest units"))
    value=str(uint(overrides.get("value_wei",tx["value"]),"value_wei"))
    if safe:
        data=encode_call(signature,args)
        updated=decode_call(data,signature)
        args=updated["args"]
        decoded.update(updated,named_args=dict(zip(names,args)))
        for i,(old,new) in enumerate(zip(old_args,args)):
            if old!=new: diff.append({"parameter":names[i] if i<len(names) else f"arg{i}","before":old,"after":new})
    else:
        data=source["data"];decoded={"status":"unknown","selector":data[:10] if len(data)>=10 else None}
    if str(tx["value"])!=value: diff.append({"parameter":"value_wei","before":str(tx["value"]),"after":value})
    diff.insert(0,{"parameter":"transaction.from","before":tx["from"],"after":wallet["address"]})
    notes=["Wallet-bound signatures/proofs, owner fields and deadlines are not rewritten", "Preview is not an authorization to broadcast"]
    if not safe: notes.append("Unconfirmed calldata is unchanged; recipient and semantics may still refer to third parties")
    template={"schema_version":1,"source":{"chain_id":chain["chain_id"],"hash":txhash(params["hash"]),"sender":tx["from"],"status":source["inspection"]["status"]},
              "to":address(tx["to"]),"value_wei":value,"data":data,"decoded":decoded,"warnings":notes}
    result={"status":"preview","template":template,"parameter_diff":diff,"warnings":notes,
            "source_status":source["inspection"]["status"],"template_safe":safe}
    live={k:v for k,v in params.items() if k in {"gas","gas_price_wei","max_fee_per_gas_wei","max_priority_fee_per_gas_wei"}}
    live["execute"]=False
    try:
        preview=Engine(tool.config,chain,rpc).send(wallet,template["to"],int(value),data,live,
            erc20=safe and signature in ("transfer(address,uint256)","transferFrom(address,address,uint256)","approve(address,uint256)"))
        result["preview"]=preview
    except ToolError as exc:
        result.update(status="blocked",simulation={"status":"failed","reason":exc.code},preview=None)
        raise ToolError(exc.code,exc.message,retryable=exc.retryable,data=result) from None
    return result

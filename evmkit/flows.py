"""Observed asset flows, not an assertion about arbitrary token storage changes."""
from .common import address
from .standards import ERC20, ERC721, ERC1155
from .abi import canonical
from web3 import Web3


def fund_flows(analyzer, transaction, receipt, internals, changes):
    focus = address(analyzer.params.get("focus_address") or transaction["from"])
    failed = receipt is not None and receipt["status"] == 0
    canonical_ok = changes.get("canonicality") == "verified" and analyzer.inspection["status"] != "reorged"
    trace_complete = internals.get("source") == "rpc.callTracer" and internals.get("complete") is True
    complete_native = canonical_ok and receipt is not None and (failed or trace_complete)
    spent,received = 0,0
    legs=[]
    for order,row in enumerate(internals.get("calls",[]) if not failed else []):
        if row.get("committed") is not True or not row.get("may_carry_value"): continue
        source = address(row["from"]) if row.get("from") else None
        destination = address(row["to"]) if row.get("to") else None
        if source == destination: continue
        value = int(row.get("value_wei") or 0)
        if source == focus: spent += value
        if destination == focus: received += value
        if value>0 and focus in (source,destination):
            legs.append({"asset_type":"native","address":None,"from":source,"to":destination,"amount_raw":str(value),
                         "order":order,"direction":"out" if source==focus else "in","call_path":row.get("path"),"source":"rpc.callTracer"})
    fee = int(analyzer.inspection.get("execution_fee_paid_wei") or 0) if receipt and address(transaction["from"]) == focus else 0
    net = spent-received if complete_native else None
    actual_delta = next((r["delta_raw"] for r in changes.get("native_transaction",[]) if address(r["address"]) == focus),None) if canonical_ok else None
    native = {"spent_wei":str(spent) if complete_native else None, "received_wei":str(received) if complete_native else None,
              "refund_wei":None, "net_wei":str(net) if net is not None else None,
              "fee_wei":str(fee) if receipt else None,"fee_scope":"EVM execution; excludes chain-specific and blob/data fees",
              "net_with_fee_wei":str(net+fee) if net is not None else None,
              "balance_delta_wei":actual_delta,"top_level_value_wei":str(transaction.get("value",0)),
              "symbol":analyzer.chain.get("symbol"),"decimals":analyzer.chain["decimals"],
              "status":"complete" if complete_native else "partial", "source":"receipt_reverted" if failed else "rpc.callTracer" if trace_complete else "top_level_only"}
    reconciled = net is not None and actual_delta is not None and int(actual_delta)==-(net+fee)
    native["reconciliation"]="matches" if reconciled else "mismatch_or_additional_fees" if net is not None and actual_delta is not None else "not_available"
    if native["reconciliation"]=="mismatch_or_additional_fees": native["status"]="partial"
    decoded = analyzer.memo.get("root_decode",{})
    if complete_native and decoded.get("template_safe") and decoded.get("signature","").startswith("swapETHForExactTokens(") and focus == address(transaction["from"]):
        native["refund_wei"] = str(sum(int(r.get("value_wei") or 0) for r in internals.get("calls",[])
                                      if r.get("committed") is True and r.get("may_carry_value") and r.get("from") == transaction.get("to") and r.get("to") == focus))
    tokens,nfts = {},{}
    transfer_valid=True
    layouts = [item for abi in (ERC20,ERC721,ERC1155) for item in abi
               if item.get("type")=="event" and item["name"] in ("Transfer","TransferSingle","TransferBatch")]
    logs = receipt.get("logs",[]) if receipt else []
    for position,raw in enumerate(logs[:analyzer.options["max_frames"]] if receipt and not failed else []):
        args,event = None,None
        for item in layouts:
            try: args=analyzer.decode_event_item(raw,item);event=item["name"];break
            except Exception: continue
        if args is None:
            topic=raw.get("topics",[None])[0] if raw.get("topics") else None
            if topic is not None:
                topic=topic.lower() if isinstance(topic,str) else Web3.to_hex(topic)
                if topic in {Web3.to_hex(Web3.keccak(text=canonical(item))) for item in layouts}: transfer_valid=False
            continue
        source,destination = address(args["from"]),address(args["to"])
        if focus not in (source,destination): continue
        location=address(raw["address"])
        if event == "Transfer" and "value" in args:
            row=tokens.setdefault(location,{"address":location,"in":0,"out":0})
            if destination==focus: row["in"]+=int(args["value"])
            if source==focus: row["out"]+=int(args["value"])
            if source!=destination and int(args["value"])>0:
                index=raw.get("logIndex")
                if isinstance(index,str): index=int(index,16) if index.startswith("0x") else int(index)
                legs.append({"asset_type":"erc20","address":location,"from":source,"to":destination,"amount_raw":str(args["value"]),
                             "order":position,"direction":"out" if source==focus else "in","receipt_position":position,"log_index":index,"source":"receipt.Transfer"})
        else:
            ids = args.get("ids",[args.get("tokenId",args.get("id"))])
            quantities = args.get("values",[args.get("value",1)])
            if len(ids)!=len(quantities): transfer_valid=False;continue
            for token_id,quantity in zip(ids,quantities):
                row=nfts.setdefault((location,str(token_id)),{"address":location,"token_id":str(token_id),"standard":"erc721" if event=="Transfer" else "erc1155","in":0,"out":0})
                if destination==focus: row["in"]+=int(quantity)
                if source==focus: row["out"]+=int(quantity)
    token_rows=[]
    for location,row in tokens.items():
        meta=analyzer.metadata(location)
        row.update(symbol=meta["symbol"] or "?",decimals=meta["decimals"],metadata_status=meta["status"],
                   net=str(row["in"]-row["out"]),source="receipt.Transfer",confidence="logs_derived",is_exact_balance_change=False)
        row["in"],row["out"]=str(row["in"]),str(row["out"])
        token_rows.append(row)
    nft_rows=[]
    for row in nfts.values():
        row.update(net=str(row["in"]-row["out"]),source="receipt.transfer_event",confidence="logs_derived")
        row["in"],row["out"]=str(row["in"]),str(row["out"]);nft_rows.append(row)
    has_receipt = receipt is not None and canonical_ok
    asset_complete = has_receipt and transfer_valid and len(logs)<=analyzer.options["max_frames"]
    positive = any(int(row["net"])>0 for row in token_rows+nft_rows) or net is not None and net<0
    for row in token_rows+nft_rows:
        value=int(row["net"])
        row["role"]="received" if value>0 else "payment" if value<0 and positive else "sent" if value<0 else "mixed"
    refunds=[]
    participants={r.get(key) for r in internals.get("calls",[]) if r.get("committed") is True for key in ("from","to")}
    for asset in dict.fromkeys((r["asset_type"],r["address"]) for r in legs):
        outgoing=[r for r in legs if (r["asset_type"],r["address"])==asset and r["direction"]=="out"]
        incoming=[r for r in legs if (r["asset_type"],r["address"])==asset and r["direction"]=="in"]
        eligible=[r for r in incoming if any(o["order"]<r["order"] and (o["to"]==r["from"] or r["from"] in participants and o["to"] in participants) for o in outgoing)]
        if not eligible or sum(int(r["amount_raw"]) for r in eligible)>sum(int(r["amount_raw"]) for r in outgoing):continue
        for leg in eligible:
            confirmed=asset[0]=="native" and native["refund_wei"] is not None and leg["from"]==transaction.get("to")
            meta=next((r for r in token_rows if r["address"]==asset[1]),{}) if asset[0]=="erc20" else native
            decimals=meta.get("decimals");symbol=meta.get("symbol") or "?"
            from .common import formatted
            refunds.append({k:v for k,v in leg.items() if k not in ("order","direction")} | {
                "kind":"confirmed_refund" if confirmed else "refund_candidate","confidence":"high" if confirmed else "medium" if trace_complete else "low",
                "symbol":symbol,"decimals":decimals,"amount_readable":formatted(int(leg["amount_raw"]),decimals)+" "+symbol if decimals is not None else None,
                "evidence":["same_asset","outflow_before_inflow","same_counterparty_or_committed_route"],
                "included_in":"native.received_wei/net_wei" if asset[0]=="native" else "tokens.in/net"})
    return {"focus_address":focus,"native":native,"tokens":token_rows,"nfts":nft_rows,"refunds":refunds,
            "status":"complete" if native["status"]=="complete" and asset_complete else "partial",
            "tokens_status":"complete" if asset_complete else "partial",
            "confidence":"event_and_trace_derived" if complete_native else "logs_derived",
            "scope":"Observed standard transfer events and committed native value calls for the focus address",
            "limitations":["Transfer logs do not prove exact storage balances or all nonstandard asset changes", "Unknown native inflow is not zero", "Native fee fields cover EVM execution only"]}


def infer_intent(classification, flows, decoded, chain):
    if classification.get("inferred_intent"): return None
    if classification.get("type") not in ("unknown","contract_call"): return None
    native=flows["native"]
    token_in=[r for r in flows["tokens"] if int(r["net"])>0]
    token_out=[r for r in flows["tokens"] if int(r["net"])<0]
    wrapped=chain.get("wrapped_native_address")
    wrapped_out=any(wrapped and address(r["address"])==address(wrapped) for r in token_out)
    wrapped_in=any(wrapped and address(r["address"])==address(wrapped) for r in token_in)
    nft_in=any(int(r["net"])>0 for r in flows["nfts"])
    evidence=[];value=None
    if native["net_wei"] is not None and int(native["net_wei"])>0 and wrapped_in and len(token_in)==1 and not token_out and not nft_in:
        value="wrapped_native_deposit";evidence=["native_net_out","configured_wrapped_native_in"]
    elif native["net_wei"] is not None and int(native["net_wei"])<0 and wrapped_out and len(token_out)==1 and not token_in:
        value="wrapped_native_withdraw";evidence=["configured_wrapped_native_out","native_net_in"]
    elif nft_in and (native["net_wei"] is not None and int(native["net_wei"])>0 or token_out):
        value="payment_for_nft";evidence=["observed_asset_net_out","nft_net_in"]
    elif (native["net_wei"] is not None and int(native["net_wei"])>0 or wrapped_out) and token_in:
        value="payment_for_tokens";evidence=["native_or_wrapped_net_out","token_net_in"]
    elif token_in and token_out:
        value="token_exchange";evidence=["token_net_out","other_token_net_in"]
    elif token_out and not token_in:
        value="token_outflow";evidence=["token_net_out","no_observed_token_inflow"]
    elif token_in:
        value="token_inflow";evidence=["token_net_in"]
    if not value: return None
    if decoded.get("status")!="decoded": evidence.append("no_confirmed_function_signature")
    return {"value":value,"confidence":"medium" if flows["status"]=="complete" else "low",
            "evidence":evidence,"scope":"observed_flows; not proof of business semantics"}

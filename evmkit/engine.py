from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager, nullcontext

from web3 import Web3

from .common import ToolError, require, uint, address, hexdata, txhash, jsonable, formatted
from .execution import guarded_execution


class Journal:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "transactions.sqlite3"
        # Create with restricted permissions before opening SQLite.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS transactions (hash TEXT PRIMARY KEY, chain_id INTEGER, sender TEXT, nonce INTEGER, tx TEXT, status TEXT, updated REAL)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lock(self, chain_id, sender):
        name = self.directory / f"{chain_id}-{sender.lower()}.lock"
        fd = os.open(name, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, hash_, chain_id, sender, tx, status):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (hash_, chain_id, sender, tx["nonce"], json.dumps(tx), status, time.time()))

    def status(self, hash_, status):
        with self.connect() as db:
            db.execute("UPDATE transactions SET status=?, updated=? WHERE hash=?", (status, time.time(), hash_))

    def get(self, hash_):
        with self.connect() as db:
            row = db.execute("SELECT chain_id,sender,tx,status FROM transactions WHERE hash=?", (hash_,)).fetchone()
        require(row, "No local transaction record", "NOT_FOUND")
        return {"chain_id": row[0], "sender": row[1], "tx": json.loads(row[2]), "status": row[3]}

    def unresolved(self, chain_id, sender):
        with self.connect() as db:
            return db.execute("SELECT hash,nonce FROM transactions WHERE chain_id=? AND sender=? AND status IN ('signed','unknown')", (chain_id, sender)).fetchall()

    def next_nonce(self, chain_id, sender, pending):
        with self.connect() as db:
            maximum = db.execute("SELECT MAX(nonce) FROM transactions WHERE chain_id=? AND sender=?", (chain_id, sender)).fetchone()[0]
        return max(pending, maximum + 1 if maximum is not None else pending)


def fees(rpc, chain, params):
    gas_price = params.get("gas_price_wei")
    max_fee, priority = params.get("max_fee_per_gas_wei"), params.get("max_priority_fee_per_gas_wei")
    require(gas_price is None or (max_fee is None and priority is None), "Legacy and EIP-1559 fee fields are mutually exclusive")
    block = rpc.read(lambda w: w.eth.get_block("latest"))
    base = block.get("baseFeePerGas")
    mode = chain.get("fee_mode", "auto")
    use1559 = gas_price is None and (max_fee is not None or priority is not None or mode == "eip1559" or (mode == "auto" and base is not None))
    if use1559:
        require(base is not None, "Chain does not expose EIP-1559 base fee")
        if priority is None:
            priority = rpc.read(lambda w: w.eth.max_priority_fee)
        priority = uint(priority, "priority fee")
        max_fee = uint(max_fee, "max fee") if max_fee is not None else int(base) * 2 + priority
        require(max_fee >= priority and max_fee >= base, "Max fee below priority or current base fee")
        return {"type": 2, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}
    require(mode != "eip1559", "Chain configuration requires EIP-1559")
    return {"gasPrice": uint(gas_price, "gas price") if gas_price is not None else rpc.read(lambda w: w.eth.gas_price)}


def fee_summary(transaction):
    """Human/agent-facing rates; never pass this representation to the signer."""
    names = ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
    return {
        "fee_model": "eip1559" if "maxFeePerGas" in transaction else "legacy",
        "rate_unit": "per_gas",
        "fees": {name: {"wei": str(uint(transaction[name])), "gwei": formatted(transaction[name], 9)}
                 for name in names if name in transaction},
    }


def wait_options(confirmations, timeout):
    confirmations, timeout = uint(confirmations), uint(timeout)
    require(confirmations >= 1, "Confirmations must be at least one")
    require(timeout > 0, "Confirmation timeout must be positive")
    return confirmations, timeout


def minted_nfts(receipt):
    """Receipt-reported ERC-721 mint IDs; not a prediction of future IDs."""
    result=[]
    topic=Web3.keccak(text="Transfer(address,address,uint256)")
    logs=receipt.get("logs",[])
    if not isinstance(logs,(list,tuple)):return result
    for position,row in enumerate(logs):
        try:
            raw_topics=row.get("topics",[])
            if not isinstance(raw_topics,(list,tuple)) or len(raw_topics)!=4:continue
            if any(not isinstance(t,(str,bytes,bytearray)) or len(t)!=(66 if isinstance(t,str) else 32) for t in raw_topics):continue
            topics=[bytes.fromhex(t[2:]) if isinstance(t,str) else bytes(t) for t in raw_topics]
            if len(topics)!=4 or topics[0]!=topic or topics[1]!=bytes(32) or any(len(t)!=32 for t in topics):continue
            data=row.get("data",b"")
            if data not in (b"","0x") or topics[2][:12]!=bytes(12):continue
            result.append({"address":address(row["address"]),"recipient":address("0x"+topics[2][-20:].hex()),
                           "token_id":str(int.from_bytes(topics[3],"big")),"receipt_position":position,"source":"receipt.ERC721_Transfer_from_zero"})
        except Exception:continue
    return result


def execution_revert_details(rpc,hash_):
    """Best-effort exact transaction trace; never substitute a later eth_call."""
    try:
        from .network import Budget
        from .abi import decode_revert
        trace=rpc.optional_request("debug_traceTransaction",[hash_,{"tracer":"callTracer","tracerConfig":{"onlyTopCall":True},"timeout":"2s"}],Budget(seconds=3,requests_limit=2))
        raw=trace.get("output") if isinstance(trace,dict) else None
        if isinstance(raw,str) and len(raw)<=131074:
            return {**decode_revert(raw),"revert_details_status":"available","revert_source":"debug_traceTransaction.callTracer"}
    except Exception:pass
    return {"revert_details_status":"unavailable"}


def wait_receipt(rpc, hash_, confirmations=1, timeout=120):
    hash_=txhash(hash_)
    confirmations, timeout = wait_options(confirmations, timeout)
    deadline=time.monotonic()+timeout
    progress={"hash":hash_, "status":"broadcast_unconfirmed", "receipt":None,
              "confirmations":0, "required_confirmations":confirmations, "phase":"confirmation"}
    while True:
        try:
            receipt=rpc.read(lambda w:w.eth.get_transaction_receipt(hash_))
            progress.update(receipt=receipt, confirmations=0, canonicality="unchecked")
            canonical=rpc.read(lambda w:w.eth.get_block(receipt["blockNumber"]))
            if canonical["hash"]==receipt["blockHash"]:
                progress["canonicality"]="verified"
                height=rpc.read(lambda w:w.eth.block_number)
                progress["confirmations"]=max(0,height-receipt["blockNumber"]+1)
                if progress["confirmations"]>=confirmations:
                    require(receipt.get("status") in (0,1), "Receipt has no supported execution status", "INVALID_RESPONSE")
                    return receipt
            else:
                progress["canonicality"]="reorged"
        except ToolError as exc:
            if exc.code!="NOT_FOUND":
                progress["confirmation_error"]={"code":exc.code}
                raise ToolError("CONFIRMATION_UNAVAILABLE", "Receipt/canonical block confirmation query unavailable; transaction is not classified as reverted", retryable=True, data=progress) from None
            # A previous observation may now be missing; retain it but invalidate it.
            progress.update(confirmations=0, canonicality="receipt_unavailable")
        except Exception:
            raise ToolError("CONFIRMATION_UNAVAILABLE", "Confirmation response could not be checked", retryable=True, data=progress) from None
        if time.monotonic()>=deadline:
            raise ToolError("CONFIRMATION_TIMEOUT", "Transaction has not reached the requested confirmations", retryable=True, data=progress)
        time.sleep(min(0.5,max(0,deadline-time.monotonic())))


class Engine:
    def __init__(self, config, chain, rpc):
        self.config, self.chain, self.rpc = config, chain, rpc

    def _journal_status(self, journal, context, state, output):
        try:journal.status(context["hash"],state)
        except Exception:
            output.setdefault("warnings",[]).append("JOURNAL_UPDATE_FAILED: transaction hash and chain outcome are preserved; inspect the original hash before sending again")

    def _wait(self, context, journal, output, params):
        context["phase"]="confirmation"
        try:
            receipt=wait_receipt(self.rpc,context["hash"],params.get("confirmations",self.chain["confirmations"]),params.get("timeout",120))
        except ToolError as exc:
            if isinstance(exc.data,dict):context.update(exc.data)
            raise ToolError(exc.code,exc.message,retryable=exc.retryable,data={**output,**(exc.data or {})}) from None
        context.update(receipt=receipt,status="confirmed" if receipt["status"]==1 else "reverted",phase="complete")
        output.update(context)
        output.pop("next", None)
        output.pop("suggested_next", None)
        if receipt["status"]==1:output["minted_nfts"]=minted_nfts(receipt)
        self._journal_status(journal,context,"confirmed" if receipt["status"]==1 else "failed",output)
        if receipt["status"]==0:
            output.update(execution_revert_details(self.rpc,context["hash"]))
            raise ToolError("EXECUTION_REVERTED", "Canonical confirmed receipt reports execution failure",data=output)
        return output

    def _broadcast(self, signed, context, journal, output):
        context.update(phase="broadcast",broadcast_attempted=True,status="broadcast_unknown")
        try:
            returned=self.rpc.send(signed.raw_transaction)
            require(Web3.to_hex(returned).lower()==context["hash"].lower(), "RPC returned an unexpected transaction hash", "BROADCAST_UNKNOWN")
        except Exception as exc:
            if isinstance(exc,ToolError) and isinstance(exc.data,dict) and exc.data.get("broadcast_attempted") is False:
                context.update(broadcast_attempted=False,status="signed_not_broadcast")
                raise ToolError(exc.code,exc.message,retryable=exc.retryable,data=output) from None
            self._journal_status(journal,context,"unknown",output)
            raise ToolError("BROADCAST_UNKNOWN", "Broadcast outcome unknown; query the original hash before explicitly replaying it",data=output) from None
        context.update(status="broadcast_unconfirmed",phase="broadcast_acknowledged")
        output.update(context)
        self._journal_status(journal,context,"broadcast",output)

    def _confirmation_hint(self, output, params):
        wait_params = {"chain":self.chain["alias"], "hash":output["hash"]}
        if params.get("confirmations") is not None:
            wait_params["confirmations"] = uint(params["confirmations"], "confirmations")
        output["next"] = "Wait for confirmation of this hash before reading balances or submitting a dependent transaction."
        output["suggested_next"] = [{"module":"transaction", "operation":"wait",
                                    "params":wait_params, "reason":output["next"]}]

    @guarded_execution
    def send(self, wallet, to, value, data, params, *, erc20=False, replacement=False, context):
        to,value,data=address(to),uint(value,"value"),hexdata(data)
        execute=params.get("execute",False)
        require(isinstance(execute,bool),"execute must be boolean")
        if params.get("wait") or params.get("confirmations") is not None:
            wait_options(params.get("confirmations",self.chain["confirmations"]), params.get("timeout",120))
        if execute:require(wallet["account"] is not None,"Watch-only wallet cannot sign","WALLET_ERROR")
        skip=params.get("skip_simulation",False)
        if skip:require(params.get("gas") is not None and isinstance(params.get("reason"),str) and params["reason"].strip(),"Skipping simulation requires gas and a reason")
        sender,chain_id=wallet["address"],self.chain["chain_id"]
        journal=Journal(self.config.state_dir) if execute else None
        with journal.lock(chain_id,sender) if journal else nullcontext():
            if journal:
                for previous,nonce in journal.unresolved(chain_id,sender):
                    try:self.rpc.read(lambda w:w.eth.get_transaction(previous))
                    except ToolError:
                        raise ToolError("BROADCAST_UNKNOWN","Resolve the previous uncertain transaction before sending",data={"previous_hash":previous}) from None
                    journal.status(previous,"broadcast")
            context["phase"]="nonce"
            pending=self.rpc.read(lambda w:w.eth.get_transaction_count(sender,"pending"))
            context["pending_nonce_before"]=pending
            if replacement:nonce=uint(params["nonce"],"nonce")
            else:
                require(params.get("nonce") is None,"Manual Nonce is only available for replacement operations")
                nonce=journal.next_nonce(chain_id,sender,pending) if journal else pending
            context["nonce"]=nonce
            tx={"chainId":chain_id,"from":sender,"to":to,"value":value,"data":data,"nonce":nonce}
            context["phase"]="fees";tx.update(fees(self.rpc,self.chain,params))
            simulation={"status":"skipped","reason":params.get("reason")} if skip else {}
            call_tx={k:v for k,v in tx.items() if k not in ("nonce","chainId","type")}
            if not skip:
                context["phase"]="simulation"
                output=self.rpc.read(lambda w:w.eth.call(call_tx,"pending"))
                if erc20 and output:require(len(output)==32 and int.from_bytes(output,"big")==1,"ERC-20 operation returned false or invalid data","TOKEN_OPERATION_FAILED")
                simulation={"status":"success","return_data":jsonable(output)}
            context["phase"]="gas_estimation"
            gas=uint(params["gas"],"gas") if params.get("gas") is not None else (self.rpc.read(lambda w:w.eth.estimate_gas(call_tx))*120+99)//100
            require(gas>0,"Gas must be positive");tx["gas"]=gas
            context["phase"]="balance"
            unit_fee=tx.get("maxFeePerGas",tx.get("gasPrice"))
            balance=self.rpc.read(lambda w:w.eth.get_balance(sender,"pending"))
            require(balance>=value+gas*unit_fee,"Insufficient native balance for value and execution fee cap","INSUFFICIENT_BALANCE")
            preview={"transaction":tx,"fee_summary":fee_summary(tx),"simulation":simulation,"execution_fee_cap_wei":str(gas*unit_fee),
                     "fee_scope":"EVM execution only; chain-specific L1/data fees are not included","status":"preview"}
            if not execute:return preview
            context["phase"]="signing"
            unsigned={k:v for k,v in tx.items() if k!="from"}
            signed=wallet["account"].sign_transaction(unsigned)
            context.update(hash=Web3.to_hex(signed.hash),status="signed_not_broadcast",phase="journal_save")
            try:journal.save(context["hash"],chain_id,sender,unsigned,"signed")
            except Exception:raise ToolError("JOURNAL_SAVE_FAILED","Cannot durably record the signed transaction; broadcasting was not attempted") from None
            self._broadcast(signed,context,journal,preview)
            self._confirmation_hint(preview, params)
            if params.get("wait"):return self._wait(context,journal,preview,params)
            return preview

    @guarded_execution
    def replay(self, wallet, hash_, params, *, context):
        hash_=txhash(hash_)
        if params.get("wait") or params.get("confirmations") is not None:
            wait_options(params.get("confirmations",self.chain["confirmations"]), params.get("timeout",120))
        journal=Journal(self.config.state_dir);record=journal.get(hash_)
        require(record["chain_id"]==self.chain["chain_id"] and record["sender"]==wallet["address"],"Record does not match chain and wallet")
        context.update(hash=hash_,nonce=record["tx"]["nonce"],status="signed_not_broadcast")
        if not params.get("execute"):return {"status":"preview","hash":hash_,"transaction":record["tx"]}
        require(wallet["account"],"Watch-only wallet cannot sign","WALLET_ERROR")
        with journal.lock(self.chain["chain_id"],wallet["address"]):
            context["phase"]="signing"
            signed=wallet["account"].sign_transaction(record["tx"])
            require(Web3.to_hex(signed.hash)==hash_,"Reconstructed transaction hash differs")
            context["phase"]="lookup_original"
            try:
                self.rpc.read(lambda w:w.eth.get_transaction(hash_));known=True
            except ToolError as exc:
                if exc.code!="NOT_FOUND":raise
                known=False
            out={"hash":hash_}
            if known:
                context.update(already_known=True,status="broadcast_unconfirmed")
                out.update(context);self._journal_status(journal,context,"broadcast",out)
            else:self._broadcast(signed,context,journal,out)
            self._confirmation_hint(out, params)
            if params.get("wait"):return self._wait(context,journal,out,params)
            return out

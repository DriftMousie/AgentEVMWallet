from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager, nullcontext

from web3 import Web3

from .common import ToolError, require, uint, address, hexdata, txhash, jsonable, formatted


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


def wait_receipt(rpc, hash_, confirmations=1, timeout=120):
    confirmations, timeout = uint(confirmations), uint(timeout)
    require(confirmations >= 1, "Confirmations must be at least one")
    deadline = time.monotonic() + timeout
    while True:
        try:
            receipt = rpc.read(lambda w: w.eth.get_transaction_receipt(hash_))
            # Check canonical block hash each poll, rather than counting a stale receipt.
            canonical = rpc.read(lambda w: w.eth.get_block(receipt["blockNumber"]))
            height = rpc.read(lambda w: w.eth.block_number)
            if canonical["hash"] == receipt["blockHash"] and height - receipt["blockNumber"] + 1 >= confirmations:
                return receipt
        except ToolError as exc:
            if exc.code != "NOT_FOUND": raise
        if time.monotonic() >= deadline:
            raise ToolError("CONFIRMATION_TIMEOUT", "Transaction not confirmed before timeout", data={"hash": hash_, "status": "unconfirmed"})
        time.sleep(0.5)


class Engine:
    def __init__(self, config, chain, rpc):
        self.config, self.chain, self.rpc = config, chain, rpc

    def send(self, wallet, to, value, data, params, *, erc20=False, replacement=False):
        to, value, data = address(to), uint(value, "value"), hexdata(data)
        execute = params.get("execute", False)
        require(isinstance(execute, bool), "execute must be boolean")
        if execute:
            require(wallet["account"] is not None, "Watch-only wallet cannot sign", "WALLET_ERROR")
        skip = params.get("skip_simulation", False)
        if skip:
            require(params.get("gas") is not None and isinstance(params.get("reason"), str) and params["reason"].strip(), "Skipping simulation requires gas and a reason")
        sender, chain_id = wallet["address"], self.chain["chain_id"]
        journal = Journal(self.config.state_dir) if execute else None
        with journal.lock(chain_id, sender) if journal else nullcontext():
            if journal:
                for hash_, nonce in journal.unresolved(chain_id, sender):
                    try:
                        self.rpc.read(lambda w: w.eth.get_transaction(hash_))
                        journal.status(hash_, "broadcast")
                    except ToolError:
                        raise ToolError("BROADCAST_UNKNOWN", "Resolve or replay previous uncertain transaction before sending", data={"hash": hash_}) from None
            pending = self.rpc.read(lambda w: w.eth.get_transaction_count(sender, "pending"))
            if replacement:
                nonce = uint(params["nonce"], "nonce")
            else:
                require(params.get("nonce") is None, "Manual Nonce is only available for replacement operations")
                nonce = journal.next_nonce(chain_id, sender, pending) if journal else pending
            tx = {"chainId": chain_id, "from": sender, "to": to, "value": value, "data": data, "nonce": nonce}
            tx.update(fees(self.rpc, self.chain, params))
            simulation = {"status": "skipped", "reason": params.get("reason")} if skip else {}
            call_tx = {k: v for k, v in tx.items() if k not in ("nonce", "chainId", "type")}
            if not skip:
                output = self.rpc.read(lambda w: w.eth.call(call_tx, "pending"))
                if erc20 and output:
                    require(len(output) == 32 and int.from_bytes(output, "big") == 1, "ERC-20 operation returned false or invalid data", "TOKEN_OPERATION_FAILED")
                simulation = {"status": "success", "return_data": jsonable(output)}
            gas = uint(params["gas"], "gas") if params.get("gas") is not None else (self.rpc.read(lambda w: w.eth.estimate_gas(call_tx)) * 120 + 99) // 100
            require(gas > 0, "Gas must be positive")
            tx["gas"] = gas
            unit_fee = tx.get("maxFeePerGas", tx.get("gasPrice"))
            balance = self.rpc.read(lambda w: w.eth.get_balance(sender, "pending"))
            require(balance >= value + gas * unit_fee, "Insufficient native balance for value and execution fee cap", "INSUFFICIENT_BALANCE")
            preview = {"transaction": tx, "fee_summary": fee_summary(tx), "simulation": simulation, "execution_fee_cap_wei": str(gas * unit_fee),
                       "fee_scope": "EVM execution only; chain-specific L1/data fees are not included", "status": "preview"}
            if not execute:
                return preview
            unsigned = {k: v for k, v in tx.items() if k != "from"}
            signed = wallet["account"].sign_transaction(unsigned)
            hash_ = Web3.to_hex(signed.hash)
            journal.save(hash_, chain_id, sender, unsigned, "signed")
            try:
                returned = self.rpc.send(signed.raw_transaction)
                require(Web3.to_hex(returned).lower() == hash_.lower(), "RPC returned an unexpected transaction hash", "BROADCAST_UNKNOWN")
            except ToolError:
                journal.status(hash_, "unknown")
                raise ToolError("BROADCAST_UNKNOWN", "Broadcast outcome unknown; query or replay this exact hash", data={"hash": hash_, "status": "unknown"}) from None
            journal.status(hash_, "broadcast")
            preview.update(hash=hash_, status="broadcast")
            if params.get("wait"):
                receipt = wait_receipt(self.rpc, hash_, params.get("confirmations", self.chain["confirmations"]), params.get("timeout", 120))
                journal.status(hash_, "confirmed" if receipt["status"] == 1 else "failed")
                preview.update(receipt=receipt, status="confirmed" if receipt["status"] == 1 else "failed")
                if receipt["status"] != 1:
                    raise ToolError("TRANSACTION_FAILED", "Transaction reverted on chain", data=preview)
            return preview

    def replay(self, wallet, hash_, params):
        hash_ = txhash(hash_)
        journal = Journal(self.config.state_dir)
        record = journal.get(hash_)
        require(record["chain_id"] == self.chain["chain_id"] and record["sender"] == wallet["address"], "Record does not match chain and wallet")
        if not params.get("execute"):
            return {"status": "preview", "hash": hash_, "transaction": record["tx"]}
        require(wallet["account"], "Watch-only wallet cannot sign", "WALLET_ERROR")
        with journal.lock(self.chain["chain_id"], wallet["address"]):
            signed = wallet["account"].sign_transaction(record["tx"])
            require(Web3.to_hex(signed.hash) == hash_, "Reconstructed transaction hash differs")
            try:
                self.rpc.read(lambda w: w.eth.get_transaction(hash_))
                journal.status(hash_, "broadcast")
                known = True
            except ToolError as exc:
                if exc.code != "NOT_FOUND": raise
                known = False
            if not known:
                try:
                    returned = self.rpc.send(signed.raw_transaction)
                    require(Web3.to_hex(returned) == hash_, "RPC hash mismatch", "BROADCAST_UNKNOWN")
                except ToolError:
                    journal.status(hash_, "unknown")
                    raise ToolError("BROADCAST_UNKNOWN", "Replay outcome unknown", data={"hash": hash_}) from None
                journal.status(hash_, "broadcast")
            out = {"hash": hash_, "status": "already_known" if known else "broadcast"}
            if params.get("wait"):
                receipt = wait_receipt(self.rpc, hash_, params.get("confirmations", self.chain["confirmations"]), params.get("timeout", 120))
                journal.status(hash_, "confirmed" if receipt["status"] == 1 else "failed")
                out.update(receipt=receipt, status="confirmed" if receipt["status"] == 1 else "failed")
                if receipt["status"] != 1:
                    raise ToolError("TRANSACTION_FAILED", "Transaction reverted on chain", data=out)
            return out

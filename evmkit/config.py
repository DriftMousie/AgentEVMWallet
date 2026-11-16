from __future__ import annotations

import csv
import os
import re
import stat
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from eth_account import Account

from .common import ToolError, address, require

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, path=None, env_file=None, state_dir=None, load_wallets=True):
        self.path = Path(path or ROOT / "config/settings.toml").resolve()
        try:
            self.raw = tomllib.loads(self.path.read_text())
        except (OSError, ValueError):
            raise ToolError("CONFIG_ERROR", "Cannot read TOML configuration") from None
        env_path = (Path(env_file) if env_file else ROOT / ".env").resolve()
        self.env_path = env_path
        self.warnings = []
        if not load_wallets:
            self.env = {}  # Transaction analysis never needs private keys.
        elif env_path.exists():
            if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
                self.warnings.append("Environment file is readable by other users; use chmod 600")
            self.env = {**dotenv_values(env_path, interpolate=False), **os.environ}
        else:
            self.env = dict(os.environ)
        self.chains = self.raw.get("chains", {})
        self.wallets = self.raw.get("wallets", {})
        self.state_dir = Path(state_dir or ROOT / ".state")
        self.duplicate_signing_wallets = {}
        if load_wallets:
            self._check_duplicate_signers()

    def _check_duplicate_signers(self):
        groups = {}
        for ident in self.wallets:
            try:
                wallet = self._load_wallet(ident)
            except ToolError:
                continue  # wallet.list reports the specific configuration error.
            if not wallet["watch_only"]:
                groups.setdefault(wallet["address"], []).append(ident)
        for public_address, identifiers in groups.items():
            if len(identifiers) < 2:
                continue
            for ident in identifiers:
                self.duplicate_signing_wallets[ident] = [other for other in identifiers if other != ident]
            self.warnings.append(
                f"DUPLICATE_SIGNING_ADDRESS: wallets {', '.join(identifiers)} resolve to the same signing address "
                f"{public_address}. Labels do not isolate funds; configure different private keys in {self.env_path} "
                "or the referenced process environment variables.")

    def rpc_endpoints(self, chain):
        """Load CSV lazily; relative CSV paths belong to the TOML directory."""
        self.chain(chain)
        value = self.raw.get("rpc", {}).get("csv_file", "rpc.csv")
        require(isinstance(value, str) and value.strip(), "Invalid RPC CSV path", "CONFIG_ERROR")
        path = Path(value)
        if not path.is_absolute():
            path = self.path.parent / path
        endpoints, names = [], set()
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle, strict=True)
                require(reader.fieldnames is not None and len(reader.fieldnames) == 5 and
                        set(reader.fieldnames) == {"chain", "name", "url", "priority", "enabled"},
                        "RPC CSV requires chain,name,url,priority,enabled columns", "CONFIG_ERROR")
                for index, row in enumerate(reader, 2):
                    message = f"Invalid RPC CSV row {index}"
                    require(None not in row and all(v is not None for v in row.values()), message, "CONFIG_ERROR")
                    row = {k: v.strip() for k, v in row.items()}
                    require(row["chain"] in self.chains and row["name"], message, "CONFIG_ERROR")
                    key = (row["chain"], row["name"])
                    require(key not in names, f"Duplicate RPC name in CSV row {index}", "CONFIG_ERROR")
                    names.add(key)
                    require(row["enabled"].lower() in ("true", "false", "1", "0"), message, "CONFIG_ERROR")
                    require(row["priority"].isascii() and row["priority"].isdigit(), message, "CONFIG_ERROR")
                    if row["enabled"].lower() in ("false", "0"):
                        continue
                    parsed = urlsplit(row["url"])
                    require(parsed.scheme in ("http", "https") and parsed.hostname and not any(c.isspace() for c in row["url"]),
                            f"Enabled RPC requires a valid HTTP(S) URL in CSV row {index}", "CONFIG_ERROR")
                    if row["chain"] == chain:
                        endpoints.append({"name": row["name"], "url": row["url"], "priority": int(row["priority"])})
        except ToolError:
            raise
        except (OSError, UnicodeError, csv.Error, ValueError):
            raise ToolError("CONFIG_ERROR", "Cannot read valid RPC CSV configuration") from None
        return sorted(endpoints, key=lambda item: item["priority"])

    def chain(self, name):
        require(name in self.chains, "Unknown chain alias", "CONFIG_ERROR")
        cfg = dict(self.chains[name], alias=name)
        require(isinstance(cfg.get("chain_id"), int) and cfg["chain_id"] > 0, "Invalid configured Chain ID", "CONFIG_ERROR")
        cfg.setdefault("decimals", 18)
        cfg.setdefault("confirmations", 1)
        cfg.setdefault("fee_mode", "auto")
        if cfg.get("wrapped_native_address"):
            cfg["wrapped_native_address"]=address(cfg["wrapped_native_address"],field=f"chains.{name}.wrapped_native_address")
        require(cfg["fee_mode"] in ("auto", "legacy", "eip1559"), "Invalid fee mode", "CONFIG_ERROR")
        require(0 <= cfg["decimals"] <= 255 and cfg["confirmations"] >= 1, "Invalid chain settings", "CONFIG_ERROR")
        return cfg

    def wallet(self, selector, signing=False):
        wallet = self._load_wallet(selector, signing)
        duplicates = self.duplicate_signing_wallets.get(wallet["id"])
        if duplicates:
            wallet["duplicate_signing_wallets"] = duplicates
        return wallet

    def _load_wallet(self, selector, signing=False):
        require(selector, "Wallet must be explicitly selected")
        if selector in self.wallets:
            ident = selector
        else:
            matches = [k for k, v in self.wallets.items() if v.get("label") == selector]
            require(len(matches) == 1, "Unknown or ambiguous wallet label", "WALLET_ERROR")
            ident = matches[0]
        item = self.wallets[ident]
        key_env = item.get("key_env")
        require(key_env is None or isinstance(key_env, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env),
                f"wallets.{ident}.key_env in {self.path} must name a private-key environment variable", "CONFIG_ERROR")
        key = self.env.get(key_env) if key_env else None
        configured = address(item["address"], field=f"wallets.{ident}.address") if item.get("address") else None
        details = {"wallet_id": ident, "env_file": str(self.env_path), "key_env": key_env,
                   "env_file_exists": self.env_path.is_file(), "config_file": str(self.path)}
        acct = None
        if key:
            try:
                acct = Account.from_key(key)
            except Exception:
                raise ToolError("WALLET_ERROR", f"Invalid private key for wallet '{ident}'; check {key_env} in {self.env_path} or the overriding process environment (expected 32-byte hex key)", data=details) from None
            if configured is not None and configured != acct.address:
                raise ToolError("WALLET_ERROR", f"wallets.{ident}.address in {self.path} differs from the address derived from {key_env}; verify both configurations", data=details)
        if acct is None and configured is None:
            if key_env:
                state = "file not found" if not self.env_path.exists() else "variable missing or empty"
                message = f"Wallet '{ident}' has no address or private key; set {key_env} in {self.env_path} ({state}), or supply that process environment variable"
            else:
                message = f"Wallet '{ident}' has neither address nor key_env; configure wallets.{ident}.address or wallets.{ident}.key_env in {self.path}"
            raise ToolError("WALLET_ERROR", message, data=details)
        if signing and acct is None:
            message = f"Wallet '{ident}' is watch-only and cannot sign; configure wallets.{ident}.key_env in {self.path} and its private-key variable in {self.env_path}"
            if key_env:
                message += f" (required variable: {key_env}; process environment takes precedence)"
            raise ToolError("WALLET_ERROR", message, data=details)
        return {"id": ident, "label": item.get("label", ident), "address": acct.address if acct else configured,
                "account": acct, "watch_only": acct is None}

    @staticmethod
    def public_wallet(wallet):
        return {k: v for k, v in wallet.items() if k != "account"}

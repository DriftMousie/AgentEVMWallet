"""Optional explorer adapters. No wallet secrets or arbitrary remote redirects."""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from .common import ToolError, address, require
from .network import json_request


class Sources:
    def __init__(self, config, chain, budget, enabled=True, max_pages=10):
        self.config, self.chain, self.budget = config, chain, budget
        self.enabled, self.max_pages = enabled, max_pages
        self.warnings, self.memo = [], {}
        settings = config.raw.get("explorers", {})
        path = Path(settings.get("config_file", "explorers.toml"))
        self.path = path if path.is_absolute() else config.path.parent / path
        self.raw = {}
        if enabled and self.path.exists():
            try:
                self.raw = tomllib.loads(self.path.read_text())
                require(isinstance(self.raw.get("chains", {}), dict) and isinstance(self.raw.get("fourbyte", {}), dict), "Invalid explorer configuration", "CONFIG_ERROR")
                cfg = self.raw.get("fourbyte", {})
                require(isinstance(cfg.get("enabled", False), bool) and isinstance(cfg.get("base_url", ""), str), "Invalid 4byte configuration", "CONFIG_ERROR")
            except (ValueError, OSError, ToolError):
                self.warn("EXPLORER_CONFIG_INVALID"); self.raw = {}
        self.provider = self.raw.get("chains", {}).get(chain["alias"], {})
        if not isinstance(self.provider, dict) or not isinstance(self.provider.get("etherscan", False), bool) or not isinstance(self.provider.get("blockscout_url", ""), str):
            self.warn("EXPLORER_CONFIG_INVALID"); self.provider = {}
        self.cache = config.state_dir / "analysis-cache.sqlite3"

    def warn(self, code):
        if code not in self.warnings: self.warnings.append(code)

    def keys(self):
        if "keys" in self.memo: return self.memo["keys"]
        value = self.config.raw.get("explorers", {}).get("secrets_file", "explorers.local.toml")
        path = Path(value)
        if not path.is_absolute(): path = self.config.path.parent / path
        if not path.exists(): return {}
        require(path.stat().st_mode & 0o077 == 0, "Explorer credentials file requires chmod 600", "CONFIG_ERROR")
        try: result = tomllib.loads(path.read_text())
        except (OSError, ValueError): raise ToolError("CONFIG_ERROR", "Cannot read explorer credentials TOML") from None
        self.memo["keys"] = result
        return result

    def cached_get(self, base, route, params=None, api_key=None, ttl=3600, context=None):
        self.budget.remaining()
        parsed = urlsplit(base)
        require(parsed.scheme in ("https", "http") and parsed.hostname and not parsed.query and not parsed.fragment and not parsed.username,
                "Explorer base URL must be HTTP(S), without credentials or query parameters", "CONFIG_ERROR")
        url = base.rstrip("/") + route
        key = hashlib.sha256(json.dumps([self.chain["chain_id"], url, params, context], sort_keys=True).encode()).hexdigest()
        if key in self.memo: return self.memo[key]
        connection = None
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.cache, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
            connection = sqlite3.connect(self.cache, timeout=2)
            connection.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, data TEXT)")
            row = connection.execute("SELECT data FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
            if row:
                value = json.loads(row[0]); self.memo[key] = value; return value
        except (OSError, sqlite3.Error, ValueError):
            self.warn("CACHE_UNAVAILABLE")
        finally:
            if connection: connection.close()
        query = dict(params or {})
        if api_key: query["apikey"] = api_key
        data = json_request("GET", url, params=query, timeout=min(10, self.budget.spend()))
        require(not api_key or api_key not in json.dumps(data), "Optional provider reflected credentials; response discarded", "SOURCE_UNAVAILABLE")
        if isinstance(data, dict) and (data.get("status") == "0" or data.get("error")):
            raise ToolError("SOURCE_UNAVAILABLE", "Optional provider returned an error; response not cached")
        self.memo[key] = data
        try:
            with closing(sqlite3.connect(self.cache, timeout=2)) as db:
                with db:
                    db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, time.time()+ttl, json.dumps(data)))
        except (OSError, sqlite3.Error, ValueError): self.warn("CACHE_UNAVAILABLE")
        return data

    def blockscout(self, route, params=None, context=None):
        base = self.provider.get("blockscout_url")
        require(self.enabled and base, "Blockscout not configured", "SOURCE_UNCONFIGURED")
        key = self.keys().get("chains", {}).get(self.chain["alias"], {}).get("blockscout_api_key")
        return self.cached_get(base, route, params, key, context=context)

    def etherscan(self, target, context=None):
        require(self.enabled and self.provider.get("etherscan", False), "Etherscan not configured", "SOURCE_UNCONFIGURED")
        key = self.keys().get("etherscan_api_key")
        require(isinstance(key, str) and key, "Etherscan API key missing from local explorer credentials", "SOURCE_UNCONFIGURED")
        data = self.cached_get("https://api.etherscan.io", "/v2/api",
                               {"chainid": str(self.chain["chain_id"]), "module": "contract", "action": "getsourcecode", "address": target}, key, context=context)
        require(data.get("status") == "1" and isinstance(data.get("result"), list) and data["result"], "Etherscan ABI/metadata unavailable", "SOURCE_UNAVAILABLE")
        return data["result"][0]

    def contract(self, target, code_hash=None):
        target = address(target)
        key = ("contract", target, code_hash)
        if key in self.memo: return self.memo[key]
        out = {"abi": None, "contract_name": None, "is_verified": None, "is_proxy": None,
               "implementations": [], "source": None, "metadata_scope": "current"}
        if not self.enabled: return out
        try:
            info = self.blockscout("/api/v2/addresses/" + target, context=code_hash)
            out.update(contract_name=info.get("name"), is_verified=info.get("is_verified") if isinstance(info.get("is_verified"), bool) else None)
            implementations = info.get("implementations") or []
            out["implementations"] = [address(i["address"]) for i in implementations if isinstance(i, dict) and i.get("address")]
            if out["implementations"]: out["is_proxy"] = True
            out["source"] = "blockscout"
        except ToolError as exc: self.warn("BLOCKSCOUT_METADATA_" + exc.code)
        except (ValueError, TypeError, AttributeError, KeyError): self.warn("BLOCKSCOUT_METADATA_INVALID_DATA")
        try:
            info = self.blockscout("/api/v2/smart-contracts/" + target, context=code_hash)
            if isinstance(info.get("abi"), list):
                out.update(abi=info["abi"], is_verified=True, source="blockscout")
            out["contract_name"] = out["contract_name"] or info.get("name")
        except ToolError as exc: self.warn("BLOCKSCOUT_ABI_" + exc.code)
        except (ValueError, TypeError, AttributeError, KeyError): self.warn("BLOCKSCOUT_ABI_INVALID_DATA")
        if out["abi"] is None:
            try:
                info = self.etherscan(target, context=code_hash)
                abi = info.get("ABI", "")
                if abi == "Contract source code not verified":
                    out["is_verified"] = False
                else:
                    parsed = json.loads(abi)
                    require(isinstance(parsed, list), "Invalid verified ABI", "SOURCE_UNAVAILABLE")
                    out.update(abi=parsed, is_verified=True, source="etherscan")
                out["contract_name"] = out["contract_name"] or info.get("ContractName") or None
                if info.get("Proxy") in ("0", "1"): out["is_proxy"] = info["Proxy"] == "1"
                if info.get("Implementation"): out["implementations"] = [address(info["Implementation"])]
            except (ToolError, ValueError, TypeError, AttributeError, KeyError): self.warn("ETHERSCAN_ABI_UNAVAILABLE")
        self.memo[key] = out
        return out

    def signatures(self, selector):
        cfg = self.raw.get("fourbyte", {})
        if not self.enabled or not cfg.get("enabled", False): return {"candidates": [], "complete": False, "status": "disabled"}
        base = cfg.get("base_url", "https://www.4byte.directory").rstrip("/")
        params, values, seen = {"hex_signature": selector}, set(), set()
        complete = False
        try:
            for _ in range(self.max_pages):
                key = json.dumps(params, sort_keys=True)
                require(key not in seen, "Repeated 4byte pagination cursor", "SOURCE_UNAVAILABLE"); seen.add(key)
                page = self.cached_get(base, "/api/v1/signatures/", params, ttl=86400)
                require(isinstance(page, dict) and isinstance(page.get("results"), list), "Invalid 4byte response", "SOURCE_UNAVAILABLE")
                for item in page.get("results", []):
                    require(isinstance(item, dict) and isinstance(item.get("text_signature"), str), "Invalid 4byte candidate", "SOURCE_UNAVAILABLE")
                    values.add(item["text_signature"])
                if not page.get("next"): complete = True; break
                next_url = urlsplit(page["next"])
                origin = urlsplit(base)
                require((next_url.scheme, next_url.netloc, next_url.path) == (origin.scheme, origin.netloc, origin.path+"/api/v1/signatures/"),
                        "Unexpected 4byte pagination URL", "SOURCE_UNAVAILABLE")
                params = dict(parse_qsl(next_url.query)); params["hex_signature"] = selector
        except ToolError as exc: self.warn("FOURBYTE_" + exc.code)
        return {"candidates": sorted(values), "complete": complete, "status": "complete" if complete else "partial"}

    def internals(self, hash_):
        items, cursor, seen = [], {}, set()
        for _ in range(self.max_pages):
            try:
                key = json.dumps(cursor, sort_keys=True)
                require(key not in seen, "Repeated internal transaction cursor", "SOURCE_UNAVAILABLE"); seen.add(key)
                page = self.blockscout("/api/v2/transactions/"+hash_+"/internal-transactions", cursor)
                require(isinstance(page, dict) and isinstance(page.get("items"), list), "Invalid explorer internal transactions response", "SOURCE_UNAVAILABLE")
                items.extend(page.get("items", []))
                cursor = page.get("next_page_params")
                require(cursor is None or isinstance(cursor, dict), "Invalid explorer pagination cursor", "SOURCE_UNAVAILABLE")
            except ToolError as exc:
                self.warn("BLOCKSCOUT_INTERNALS_"+exc.code)
                if not items: raise
                return {"items": items, "complete": False}
            if not cursor: return {"items": items, "complete": True}
        return {"items": items, "complete": False}

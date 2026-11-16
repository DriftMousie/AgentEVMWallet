"""Versioned read-only snapshots; credentials are never stored in cache keys."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from contextlib import closing
from .common import jsonable


class SnapshotCache:
    def __init__(self, config):
        self.path = config.state_dir / "analysis-cache.sqlite3"
        self.available = True

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, expires REAL, data TEXT)")
        return db

    def get(self, key):
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT data FROM snapshots WHERE key=? AND expires>? AND length(data)<=20971520", (key,time.time())).fetchone()
            return json.loads(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError):
            self.available=False;return None

    def put(self, key, value, ttl):
        try:
            payload=json.dumps(jsonable(value))
            if len(payload.encode())>20*1024*1024: return False
            with closing(self.connect()) as db:
                with db: db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)", (key,time.time()+ttl,payload))
            return True
        except (OSError, sqlite3.Error, ValueError):
            self.available=False;return False


def analysis_key(config, params):
    clean = {k:v for k,v in params.items() if k not in ("view","no_cache")}
    fingerprints = []
    paths = [config.path]
    for value in (config.raw.get("rpc",{}).get("csv_file","rpc.csv"),
                  config.raw.get("explorers",{}).get("config_file","explorers.toml")):
        path = Path(value)
        paths.append(path if path.is_absolute() else config.path.parent/path)
    if isinstance(params.get("abi"),str):
        paths.append(Path(params["abi"]))
    for path in paths:
        try: fingerprints.append(hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError: fingerprints.append(None)
    secret = Path(config.raw.get("explorers",{}).get("secrets_file","explorers.local.toml"))
    if not secret.is_absolute(): secret = config.path.parent/secret
    try: stat=secret.stat(); fingerprints.append((stat.st_mtime_ns,stat.st_size,stat.st_mode & 0o777))
    except OSError: fingerprints.append(None)
    return hashlib.sha256(json.dumps(["analysis-v2",clean,config.raw,fingerprints],sort_keys=True).encode()).hexdigest()

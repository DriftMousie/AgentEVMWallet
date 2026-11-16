"""CLI adapter. Business logic is importable through Toolkit.run()."""
from __future__ import annotations

import argparse
import json
import sys

from .common import ToolError, jsonable, result, require
from .config import Config
from .operations import OPS, Toolkit

JSON_FIELDS = {"args", "items", "tokens", "wallets", "addresses", "token_ids", "quantities", "topics", "output_types"}
BOOL_FIELDS = {"execute", "wait", "skip_simulation", "unlimited", "zero_first", "all", "approved", "continue_on_error", "include_unconfirmed"}
NUM_FIELDS = {"decimals", "confirmations", "timeout", "chunk_size"}


class ParseError(Exception): pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo user arguments: an accidental private-key argument must not leak.
        raise ParseError("Invalid CLI arguments; use --help")


def runtime(parser):
    for name in ("config", "env_file", "state_dir", "input_json"):
        parser.add_argument("--" + name.replace("_", "-"), default=argparse.SUPPRESS)


def main(module, argv=None):
    parser = Parser(description=f"EVM {module} tools. JSON stdout; writes preview unless --execute.")
    runtime(parser)
    subs = parser.add_subparsers(dest="operation")
    for name, fields in OPS[module].items():
        sub = subs.add_parser(name, help=f"{module}.{name}")
        runtime(sub)
        for field in sorted(fields):
            kwargs = {"default": argparse.SUPPRESS}
            if field in BOOL_FIELDS:
                kwargs["action"] = argparse.BooleanOptionalAction
            elif field in JSON_FIELDS:
                kwargs["type"] = json.loads
                kwargs["help"] = "JSON array"
            elif field in NUM_FIELDS:
                kwargs["type"] = int
            sub.add_argument("--" + field.replace("_", "-"), **kwargs)
    try:
        opts = vars(parser.parse_args(argv))
        operation = opts.pop("operation")
        config_options = {k: opts.pop(k) for k in ("config", "env_file", "state_dir") if k in opts}
        source = opts.pop("input_json", None)
        if source is not None:
            require(not opts, "JSON input cannot be mixed with business CLI arguments")
            try:
                if source == "-":
                    request = json.load(sys.stdin)
                else:
                    from .common import read_json
                    request = read_json(source)
            except ValueError:
                raise ToolError("INVALID_INPUT", "Invalid JSON input") from None
            require(isinstance(request, dict) and set(request) == {"operation", "params"}, "Input JSON must contain operation and params")
            require(operation is None or operation == request["operation"], "Operation differs from JSON input")
            operation, opts = request["operation"], request["params"]
        require(operation is not None, "Provide a subcommand or --input-json")
        config = Config(path=config_options.get("config"), env_file=config_options.get("env_file"), state_dir=config_options.get("state_dir"))
        response = Toolkit(config).run(module, operation, opts)
    except (ParseError, ToolError) as exc:
        response = result(module, error={"code": getattr(exc, "code", "INVALID_INPUT"),
                                        "message": str(exc), "retryable": False})
    except Exception:
        response = result(module, error={"code": "INVALID_INPUT", "message": "Unable to process request", "retryable": False})
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0 if response["ok"] else 1

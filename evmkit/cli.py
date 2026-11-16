"""CLI adapter. Business logic is importable through Toolkit.run()."""
from __future__ import annotations

import argparse
import json
import sys

from .common import ToolError, jsonable, result, require, wallet_selectors
from .config import Config
from .operations import OPS, Toolkit

JSON_FIELDS = {"args", "items", "tokens", "wallets", "addresses", "token_ids", "quantities", "topics", "output_types"}
BOOL_FIELDS = {"execute", "wait", "skip_simulation", "unlimited", "zero_first", "all", "approved", "continue_on_error", "include_unconfirmed", "external", "trace"}
NUM_FIELDS = {"decimals", "confirmations", "timeout", "chunk_size", "time_budget_seconds", "max_requests", "max_frames", "max_depth", "max_pages", "max_tokens", "max_addresses"}


class ParseError(Exception): pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo user arguments: an accidental private-key argument must not leak.
        raise ParseError("Invalid CLI arguments; use --help")


class WalletsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if hasattr(namespace, self.dest):
            raise ParseError("Specify --wallets (or its --wallet alias) only once")
        setattr(namespace, self.dest, wallet_selectors(values))


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
            if field == "wallets":
                sub.add_argument("--wallets", "--wallet", dest="wallets", action=WalletsAction,
                                 default=argparse.SUPPRESS, help='Wallet IDs/labels: trading,treasury or JSON ["trading","treasury"]')
                continue
            if field in BOOL_FIELDS:
                kwargs["action"] = argparse.BooleanOptionalAction
            elif field in JSON_FIELDS:
                kwargs["type"] = json.loads
                kwargs["help"] = "JSON array"
            elif field in NUM_FIELDS:
                kwargs["type"] = int
            elif field == "abi":
                kwargs["help"] = "ABI JSON file or preset: erc20, erc721, erc1155, weth, erc4626; transaction decode also auto-detects"
            elif field in {"gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei"}:
                kwargs["help"] = "Integer wei per gas, NOT gwei (1 gwei = 1000000000 wei)"
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
        analysis_only = module == "events" or module == "transaction" and operation in {"analyze", "decode", "export-template", "inspect", "status", "wait", "gas"}
        config = Config(path=config_options.get("config"), env_file=config_options.get("env_file"), state_dir=config_options.get("state_dir"), load_wallets=not analysis_only)
        response = Toolkit(config).run(module, operation, opts)
    except (ParseError, ToolError) as exc:
        response = result(module, error={"code": getattr(exc, "code", "INVALID_INPUT"),
                                        "message": str(exc), "retryable": False})
    except Exception:
        response = result(module, error={"code": "INVALID_INPUT", "message": "Unable to process request", "retryable": False})
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0 if response["ok"] else 1

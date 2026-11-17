"""CLI adapter. Business logic is importable through Toolkit.run()."""
from __future__ import annotations

import argparse
import json
import sys

from .common import ToolError, jsonable, result, require, wallet_selectors
from .config import Config
from .operations import OPS, Toolkit

JSON_FIELDS = {"args", "items", "tokens", "wallets", "addresses", "token_ids", "quantities", "topics", "output_types", "override", "field_map","functions"}
BOOL_FIELDS = {"execute", "wait", "skip_simulation", "unlimited", "zero_first", "all", "approved", "continue_on_error", "include_unconfirmed", "external", "trace", "require_trace", "explorer", "no_cache", "include_inputs", "verify","auto","include_source"}
NUM_FIELDS = {"decimals", "confirmations", "timeout", "chunk_size", "time_budget_seconds", "max_requests", "max_frames", "max_depth", "max_pages", "max_tokens", "max_addresses", "limit", "word", "take_candidate","max_probes","max_nodes","max_proxy_depth","max_source_files"}


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
        from .describe import SUPPORTED
        if (module,name) in SUPPORTED: sub.add_argument("--describe",action="store_true",default=argparse.SUPPRESS,help="Static machine-readable JSON help; no configuration or network needed")
        for field in sorted(fields):
            kwargs = {"default": argparse.SUPPRESS}
            if field == "wallets":
                sub.add_argument("--wallets", "--wallet", dest="wallets", action=WalletsAction,
                                 default=argparse.SUPPRESS, help='Wallet IDs/labels: trading,treasury or JSON ["trading","treasury"]')
                continue
            if field=="verify":
                kwargs["action"]="store_true";kwargs["help"]="Required: eth_call the patched payload from the selected sender; never broadcasts"
            elif field == "no_semantics":kwargs["action"]="store_true"
            elif field == "no_cache":
                kwargs["action"]="store_true"
                kwargs["help"]="Refresh persistent caches; within-request deduplication remains enabled"
            elif field in BOOL_FIELDS:
                kwargs["action"] = argparse.BooleanOptionalAction
            elif field in JSON_FIELDS:
                kwargs["type"] = json.loads
                kwargs["help"] = "JSON object" if field in ("override","field_map") else "JSON array"
            elif field=="type" and module=="raw" and name=="patch":kwargs["choices"]=["address","uint256","bytes32"]
            elif field == "view":
                kwargs["choices"]=["summary","full"]
            elif field == "direction":
                kwargs["choices"]=["in","out","both"]
            elif field in NUM_FIELDS:
                kwargs["type"] = int
            elif field == "abi":
                from .standards import PRESETS
                kwargs["help"] = "ABI JSON file or preset: "+", ".join(PRESETS)+"; transaction decode also auto-detects"
            elif field in {"gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei"}:
                kwargs["help"] = "Integer wei per gas, NOT gwei (1 gwei = 1000000000 wei)"
            if field == "require_trace": kwargs["help"] = "Require both callTracer and prestateTracer diff; optional source failures otherwise do not lower core status"
            if field == "explorer": kwargs["help"] = "Probe configured explorer APIs too (read-only, consumes provider quota)"
            sub.add_argument("--" + field.replace("_", "-"), **kwargs)
    try:
        opts = vars(parser.parse_args(argv))
        operation = opts.pop("operation")
        if opts.pop("describe",False):
            from .describe import describe
            response=result(module+"."+operation+".describe")
            response["data"]=describe(module,operation)
            print(json.dumps(response,ensure_ascii=False,separators=(",",":")))
            return 0
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
        if module=="contract" and operation=="selector":
            require(isinstance(opts,dict) and set(opts)=={"signature"},"selector requires only signature")
            from .abi import selector
            print(json.dumps(result('contract.selector',{'signature':opts['signature'],'selector':selector(opts['signature'])}),separators=(',',':')))
            return 0
        analysis_only = module in ("events","address") or module == "raw" and operation=="patch" and not opts.get("wallet") or module == "chain" and operation == "probe" or module == "transaction" and operation in {"analyze", "decode", "export-template", "inspect", "status", "wait", "gas"}
        analysis_only = analysis_only or module=="contract" and operation in ("functions","encode","introspect","diagnose")
        config = Config(path=config_options.get("config"), env_file=config_options.get("env_file"), state_dir=config_options.get("state_dir"), load_wallets=not analysis_only)
        response = Toolkit(config).run(module, operation, opts)
    except (ParseError, ToolError) as exc:
        response = result(module, error={"code": getattr(exc, "code", "INVALID_INPUT"),
                                        "message": str(exc), "retryable": False})
    except Exception:
        response = result(module, error={"code": "INVALID_INPUT", "message": "Unable to process request", "retryable": False})
    if response.get("error") and "suggested_next" not in response["error"]:
        from .agent_output import suggested_next
        response["error"]["suggested_next"]=suggested_next({},response.get("data"),response["error"]["code"])
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0 if response["ok"] else 1

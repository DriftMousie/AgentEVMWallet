"""Safe diagnostics for exceptions crossing the public CLI and SDK boundaries."""
import importlib.util
from pathlib import Path
import re
import traceback

from .common import ToolError

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = {"yaml": "PyYAML", "dotenv": "python-dotenv", "web3": "web3",
            "eth_abi": "eth-abi", "eth_account": "eth-account", "eth_keys": "eth-keys",
            "requests": "requests", "rlp": "rlp"}


def dependency_error(module):
    if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) or len(module) > 120:
        module = None
    package = PACKAGES.get((module or "").split(".")[0])
    label = f" '{module}'" if module else ""
    return ToolError(
        "DEPENDENCY_MISSING",
        f"Required module{label} is unavailable. Run python -m pip install -r requirements.txt with the project's interpreter.",
        data={"module": module, "package": package, "install_command": "python -m pip install -r requirements.txt"},
    )


def check_dependencies(module, operation):
    if module == "contract" and operation in {"introspect", "diagnose"}:
        try:
            available = importlib.util.find_spec("yaml") is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            raise dependency_error("yaml")


def exception_details(exc):
    """Only report project locations, with no source lines or exception text."""
    data = {"exception_type": type(exc).__name__}
    for frame, line in reversed(list(traceback.walk_tb(exc.__traceback__))):
        path = Path(frame.f_code.co_filename)
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            continue
        if relative.parts[0] != "evmkit":
            continue
        data["exception_at"] = {
            "file": relative.as_posix(), "function": frame.f_code.co_name, "line": line,
        }
        break
    return data


def diagnostic_error(exc, context=None, *, invalid_input=False):
    if isinstance(exc, ImportError):
        error = dependency_error(exc.name)
        error.data = {**(context or {}), **error.data}
        return error
    return ToolError(
        "INVALID_INPUT" if invalid_input else "INTERNAL_ERROR",
        "Missing or invalid operation parameters" if invalid_input else "Unexpected operation failure; inspect the error type and location",
        data={**(context or {}), **exception_details(exc)},
    )

# Preserve Python's standard token API because this filename shadows it.
import os as _os
_stdlib_token = _os.path.join(_os.path.dirname(_os.__file__), 'token.py')
with open(_stdlib_token, 'rb') as _handle:
    exec(compile(_handle.read(), _stdlib_token, 'exec'), globals())

if __name__ == "__main__":
    from evmkit.cli import main
    raise SystemExit(main("token"))
else:
    def run(params, operation, config=None):
        """Return the same JSON-compatible envelope as CLI."""
        from evmkit.config import Config
        from evmkit.operations import Toolkit
        return Toolkit(config or Config()).run("token", operation, params)

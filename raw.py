if __name__ == "__main__":
    from evmkit.cli import main
    raise SystemExit(main("raw"))
else:
    def run(params, operation, config=None):
        """Return the same JSON-compatible envelope as CLI."""
        from evmkit.config import Config
        from evmkit.operations import Toolkit
        return Toolkit(config or Config(load_wallets=not(operation=="patch" and not params.get("wallet")))).run("raw", operation, params)

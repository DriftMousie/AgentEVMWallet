if __name__ == "__main__":
    from evmkit.cli import main
    raise SystemExit(main("transaction"))
else:
    def run(params, operation, config=None):
        """Return the same JSON-compatible envelope as CLI."""
        from evmkit.config import Config
        from evmkit.operations import Toolkit
        readonly={"analyze","decode","inspect","status","wait","gas","export-template"}
        return Toolkit(config or Config(load_wallets=operation not in readonly)).run("transaction", operation, params)

if __name__ == "__main__":
    from evmkit.cli import main
    raise SystemExit(main("address"))
else:
    def run(params, operation, config=None):
        from evmkit.config import Config
        from evmkit.operations import Toolkit
        return Toolkit(config or Config(load_wallets=False)).run("address",operation,params)

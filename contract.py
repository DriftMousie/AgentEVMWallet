if __name__ == "__main__":
    from evmkit.cli import main
    raise SystemExit(main("contract"))
else:
    def run(params, operation, config=None):
        """Return the same JSON-compatible envelope as CLI."""
        from evmkit.config import Config
        from evmkit.operations import Toolkit
        if operation=="selector" and config is None:
            from evmkit.abi import selector
            from evmkit.common import ToolError,result
            try:
                from evmkit.common import require
                require(isinstance(params,dict) and set(params)=={'signature'},'selector requires only signature')
                return result('contract.selector',{'signature':params['signature'],'selector':selector(params['signature'])})
            except ToolError as exc:return result('contract.selector',error={'code':exc.code,'message':exc.message,'retryable':False})
        return Toolkit(config or Config(load_wallets=operation not in {'functions','encode','introspect','diagnose','math','storage'})).run("contract", operation, params)

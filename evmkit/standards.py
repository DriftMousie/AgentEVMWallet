"""Named standard interfaces. A matching selector is evidence, not proof of identity."""
from copy import deepcopy
from .abi import canonical


def function(name, inputs=(), outputs=(), state="view"):
    return {"type": "function", "name": name, "stateMutability": state,
            "inputs": [{"name": n, "type": t} for n, t in inputs],
            "outputs": [{"name": n, "type": t} for n, t in outputs]}


def event(name, fields):
    return {"type": "event", "name": name, "anonymous": False,
            "inputs": [{"name": n, "type": t, "indexed": indexed} for n, t, indexed in fields]}


F, E = function, event
METADATA = [F("name", outputs=[("name", "string")]), F("symbol", outputs=[("symbol", "string")]),
            F("decimals", outputs=[("decimals", "uint8")])]
ERC20 = METADATA + [
    F("totalSupply", outputs=[("supply", "uint256")]),
    F("balanceOf", [("owner", "address")], [("balance", "uint256")]),
    F("allowance", [("owner", "address"), ("spender", "address")], [("remaining", "uint256")]),
    F("transfer", [("to", "address"), ("value", "uint256")], [("success", "bool")], "nonpayable"),
    F("approve", [("spender", "address"), ("value", "uint256")], [("success", "bool")], "nonpayable"),
    F("transferFrom", [("from", "address"), ("to", "address"), ("value", "uint256")], [("success", "bool")], "nonpayable"),
    E("Transfer", [("from", "address", True), ("to", "address", True), ("value", "uint256", False)]),
    E("Approval", [("owner", "address", True), ("spender", "address", True), ("value", "uint256", False)]),
]
OPERATORS = [F("setApprovalForAll", [("operator", "address"), ("approved", "bool")], state="nonpayable"),
             F("isApprovedForAll", [("owner", "address"), ("operator", "address")], [("approved", "bool")]),
             E("ApprovalForAll", [("owner", "address", True), ("operator", "address", True), ("approved", "bool", False)])]
ERC165 = F("supportsInterface", [("interfaceId", "bytes4")], [("supported", "bool")])
ERC721 = [ERC165] + OPERATORS + [
    F("name", outputs=[("name", "string")]), F("symbol", outputs=[("symbol", "string")]),
    F("balanceOf", [("owner", "address")], [("balance", "uint256")]),
    F("ownerOf", [("tokenId", "uint256")], [("owner", "address")]),
    F("tokenURI", [("tokenId", "uint256")], [("uri", "string")]),
    F("getApproved", [("tokenId", "uint256")], [("approved", "address")]),
    F("approve", [("spender", "address"), ("tokenId", "uint256")], state="nonpayable"),
    F("transferFrom", [("from", "address"), ("to", "address"), ("tokenId", "uint256")], state="nonpayable"),
    F("safeTransferFrom", [("from", "address"), ("to", "address"), ("tokenId", "uint256")], state="nonpayable"),
    F("safeTransferFrom", [("from", "address"), ("to", "address"), ("tokenId", "uint256"), ("data", "bytes")], state="nonpayable"),
    E("Transfer", [("from", "address", True), ("to", "address", True), ("tokenId", "uint256", True)]),
    E("Approval", [("owner", "address", True), ("spender", "address", True), ("tokenId", "uint256", True)]),
]
ERC1155 = [ERC165] + OPERATORS + [
    F("balanceOf", [("owner", "address"), ("id", "uint256")], [("balance", "uint256")]),
    F("balanceOfBatch", [("owners", "address[]"), ("ids", "uint256[]")], [("balances", "uint256[]")]),
    F("uri", [("id", "uint256")], [("uri", "string")]),
    F("safeTransferFrom", [("from", "address"), ("to", "address"), ("id", "uint256"), ("value", "uint256"), ("data", "bytes")], state="nonpayable"),
    F("safeBatchTransferFrom", [("from", "address"), ("to", "address"), ("ids", "uint256[]"), ("values", "uint256[]"), ("data", "bytes")], state="nonpayable"),
    E("TransferSingle", [("operator", "address", True), ("from", "address", True), ("to", "address", True), ("id", "uint256", False), ("value", "uint256", False)]),
    E("TransferBatch", [("operator", "address", True), ("from", "address", True), ("to", "address", True), ("ids", "uint256[]", False), ("values", "uint256[]", False)]),
]
WETH = ERC20 + [F("deposit", state="payable"), F("withdraw", [("wad", "uint256")], state="nonpayable"),
                 E("Deposit", [("dst", "address", True), ("wad", "uint256", False)]),
                 E("Withdrawal", [("src", "address", True), ("wad", "uint256", False)])]
ERC4626 = ERC20 + [
    F("asset", outputs=[("asset", "address")]), F("totalAssets", outputs=[("assets", "uint256")]),
    F("convertToShares", [("assets", "uint256")], [("shares", "uint256")]),
    F("convertToAssets", [("shares", "uint256")], [("assets", "uint256")]),
    F("deposit", [("assets", "uint256"), ("receiver", "address")], [("shares", "uint256")], "nonpayable"),
    F("mint", [("shares", "uint256"), ("receiver", "address")], [("assets", "uint256")], "nonpayable"),
    F("withdraw", [("assets", "uint256"), ("receiver", "address"), ("owner", "address")], [("shares", "uint256")], "nonpayable"),
    F("redeem", [("shares", "uint256"), ("receiver", "address"), ("owner", "address")], [("assets", "uint256")], "nonpayable"),
    E("Deposit", [("sender", "address", True), ("owner", "address", True), ("assets", "uint256", False), ("shares", "uint256", False)]),
    E("Withdraw", [("sender", "address", True), ("receiver", "address", True), ("owner", "address", True), ("assets", "uint256", False), ("shares", "uint256", False)]),
]
ROUTE = [("path", "address[]"), ("to", "address"), ("deadline", "uint256")]
V2_ROUTER = [
    F(name, amounts + ROUTE, [] if "SupportingFeeOnTransferTokens" in name else [("amounts", "uint256[]")],
      "payable" if name.startswith(("swapExactETH", "swapETH")) else "nonpayable")
    for name, amounts in [
        ("swapExactTokensForTokens", [("amountIn", "uint256"), ("amountOutMin", "uint256")]),
        ("swapTokensForExactTokens", [("amountOut", "uint256"), ("amountInMax", "uint256")]),
        ("swapExactETHForTokens", [("amountOutMin", "uint256")]),
        ("swapTokensForExactETH", [("amountOut", "uint256"), ("amountInMax", "uint256")]),
        ("swapExactTokensForETH", [("amountIn", "uint256"), ("amountOutMin", "uint256")]),
        ("swapETHForExactTokens", [("amountOut", "uint256")]),
        ("swapExactTokensForTokensSupportingFeeOnTransferTokens", [("amountIn", "uint256"), ("amountOutMin", "uint256")]),
        ("swapExactETHForTokensSupportingFeeOnTransferTokens", [("amountOutMin", "uint256")]),
        ("swapExactTokensForETHSupportingFeeOnTransferTokens", [("amountIn", "uint256"), ("amountOutMin", "uint256")]),
    ]
]
POOL_TOKENS = [F("token0", outputs=[("token", "address")]), F("token1", outputs=[("token", "address")])]
V2_PAIR = POOL_TOKENS + [
    E("Swap", [("sender", "address", True), ("amount0In", "uint256", False), ("amount1In", "uint256", False),
               ("amount0Out", "uint256", False), ("amount1Out", "uint256", False), ("to", "address", True)]),
    E("Mint", [("sender", "address", True), ("amount0", "uint256", False), ("amount1", "uint256", False)]),
    E("Burn", [("sender", "address", True), ("amount0", "uint256", False), ("amount1", "uint256", False), ("to", "address", True)]),
    E("Sync", [("reserve0", "uint112", False), ("reserve1", "uint112", False)]),
]
V3_SWAP = [("sender", "address", True), ("recipient", "address", True), ("amount0", "int256", False),
           ("amount1", "int256", False), ("sqrtPriceX96", "uint160", False), ("liquidity", "uint128", False), ("tick", "int24", False)]
V3_COMMON = POOL_TOKENS + [
    E("Initialize", [("sqrtPriceX96", "uint160", False), ("tick", "int24", False)]),
    E("Mint", [("sender", "address", False), ("owner", "address", True), ("tickLower", "int24", True), ("tickUpper", "int24", True),
               ("amount", "uint128", False), ("amount0", "uint256", False), ("amount1", "uint256", False)]),
    E("Burn", [("owner", "address", True), ("tickLower", "int24", True), ("tickUpper", "int24", True),
               ("amount", "uint128", False), ("amount0", "uint256", False), ("amount1", "uint256", False)]),
    E("Collect", [("owner", "address", True), ("recipient", "address", False), ("tickLower", "int24", True), ("tickUpper", "int24", True),
                  ("amount0", "uint128", False), ("amount1", "uint128", False)]),
    E("Flash", [("sender", "address", True), ("recipient", "address", True), ("amount0", "uint256", False), ("amount1", "uint256", False),
                ("paid0", "uint256", False), ("paid1", "uint256", False)]),
]
UNISWAP_V3_POOL = V3_COMMON + [E("Swap", V3_SWAP)]
PANCAKE_V3_POOL = V3_COMMON + [E("Swap", V3_SWAP + [("protocolFeesToken0", "uint128", False), ("protocolFeesToken1", "uint128", False)])]
PRESETS = {"erc20": ERC20, "erc721": ERC721, "erc1155": ERC1155, "weth": WETH, "erc4626": ERC4626,
           "uniswap_v2_router": V2_ROUTER, "dex_v2_pair": V2_PAIR,
           "uniswap_v3_pool": UNISWAP_V3_POOL, "pancake_v3_pool": PANCAKE_V3_POOL}


def preset(name):
    return deepcopy(PRESETS[name])


def entries(kind):
    """Deduplicate identical layouts but retain distinct standards/semantic names."""
    import json
    groups = {}
    for standard, abi in PRESETS.items():
        for item in abi:
            if item["type"] != kind:
                continue
            key = json.dumps(item, sort_keys=True)
            if key not in groups:
                groups[key] = {"abi": item, "standards": []}
            groups[key]["standards"].append(standard)
    return list(groups.values())

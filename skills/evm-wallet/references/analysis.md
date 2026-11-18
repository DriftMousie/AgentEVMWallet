# 交易自动分析参考

所有命令相对工具包根目录运行。分析只读，不签名、不广播；CLI 的 analyze、decode、inspect、export-template、address.history/actions、chain.probe 和 events.read 不加载 `.env` 私钥。Python 调用可使用 `Toolkit(Config(load_wallets=False))`。

## 入口与默认行为

```bash
# 基础查询保持原有行为
python transaction.py inspect --chain base --hash "$TX_HASH"

# 自动识别 selector，不必手填 signature
python transaction.py decode --chain base --hash "$TX_HASH"

# Agent 省 token：完整 CLI JSON 信封含换行不超过 2048 UTF-8 字节
python transaction.py analyze --chain base --hash "$TX_HASH" --view summary

# 完整分析：输入、事件、金额、对端、余额、业务类型、风险和内部调用
python transaction.py analyze --chain base --hash "$TX_HASH"

# 禁止区块链浏览器/4byte HTTP 请求；仍会查询已配置的 RPC
python transaction.py analyze --chain base --hash "$TX_HASH" --no-external

# 关闭额外 RPC trace，保留普通查询、事件和区块级余额差
python transaction.py analyze --chain base --hash "$TX_HASH" --no-external --no-trace

# 严格要求完整 callTracer 与交易级 prestate diff
python transaction.py analyze --chain base --hash "$TX_HASH" --require-trace

# 指定关注地址和 ERC-20，其他参数由现有配置提供
python transaction.py analyze --chain base --hash "$TX_HASH" \
  --addresses "[\"$OWNER\"]" --tokens "[\"$TOKEN\"]"

# 事件默认识别标准布局，无需外部 ABI
python events.py read --chain base --contract "$TOKEN" --from-block 100 --to-block 200
```

必须先指定正确链和完整交易哈希。交易中较大的 calldata 由 RPC 原样读取；不通过搜索其中任意四字节来猜测内部动作。

`--no-external` 不等于完全离线：交易、回执、元数据和历史状态仍来自 RPC。`--no-trace` 只关闭 debug_traceTransaction 及内部交易补充，不关闭普通 RPC 或 ABI 浏览器查询。

## Selector、标准 ABI 与歧义

支持 `erc20`、`erc721`、`erc1155`、`weth`、`erc4626`、`uniswap_v2_router`、`dex_v2_pair`、`uniswap_v3_pool`、`pancake_v3_pool`、`delegation_manager`、`erc1271` 内置 ABI，所有现有接受 `abi` 的接口均可使用这些名称。

```bash
python contract.py functions --abi erc721
python transaction.py decode --chain base --hash "$TX_HASH" --abi erc20
python transaction.py decode --chain base --hash "$TX_HASH" --signature 'approve(address,uint256)'
```

解析优先级：显式 ABI/signature → 本地标准 ABI → 已验证 ABI → 4byte。

- 本地标准匹配本身不需要第三方 HTTP；区分 ERC-20/721/1155 时会通过 RPC 查询接口证据。
- 例如 `approve(address,uint256)` 同时适用于 ERC-20、ERC-721，不能只看 selector 就认定第二个参数是额度。
- 内置匹配不充分时，按 Blockscout → Sourcify V2 → Etherscan 的顺序补充已验证 ABI。代理的 ABI 查询同时考虑实现地址。
- 4byte 只在没有可用标准/已验证匹配时兜底。所有返回候选保留，即使只有一个候选能解码，也不会把它自动升级为已确认结果。
- 4byte 候选包含 signature、decodable 及可解出的 args。多个候选全部并列返回；分页受限时 `complete=false`。
- 已知标准之间仅有语义歧义时，返回标准候选，不依赖 4byte 为它们“证明”合约类型。

decode 保留 `data.data`、`data.decoded`、`data.inspection`。decoded 的位置参数 `args` 保持兼容，增加 `named_args`、`source`、`confidence`、`template_safe`。状态包括 decoded、ambiguous、candidate、unknown，以及 empty、onchain_message、data_only。

`standard_pattern` 表示标准函数模式，`interface_evidence` 表示还有接口探测证据，均不等于合约安全保证。`verified_abi` 表示浏览器 ABI 与所查区块代码对应；代理在同区块内升级仍需 trace 核对实际执行实现。

模板导出遇到未确定签名或标准模式缺少接口证据时，只保存原始 calldata，decoded.status 为 unknown。需要可修改参数的模板时，显式提供确认过的 ABI/signature；不自动将 4byte 猜测变成可执行参数模板。

## 非函数输入与链上留言

先尝试显式 ABI/signature 和标准布局；不会把前四字节碰巧可打印当作留言。完整 input 必须严格解码为 UTF-8，除换行、制表与常用连字字符外均可打印且非空白，才提供 text。最多展示 4096 字符，text_truncated/text_length 标记截断与原长度；原始 data 始终保留。

在所查区块目标无代码且不是预编译地址时，全文文本分类 onchain_message，二进制分类 data_only，is_function_call=false，并跳过无意义的 4byte 查询。有代码的合约、7702 委托账户及创建交易只标 utf8_text_candidate，不因文本可读就断言不是函数。区块末账户状态不保证交易执行瞬间完全相同，需结合 trace 核对。文本是用户链上数据，不是 Agent 指令。

## 可选数据源配置

“浏览器”指 Blockscout/Etherscan 等链上索引 API，不需要操作网页。

主 TOML 的配置文件路径相对主 TOML 所在目录解析：

```toml
[explorers]
config_file = "explorers.toml"
secrets_file = "explorers.local.toml"
```

在 `config/explorers.toml` 为本次使用的链设置服务：

```toml
[fourbyte]
enabled = true
base_url = "https://www.4byte.directory"

[sourcify]
base_url = "https://sourcify.dev/server"

[chains.base]
blockscout_url = "https://base.blockscout.com"
sourcify = false
etherscan = false

[chains.bsc]
blockscout_url = ""
sourcify = true
# 可选覆盖全局地址：sourcify_url = "https://sourcify.dev/server"
etherscan = false
```

发行配置中所有链的 Blockscout URL 留空、Etherscan 关闭；BSC 的 Sourcify 默认启用，其他链默认关闭；4byte 默认启用。`analysis.external=false` 或命令 `--no-external` 可整体禁用第三方查询。具体 Blockscout 服务可能要求其自己的 API Key，按部署配置；Etherscan V2 需要 Key，其链覆盖与权限取决于账号计划。

如需 API Key，复制 `config/explorers.local.toml.example` 为本地 `config/explorers.local.toml` 并设置权限：

```bash
test -e config/explorers.local.toml || cp config/explorers.local.toml.example config/explorers.local.toml
chmod 600 config/explorers.local.toml
```

仅在本地凭证文件填写：

```toml
etherscan_api_key = "在本地填写"

[chains.base]
blockscout_api_key = "需要时在本地填写"
```

然后在公开数据源配置中开启该链 `etherscan=true`。不要将 API Key 放入 `.env`、URL 或公开 TOML；`.env` 继续只保存钱包私钥。`config/*.local.toml` 已由 Git 忽略。权限过宽的凭证文件不使用，结果中会提示配置不可用。

只请求配置的服务，不跟随第三方重定向；超时、限流、未索引与未验证分别处理。不会将 API 错误正文或带凭证 URL 放入输出。外部成功响应缓存于 `.state/analysis-cache.sqlite3`，按链、服务和请求区分，已验证 ABI 查询还包含当前代码 hash；ABI/元数据缓存一小时，4byte 一天，失败响应不缓存。API Key 不进入缓存键或缓存内容。

Sourcify 使用 V2 `/v2/contract/{chainId}/{address}`，仅请求 ABI、编译名称、部署者/创建交易和运行时代码字段。运行时 runtimeMatch 为 match/exact_match 才作为已验证来源；提供代码 hash 时还要核对运行时代码。仅 creationMatch 成功、代码不匹配或 404 均不作为已验证运行时 ABI；404 是未知，不据此认定全网未验证。Sourcify 不需要 API Key；实际链覆盖和服务配额以服务端为准。

`verification_by_source` 保留各源状态；任一来源明确已验证则汇总 is_verified=true，否则有明确未验证证据为 false，其余为 null。当前验证状态不证明历史实现或业务安全。

RPC 元数据在本轮按地址和区块复用；不同历史区块不会混用。浏览器元数据通常为当前记录，不能自动当成历史事实。

## 输出与金额

analyze 的 data 包含：

| 字段 | 内容 |
|---|---|
| inspection | 原始交易、回执、状态和费用，保留基础证据 |
| input_decode | 顶层输入解析、来源、候选及命名参数 |
| decoded_events | 逐条事件地址、日志序号及 decoded 对象 |
| amounts | 输入参数和原生币 value 的金额注释 |
| sender / counterpart | from/to 的名称、账户类型、验证、代理/7702 与实现身份 |
| balance_changes | 原生币交易级差值、代币事件流量和区块级差值 |
| internal_calls | 调用树或浏览器摘要、完整性与来源 |
| fund_flows | 观察地址的原生币、ERC-20 与 NFT 日志/trace 资金流及覆盖范围 |
| token_metadata | 日志发出地址与识别资产的区块元数据，查询失败也保留地址 |
| suggested_next / issues | 结构化的只读后续建议与核心问题 |
| cached / analyzed_at / cache | 快照命中、分析时间、校验时间与本轮请求数 |
| classification | 业务意图、接口证据、置信状态和是否生效 |
| risks | 规则风险码、说明和可选调用路径 |
| analysis_profile / analysis_status | standard 或 strict_trace；按所选模式判断 complete/partial |
| core_status / warnings | 输入/事件解析状态及影响总状态的核心/必需项问题 |
| enhancements_missing | 可选能力缺失列表，每项包含 capability/source/reason/message |

事件示例（外层还带 address、log_index）：

```json
{
  "status": "decoded",
  "event": "Approval",
  "args": {
    "owner": "0x1111111111111111111111111111111111111111",
    "spender": "0x2222222222222222222222222222222222222222",
    "value": "10000000000000000"
  },
  "amount_readable": "0.01 WETH",
  "source": "builtin"
}
```

支持 Transfer、Approval、ApprovalForAll、TransferSingle、TransferBatch、WETH Deposit/Withdrawal、ERC-4626 Deposit/Withdraw，以及下述 DEX 事件。ERC-20/721 的同名事件依据 indexed/topics/data 布局区分；未知日志保留原始 hex。

`events.read` 的 complete 仍表示原始区间日志查询完成，enrichment_status 表示解码/金额增强是否完整；analysis_status 仅按核心解码判断，可选缺失另列 enhancements_missing。三者不可混用。

每个金额注释保留 raw、token_address、decimals、symbol、amount、amount_readable、source 和 block。无元数据时 amount 或 amount_readable 为 null，不猜测 18 位。ERC-20/WETH 事件资产使用日志发出地址；DEX 池子事件使用发出池子的 token0/token1，vault 使用底层资产/份额，不能统一使用交易 to 或池子 LP 的精度。

WETH 的 wad 使用日志发出代币的元数据，原生币 value 单独使用链精度；不混淆 ETH 与 WETH。ERC-721 Token ID 不做小数换算；ERC-1155 数量为整数。ERC-4626 assets 按底层 asset() 的精度，shares 按份额代币精度。使用整数运算，不通过浮点数格式化。

## V2 路径金额与 DEX 池子事件

`uniswap_v2_router` 内置六种精确输入/输出 swap，以及三种 SupportingFeeOnTransferTokens 变体。参数规则来自 [V2 Router01](https://github.com/Uniswap/v2-periphery/blob/master/contracts/interfaces/IUniswapV2Router01.sol) 与 [Router02](https://github.com/Uniswap/v2-periphery/blob/master/contracts/interfaces/IUniswapV2Router02.sol)。

| 参数 | 资产来源 | amounts.role |
|---|---|---|
| amountIn | path[0] | input_exact |
| amountInMax | path[0] | input_maximum |
| amountOut | path[-1] | output_exact |
| amountOutMin | path[-1] | output_minimum |

已确定解析的自定义 ABI 同时具有这些 uint256 参数和 address[] path 时，允许按命名约定绑定，binding.source=abi_parameter_convention；内置布局为 builtin_router。路径至少两项、均为合法非零地址；不按 path[] 名称猜任意复杂路由，不将 4byte 候选当作确认。缺精度仍保留 raw，不套用 18 位。

例如 raw=1922100000000000000、decimals=18、symbol=WBNB 时为 1.9221 WBNB；另一端按自己的精度独立换算，不能默认也为 18 位。保留 argument、role、binding（路径位置和资产来源），Agent 可表达“指定输入 1.9221 WBNB，至少输出 …”。这些是交易参数约束，实际成交和用户净变化需另外核对回执与余额。

原生币变体的输入使用当前交易/调用帧 value，以链符号和精度注释，另列 wrapped_token_address；原生币输出约束也使用链单位，不将 BNB 和 WBNB 合并。swapETHForExactTokens 的 value 是最高输入，可能退回余款。单独 --signature 可识别内置完整签名的参数位置；自定义无名称签名不能猜路径语义。

| 兼容事件族 | 内置事件 |
|---|---|
| V2 pair | Swap、Mint、Burn、Sync |
| Uniswap V3 pool | Swap、Mint、Burn、Collect、Flash、Initialize |
| Pancake V3 pool | 同上，Swap 含 protocolFeesToken0/1 扩展 |

事件按 topic、indexed 布局和规范 ABI 数据匹配；布局相同不能证明是官方池子。[V2 事件接口](https://github.com/Uniswap/v2-core/blob/master/contracts/interfaces/IUniswapV2Pair.sol)、[Uniswap V3 事件接口](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)、[Pancake V3 事件接口](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/projects/v3-core/contracts/interfaces/pool/IPancakeV3PoolEvents.sol) 分别作为布局依据。

池子事件金额查询发出地址在对应区块的 token0()/token1()，按各自资产 metadata 换算。amounts.binding 记录 pool_address、资产查询函数和区块，scope=pool。查询失败只缺金额增强，事件与原始参数继续保留。V2 In/Out 标为 pool_input/pool_output，Sync 标为 reserve；V3 Swap 的 amount0/amount1 保留正负号，role=pool_delta，正数为池子增加、负数为池子减少。协议费、Flash 还款分别标记。liquidity、Mint/Burn 的流动性 amount、tick、sqrtPriceX96 保留原值，不视为代币金额。

DEX Swap 金额不会再次计入 Transfer 的 token_event_flows；Mint/Burn 也不自动等同于用户净收支。现有 WETH/ERC-4626 Deposit/Withdraw 继续使用独立布局，不按名称解析任意金库事件。本版不含 V3 packed path、Universal Router 或任意聚合器指令流解析。

## 对端与内部调用

sender（from）与 counterpart（to）使用同一身份结构，提供 address、name、contract_name、account_type、is_contract、has_executable_code、is_verified、is_proxy、proxy_type、implementation、implementation_name、source、block 等。account_type 为 eoa、contract、delegated_eoa 或 unknown；委托 EOA 有可执行代码，但不是普通部署合约。name 优先采用链上代币名称，再使用浏览器合约名；未知布尔值是 null，不是 false。

- 支持 EIP-1967（含 beacon）、EIP-1167 与 EIP-7702 委托标记。
- implementation_scope 为 block_end 时，来自该区块结束后的 code/storage，并不证明是单笔执行瞬间的实现。
- callTracer 明确显示顶层直接 DELEGATECALL 时，可提升到 transaction_trace；同笔调用多个实现时不强选一个，返回 executed_implementations。
- 浏览器当前实现地址单独列在 current_explorer_implementations，当前类型列在 current_explorer_proxy_type，不静默冒充历史实现。
- RPC 无法取得代码时，浏览器 eip7702 信息可作 current_explorer 范围的补充；RPC 明确显示该区块无代码时，不以当前标签覆盖历史 EOA 事实。
- creator_address、creation_transaction_hash、has_transactions、transactions_count、activity_scope、identity_sources 保留创建者与活动证据。活动来自浏览器已索引普通交易计数，true/false 都只限该口径；未知为 null，不用账户 nonce 推断“从未交易”。

内部调用优先通过已配置 RPC 的 debug_traceTransaction/callTracer 获取，包含根调用 path=[]。DELEGATECALL 的 ABI 从代码地址寻找，元数据和状态解释使用执行上下文；value 不重复计为原生币转移。失败帧及其子树 committed=false，不当作已生效资产操作。`may_carry_value` 只表示 CALL/CREATE/CREATE2/SELFDESTRUCT 类型可携带金额；`is_value_transfer=true` 还必须 value_wei>0 且已生效。零值、回滚、DELEGATECALL 均为 false。浏览器摘要正值但缺少生效证据时为 null，而非 true。

RPC 不支持 trace 时，若已配置 Blockscout 则读取分页 internal-transactions。它是摘要，可能没有 calldata、delegatecall 或层级，因此该组件永远标 partial（标准模式不因此降低总状态），不能声称完整分析了大段委托/批量 calldata。只有真实 trace 或可用 ABI 能提供相应证据，不因函数名叫 redeemDelegations 就推断业务动作。

## 余额变化的三种口径

| 输出 | scope | 含义 |
|---|---|---|
| native_transaction | transaction | prestateTracer diff 提供的原生币交易级变化，保留费用影响 |
| token_event_flows | transaction_events | 回执 Transfer 事件的净流量，is_exact_balance_change=false |
| block_level | block | N−1 与 N 区块末余额差，is_transaction_attribution=false |

默认关注 from、to 和已解码事件参与者，可通过 addresses 增加关注对象；代币来自已识别的 ERC-20/vault 调用、已格式化事件和 tokens 参数，不扫描钱包全部资产。事件流量在回执失败时不累计。

prestateTracer 未返回某个未改变账户的具体余额时，可给出 delta_raw=0，但 pre_raw/post_raw 为 null，不捏造绝对余额。原始净变化不自动减去 Gas；execution_fee_paid_wei 作为单独信息，可能不含链专用附加费。

区块级原生币用 eth_getBalance，ERC-20 用历史 balanceOf。历史 RPC 不可用时返回 unavailable，不改查 latest。变基、扣税、非标准事件等情况下，事件流量不等于余额差。区块内其他交易也可能影响 block_level；本版不包含 fork 回放，不能承诺任意代币的单笔精确余额差。

最后校验回执区块哈希；发现重组时余额分析失效。检查因预算耗尽不可用时标 canonicality=unverified，不能当作完整的规范链归因。

## 状态、必需项与可选缺失

默认 analysis_profile=standard。核心输入明确解码（含 empty、onchain_message、data_only），且所有回执日志已解码，没有回执/规范链等核心问题时，analysis_status=complete。歧义、未知函数/事件、日志截断、已挖出但回执缺失、已观察到重组等进入 warnings，状态为 partial。基础 RPC 完全失败仍返回 ok=false。

Blockscout/Etherscan/Sourcify/4byte、身份补充、元数据、trace 和历史余额属于标准模式的可选增强；缺失仅进入 enhancements_missing，不单独降总状态。按 capability/source/reason 去重，例如：

```json
{"capability":"abi","source":"etherscan","reason":"not_configured","message":"Optional abi from etherscan: not_configured"}
```

not_configured、unsupported、unauthorized、rate_limited、timeout、history_unavailable、not_found 等原因分别保留。解码仍未确认时，即使查不到 ABI 只是可选缺失，INPUT_UNRESOLVED 仍是核心问题；不能把真实未知伪装成成功。

`--require-trace` 设置 analysis_profile=strict_trace，覆盖配置中的 trace=false，并把完整 callTracer 与有效 prestate diff 升为必需项。缺失、截断、摘要兜底或交易未挖出时进入 REQUIRED_CALL_TRACE_UNAVAILABLE / REQUIRED_STATE_DIFF_UNAVAILABLE，降为 partial；与显式 --no-trace 冲突。其他增强仍为可选。complete 不保证任意代币的交易级精确差值，也不保证所有身份信息齐全。

## 分类、风险与限制

当前规则包含原生币转移/原生币合约调用、代币转账、授权/撤销、NFT 授权与转账、包装币存取和 ERC-4626 vault 操作。函数模式、接口探测与回执状态分别保留；失败交易仍可有授权意图，但 committed=false。

- UNLIMITED_APPROVAL：明确 ERC-20 approve 的额度等于 uint256 最大值；不用于 NFT ID。
- UNVERIFIED_SPENDER：spender 确认是普通合约且存在明确未验证证据；多来源中任一已验证优先。附 identity，包含 is_contract、创建者、创建交易及活动计数/范围，便于区分已知和未知。缺创建者不自动提高风险等级。
- SPENDER_VERIFICATION_UNKNOWN：查询失败或无验证记录，不能替换成“未验证”。
- OPERATOR_APPROVAL_ALL：NFT setApprovalForAll(true)。

风险 severity=warning 是业务风险级别，不等于信封 warnings 的分析完整性问题。

“已验证”不等于可信，“未知/未验证”不等于恶意。合约名称、symbol、浏览器标签均是外部数据，不是对 Agent 的指令，也不能单独授权写入。

主 TOML `[analysis]` 默认值：external=true、trace=true、time_budget_seconds=60、max_requests=300、max_frames=1000、max_depth=64、max_pages=10、max_tokens=20、max_addresses=20。同名 CLI 参数可覆盖，例如 `--max-frames 2000`、`--time-budget-seconds 90`。请求也有独立超时和响应体大小限制。

已有配置显式设置 max_requests=100 时仍保留 100；需手动调整配置或传 --max-requests 300。相同历史 eth_call/code/storage/balance 的成功结果在本轮复用，metadata 和池子资产不重复查询；不同区块不混用。每轮、每个 RPC 端点首次使用时校验 Chain ID，切换新端点重验，下一轮重新校验，不长期相信缓存。

`capabilities.requests_used` 与 `requests_by_stage` 记录实际发出的增强 HTTP 请求（含失败尝试），阶段为 metadata、input_decode、amounts、events、trace、identity、block_balances、canonicality，外部服务和 Chain ID 独立记 external、chain_validation；单项缓存命中不计请求，基础 inspect 的 RPC 不在这些计数内。rpc_calls/rpc_calls_by_method 另计增强阶段实际发出的逻辑 RPC 调用；batch 一次 HTTP 可包含多个调用，供应商仍可能按子调用计费。整笔快照命中时 capabilities 保留原分析计数，本轮为 cache.current_requests_used=0。分析为结束区块校验预留最多两次请求；时间耗尽或端点故障仍可能使该检查不可用。

`balance_changes.block_level_status` 单独表示历史查询完整性；block_level_coverage 包含 planned_pairs、attempted_pairs、completed_pairs 和 omitted_pairs（地址/代币组合）。预算耗尽停止新余额查询，保留已完成结果与遗漏范围；不能把空结果当作全部完成。地址/代币数量另受各自上限约束。

该预算用于基础交易取得后的增强阶段；基础 RPC 查询仍使用原有独立超时和有限重试。浏览器故障不触发交易发送或业务重试。

达到预算、深度、分页或帧数限制时保留已有结果并标记不完整。max_frames 同时限制日志增强数量，max_tokens/max_addresses 限制历史余额查询范围。原始日志仍保留在 inspection 或 events.read 的原始字段中。

顶层 ok=true 只表示基础查询与分析请求完成，不代表每个可选来源都成功。Agent 必须检查 analysis_profile、各部分的 status/complete、warnings 与 enhancements_missing；缺数据时报告事实，不输出“没有风险”或“完整追踪资金”等无依据结论。

## fund_flows：付出与得到

默认观察原交易 from；`--focus-address` 可指定其他地址，同时加入余额查询关注对象。fund_flows 不依赖项目 ABI：独立匹配 ERC-20 Transfer、ERC-721 Transfer、ERC-1155 TransferSingle/TransferBatch。即使自定义 ABI 没覆盖标准日志，也不丢弃这些流量。

| 字段 | 口径 |
|---|---|
| native.spent_wei / received_wei | 观察地址在已生效 CALL/CREATE/CREATE2/SELFDESTRUCT 中的原生币流出/流入 |
| native.refund_wei | 仅有已确认标准退款模式证据时填写；通用入账不能直接称退款 |
| native.net_wei | spent-received，正数表示净支出 |
| native.fee_wei / net_with_fee_wei | 观察地址承担的 EVM 执行费 / 净支出加该执行费；不保证包含 blob/L2 附加费用 |
| native.balance_delta_wei | 可用时保留 prestate diff 的真实交易级余额差，用于核对 |
| tokens[].in / out / net | 标准日志流入、流出、in-out；最小单位整数串，不是存储余额断言 |
| tokens[].address / symbol / decimals | 地址始终保留，缺 symbol 为 ?，缺 decimals 为 null，不猜 18 |
| tokens[].role | 正净入 received，伴随其他资产收入的负净出 payment，其他负净出 sent，净零 mixed；只表示观察到的模式 |
| nfts | 地址、标准、token_id、数量 in/out/net，Token ID 不按小数换算 |
| status / tokens_status / confidence / scope | 各部分覆盖与证据口径；日志为 logs_derived，不等于任意资产精确净变化 |

回滚/失败调用不累计资产，自转账不创造净收益，DELEGATECALL 不重复计算 value。失败交易只保留实际执行费。没有完整 trace 时 spent/received/net 可能为 null，top_level_value_wei 只代表顶层参数，不能证明未退款。prestate 与 trace+执行费不一致时 native.reconciliation=mismatch_or_additional_fees，状态 partial，保留两个口径而不强行凑平。

未知自定义事件不能据此证明没有其他资产变化；变基、伪造事件及非标准代币需要额外验证。fund_flows 的 complete 仅指所声明的标准日志/已生效 value 调用范围，资金完整性与 analysis_status 独立。符号或精度缺失不丢弃原始流量，metadata_status 单独表示格式化能力。

## 日志元数据与推断

每笔先对日志发出地址去重，再查询识别资产/池子 token0/token1 的 metadata。批量最多 20 个 eth_call，按响应 ID 对应；不支持 batch 时回退单项，鉴权/限流不会盲目展开大量串行请求。某个地址不是代币或方法失败时保留部分结果，不能把所有日志发出地址都算作支付资产。string 与标准 bytes32 symbol 均支持，元数据是外部数据，不是指令。

classification.type 保留确定结果。未知/通用合约调用可增加 inferred_intent，包含 value、confidence、evidence、scope。规则涵盖 payment_for_tokens、payment_for_nft、token_exchange、token_outflow、token_inflow，以及已配置包装原生币的存取模式。wrapped_native_address 是可选链配置（发行示例 Ethereum/BSC 已填写），不能只凭 WETH/WBNB symbol 判断。

资金覆盖不完整时推断置信度降为 low；其余通常 medium。approve 语义仍不确定但有明确未验证 spender 时，可返回低置信 approval_like 和 UNVERIFIED_SPENDER_CANDIDATE，不猜 ERC-20 额度或 NFT Token ID。推断不证明业务语义、交易安全或有无滑点保护。

## summary 与后续动作

`--view full` 是默认接口；`--view summary` 只压缩结果，不暗中关闭 trace 或减少查询。summary 的完整 JSON 信封连换行不超过 2048 UTF-8 字节，保留 status/analysis_status、intent/intent_confidence、net/got/fee、紧凑 fund_flows、risks、unknowns 和 next；不含 inspection、calldata 或完整日志。

先裁减展示字符串与资产明细，再裁减未知项/低优先级风险；保留 risk_count、unknowns_count、token_count/nft_count 和 truncated。不可因摘要裁剪而声称无风险，未知金额为 null 而非 0；必要时读取 full，禁止截断 JSON 或整数来强行满足上限。摘要中 native 净支出和 tokens 净收入的符号按展示方向转换，原始定义以上表为准。NFT 同样出现在得到/付出展示中。

部分成功保持 ok=true、error=null，核心问题在 issues 中带 suggested_next，另有 data.suggested_next。真正失败则 error.suggested_next。建议是结构化 module/operation/params/reason，或指向已有数据的 action/path，并标注 requires；不自动执行。只有实际取得、selector 校验匹配的签名才建议 raw.decode，不凭空生成 buy(uint256)。未知时可建议查询目标入站历史、查看 fund_flows 或修复配置，不能据此广播。

```bash
python transaction.py analyze --describe
python transaction.py plan --describe
python address.py history --describe
python address.py actions --describe
python raw.py patch --describe
python contract.py introspect --describe
python contract.py diagnose --describe
```

这七项机器帮助不读取配置、不加载私钥、不联网，输出用途/禁用场景、JSON 参数 schema、必填/默认值、稳定字段和成本影响因素。chain 仍必须显式提供；耗时和请求量是条件相关估计，不承诺固定 3–8 秒。

## 地址历史与 actions

```bash
python address.py history --chain bsc --address "$OWNER" --limit 50
python address.py history --chain bsc --address "$CONTRACT" --direction in --limit 50
python address.py actions --chain bsc --address "$OWNER" --limit 50
```

history 默认最近 50 笔，范围 1–200；direction=in/out/both，--to 可过滤目标。复用 Blockscout 的 /addresses/{address}/transactions 与 Etherscan V2 account/txlist；API Key 继续在本地 explorers.local.toml。无服务、鉴权失败、限流和真实空列表分别返回，不用普通 RPC 无限扫块。供应商可能对历史和 ABI 查询采用不同套餐权限。

输出统一 hash、block_number、timestamp、from/to、value_wei、selector、status 和 reported_function；reported_function 保留浏览器原标签。resolved_function 优先使用能解码 calldata 的内置签名，其次使用浏览器标签，最后回退 selector。该字段含 name、signature、source、ambiguous、candidates。ERC-20 与 ERC-721 的 approve 共享签名，ambiguous=true 时需核对目标接口。此解析不增加网络请求。complete 表示该过滤范围的数据源已耗尽，status 表示本次窗口请求是否满足；next_cursor 继续分页，包括未消费完的同页记录。cursor 绑定链、地址和过滤条件，不能携带任意远程 URL。

actions 只聚合窗口内该地址发出的直接调用候选，按目标/selector 统计 count/success/failed/unknown；目标是否合约未知时保留 null，不把任意非空 input 当作已验证合约身份。scope/limit/complete/next_cursor 必须一起报告，不代表全部生涯、token-event-only 活动或内部调用。

## transaction.plan：来源到实时预览

```bash
python transaction.py plan --chain sepolia --hash "$TX_HASH" --include-inputs
python transaction.py plan --chain sepolia --hash "$TX_HASH" --address "$ACCOUNT"
```

wallet 和 address 是可选且互斥的发送者。仅给链与交易哈希时，`status=template_only`、`preview=null`、`simulation.status=skipped`，并返回 `SIMULATION_SKIPPED_NO_SENDER`。原收件人保留，可应用已有可靠依据的显式改写。recipient:self 或需要核对调用者的改写缺发送者时，返回 SENDER_REQUIRED 并保留已取得的候选与模板。多候选继续显式选择。公开地址与无发送者模式无需在 CLI 或 SDK 加载钱包密钥。

```bash
python transaction.py plan --chain base --hash "$TX_HASH" --wallet trading --abi erc20 \
  --override '{"amount":"2000000000000000000"}'
python transaction.py plan --chain bsc --hash "$TX_HASH" --wallet trading --abi uniswap_v2_router \
  --override '{"args":{"amountIn":"2000000000000000000"}}'
```

第一例是 ERC-20 transfer 的最小单位简写，第二例是 Router 的明确参数名。override.args 支持完整数组或唯一顶层名称对象；tuple/数组参数通过完整顶层值替换，value_wei 单独覆盖。amount 简写只接受能唯一确定主要输入的标准接口，歧义或重复覆盖拒绝。

沿用 template_safe 规则，只有显式/已确认 ABI 才可编辑参数；提供发送者时，已确认标准受益人改为该地址并展示 parameter_diff，显式 override 可覆盖。owner、钱包绑定证明/签名、deadline 不自动重写；显式 deadline 覆盖单独处理，未知 calldata 可原样提取，或经显式字段映射改写。提供发送者后可实时模拟改写结果。源码/接口模式未确认时可明确传 ABI/signature，不将猜测直接转为可执行模板。

输出 template、parameter_diff、preview、source_status、template_safe 和 warnings。指定发送者时实时读取 pending Nonce、余额、费用并模拟、估价，不复制原 Nonce/Gas/费用，不预留 Nonce，不写广播意图。使用观察钱包地址也能预览；若仅配置私钥变量则在本地派生地址，绝不输出或外传密钥。此命令不接受 execute/skip_simulation/nonce，不能广播。模拟或余额检查失败时 ok=false、data.status=blocked，仍保留 template/diff 和 suggested_next。

## 内部原始 input 与输出开关

```bash
python transaction.py analyze --chain sepolia --hash "$TX_HASH" --view full --include-inputs
```

每个内部/声明调用都有 selector、input_bytes、input_available。input_bytes 包括 4 字节 selector；676 字节对应 1352 个 hex 字符，不含 0x。缺失为 null，不伪造 0x；真正空输入才是 input_bytes=0。原始数据不截断。

默认返回不包含内部 input/input_raw；--include-inputs 才同时打开 input_raw 与旧 input 别名。这是明确的输出变更，旧客户端需加开关。默认 full 的顶层 inspection 仍保留原始交易。summary 与 include-inputs 冲突并报参数错误；不会以超出 2KB 的摘要偷偷携带 calldata。

开关只控制投影，内部分析和本地快照仍保存原始 trace。include-inputs/full/summary 共享快照；缓存版本已更新，不删除原有状态记录。path/call_path/field_path 使用整数数组，不能把交易内序号、区块日志索引和路径混用。

## 7702 上下文与已知协议解包

`classification.type=eip7702_delegated_execution` 包含 delegator/delegators、manager、confidence、status、evidence_scope、authorization_intents、delegation_accounts 和 requires_original_authorization_context。普通已委托 EOA 发送一笔 approve，并不自动意味着执行了委托代码。

优先使用参与执行账户的 prestate 代码标记和真实 trace；必要时增加一次带代码的 prestate 查询，并在地址/请求预算内补查候选。区块末/浏览器当前证据降级，签名授权仅证明授权意图，不证明当时 nonce/代码条件校验成功。若授权列表可能改变同一账户的 prestate 委托指针，也保守降为 candidate，不把旧代码当执行实现。多个账户不擅自挑一个，单纯 ERC-1271/ecrecover/selector 不足以确认 7702。可用 evidence_scope/status 区分 observed 与 candidate，不把 from 固定当 delegator。[EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)、[ERC-1271](https://eips.ethereum.org/EIPS/eip-1271)

链配置可添加 delegation_managers 地址数组；默认 Sepolia 包含本次提供的公开管理器，其他链须明确配置，不跨链推断。识别 `redeemDelegations(bytes[],bytes32[],bytes[])` / 0xcef6d209；仅对已配置地址、规范 ABI 数组和受支持的全零 mode 解包。

single CALL 的 executionCalldata 按 20 字节 target、32 字节 value、后续 calldata 读取，不扫描任意字节找 selector。这些结果放 declared_calls，source=encoded_execution、execution_index 从 0 开始、execution_observed=false、committed=null、path=null，不与真实 internal_calls 或 fund_flows 合并。其他模式/不完整 payload 标为 unsupported/partial，不按同一布局猜读。[ERC-7579 执行编码](https://eips.ethereum.org/EIPS/eip-7579)、[DelegationManager 接口](https://github.com/MetaMask/delegation-framework/blob/main/src/interfaces/IDelegationManager.sol)

外层已知管理器不再作为 plan 模板：完整且唯一的合格业务候选自动生成内层预览并模拟；多个/不完整/无合格候选返回 CONTEXT_DEPENDENT_CALL、template=null 和候选，要求显式选择。不能因为 outer eth_call 未 revert 就认为普通钱包复现了原授权流程；本版不创建授权、不重新签名。

## take-call / take-candidate：选择实际或声明的业务 CALL

```bash
python transaction.py plan --chain sepolia --hash "$TX_HASH" --wallet trading \
  --take-call "$CONTRACT:$SELECTOR" --abi uniswap_v2_router \
  --override '{"recipient":"self","deadline":"now+1800"}'
python transaction.py plan --chain sepolia --hash "$TX_HASH" --wallet trading --take-call 'path:0.1.2'
python transaction.py plan --chain sepolia --hash "$TX_HASH" --wallet trading --take-call 'execution:0'
python transaction.py plan --chain sepolia --hash "$TX_HASH" --wallet trading --take-candidate 0 --include-inputs
```

--take-candidate N 使用本次显示的 candidate_index（零基），与 --take-call 互斥。已知管理器优先按声明 execution_index 形成候选，不因 trace 可用性变化而重排；其他完整 trace 候选按路径顺序形成，只含普通已生效、输入完整的非预编译内部 CALL。声明不完整时禁止自动接管，但可显式选择已取得的受支持执行项。

--include-inputs 同样适用于 plan，为 candidates/source_call 附 input_raw；输入不截断，默认不返回该字段。模板 data 始终完整。多候选 template=null；单候选后遇到 ABI_REQUIRED、参数冲突、解码或模拟失败，仍保留原始内层模板和候选，而非退回外层。

完整地址:selector 在完整 callTracer 中匹配；多个结果返回候选，不取第一条。trace 截断时必须指定已知 path；无 trace 时仅可从完整的已知协议声明匹配，亦可显式 execution:N。path 是根 [] 下的零基数组路径，execution:N 是声明索引，二者不能互冒。

只允许普通非预编译 CALL；reverted、DELEGATECALL、CALLCODE、STATICCALL、CREATE 不转换。声明来源须有成功来源回执，但仍不标为真实 trace 执行证据。目标、value、calldata 来自所选调用，不复制外层总 value；ABI/signature 也只用于该调用，避免拿 swap ABI 去解外层 manager。

原调用者/上下文保留在 source_call 和 template.source；声明无法确定的原调用者为 null，不猜成交易 from。实际模拟始终从选定钱包发起，simulation_context 即使失败也保留，不冒用原合约身份。失败时 simulation.details 保留 RPC 已脱敏的 revert data/可用解码，不保证每个 RPC 拒绝都能区分出具体合约原因。

### 语义改写与 field_map

recipient=self 或完整地址，deadline=整数秒或 now+正整数。可靠 ABI 下递归定位唯一 recipient/to/receiver 地址字段与 deadline/expiry/validUntil 无符号字段；多个候选须改为明确参数替换，不随机选择。与 override.args 重复覆盖同一字段拒绝，owner/签名/证明不自动重写。

无可靠 ABI 时须显式提供 field_map，例如下面仅说明语法，不能照搬为任意函数的真实位置：

```json
{
  "recipient": {"word": 6, "type": "address"},
  "deadline": {"word": 5, "type": "uint256", "unit": "seconds"}
}
```

映射只支持 selector 后完整 word，两个字段不能重叠，不支持 packed 或动态偏移猜测。确认 ABI 编辑与原始映射二选一；映射不提升 template_safe，始终 payload_unverified=true，当前模拟失败仍保留模板和 word diff。直接改动有效期可能破坏签名覆盖，工具不会补签。

## NFT 参数与 contract-assigned ID

明确 override.tokenId 仅改可靠 ABI 中唯一 tokenId/token_id（可嵌套、忽略名字前导下划线）的无符号字段；歧义或与 args 同时覆盖拒绝。常见 ERC-721 操作尽力在 pending 查询 ownerOf，并在 nft_context 标注 matches_wallet/different_owner/unavailable；不同归属不等于一定无权操作，最终还需模拟授权路径。

"tokenId":"auto" 仅用于显式/已确认接口中的 createLock(uint256,uint256)、create_lock(uint256,uint256) 及对应 For(uint256,uint256,address)，且接口不含 tokenId 入参。不传旧 NFT ID、不读取计数器猜 ID；nft_context.expected_token_id=null，实际 ID 只能从成功回执 minted_nfts 确认。仅 selector/4byte 猜测不够，其他函数或已有 NFT 操作报 TOKEN_ID_AUTO_UNSUPPORTED。用户显式 ABI/signature 仍是用户接口断言，并不提升为源码验证。

```bash
# 仅在已确认该合约接口后使用；第二参数的锁仓单位须先核实
python transaction.py plan --chain sepolia --hash "$TX_HASH" --wallet trading \
  --take-candidate 0 --signature 'createLock(uint256,uint256)' \
  --override '{"tokenId":"auto","recipient":"self"}' --include-inputs
```

amount 简写在这些已确认创建接口中对应第一个 uint256。两参数版本以模拟钱包作为 caller-owned 接口收件人，不新增 calldata 字段；For 版本改写第三个地址参数。不能把两参数版本改成任意收件人。缺少交易 deadline 的接口拒绝 deadline 别名，不把锁仓时长/解锁时间当 now+1800；锁仓参数使用明确 args 和已核实单位。

本轮公开 veNFT 样本 selector 与 createLock 两参数签名匹配，原 NFT 编号来自 mint 日志。公开 Sourcify 仅验证了代理，未提供实现 ABI；该结果不证明第二参数单位或具体业务语义，不把旧 ID 当作输入、不硬编码该项目地址和实现。

## timing：过期参数不静默复用

分析输出 timing.reference_timestamp、clock_source、fields[]；每项包含 field_path/name/raw/type、deadline、expired、expires_in_s、unit、semantic_source/confidence。秒数以整数串保留；field_path 为嵌套参数索引数组。

分析用明确标注的 local_utc_clock，缓存命中也重新计算 timing 和 DEADLINE_EXPIRED 风险，不额外联网。它是当前时钟下的提示，不是某个合约执行结果。plan 在需要检查/修改时间时读取 latest_block_timestamp，now+1800 以该值计算；获取链时钟失败时拒绝相对时间，不猜一个基准。

只对已解析 ABI 的名字/类型或明确字段映射注释；不在未知 bytes 里扫描类似时间戳的大整数。普通命名采用秒的显式假设；零、最大值或疑似毫秒等特殊/单位不明值保留 unknown/null，不无依据称永久有效。已知 V2 Router 的 deadline 按标准模式解释。过去时间标风险，时间相等时 expires_in_s=0；真实边界和是否执行检查仍以代码和模拟为准。

## raw.patch：整 word 改写后强制 eth_call

```bash
python raw.py patch --chain sepolia --to "$CONTRACT" --address "$OWNER" \
  --data "$CALLDATA" --word 6 --type address --value "$RECIPIENT" --verify
python raw.py patch --describe
python contract.py introspect --describe
python contract.py diagnose --describe
```

word 从 selector 后第一格的 0 开始，byte_offset=4+word×32。--type address 左补零到 32 字节；uint256 接受十进制整数或 hex 数量，检查范围；默认 bytes32 必须正好 32 字节，不能把 20 字节值暗自当地址。只替换已存在的完整 word，不改 selector、不插入/删除、不补齐半个尾部 word。

必须 verify=true 并提供 chain、to、wallet 或公开 address（二选一）；value 是被改的 word，value_wei 是模拟发送的原生币。使用 pending 状态 eth_call，成功只代表该调用未 revert，不证明 ABI、业务意图或未来上链安全。始终 payload_unverified=true，保留 data、parameter_diff、verification、标准 template。失败 ok=false/status=blocked，仍保留候选数据和 details；此命令不接受 execute，绝不签名广播。

带 payload_unverified 的模板交给 raw.write 后仍保留该标记和警告，并按原交易引擎再次实时模拟。CLI/SDK 使用公开 address 时不需要加载私钥。

## refunds：回流是净额的子集

fund_flows.refunds[] 记录 asset_type、address、amount_raw/amount_readable、from/to、kind/confidence/evidence 和 source。receipt_position 为交易内零基位置，log_index 是区块级索引；native 使用 call_path，不混淆编号。

已知标准退款可标 confirmed_refund；同资产先流出再从原对端或已生效路径收到、且候选回流合计不超过流出时，列为 refund_candidate。金额小不是依据；先借后还、空投、回款大于支出的收益不自动称退款。未知 ABI 的 WETH 微量回流也只能称疑似找零，不改变其精确数量。

refunds 已包含在 tokens.in/net 或 native.received_wei/net_wei，不再扣一次；native.refund_wei 继续只填已确认退款。历史 decimals 查不到时 amount_readable=null，不能拿当前元数据或 18 位假设冒充历史事实。summary 带 refund_count 和可裁剪的紧凑条目，仍限制 2KB。

## 本轮 Sepolia 只读验收

四笔参考交互和一笔复现授权均通过新版 transaction.wait 返回 confirmed；此前误报的授权仍确认 gasUsed=128310。新版 plan 自动选中兑换与 veNFT 的唯一声明业务调用，分别完整交付 868/68 字节原始输入；当前公共 RPC 模拟均明确回退，保留内层模板、候选、实际模拟钱包和 SIMULATION_REVERTED，不把它们报告为已执行。veNFT 预览采用显式接口断言，expected_token_id 保持 null；原始成功回执可提取其 ERC-721 mint 编号。

实链验收仅用临时观察钱包配置，禁用发送接口，未加载密钥。交易生命周期与候选/NFT 行为另用本地故障注入验证；测试脚本和真实样本明细不入仓库。

## 上一轮 Sepolia 只读验收（旧行为记录）

四笔公开样本均完成只读检查：委托外层能解包一条完整 676 字节的声明调用；它与直接兑换样本的 calldata 仅第 5、6 个 word 不同。两笔配套授权的 gasUsed 均为 128298。无 trace 时保留声明来源，未声称已验证真实第三层路径。

兑换样本的回流精确保留为 40555791 raw，交易内 receipt_position=7，区块 log_index=74，标为 refund_candidate；该公共节点拒绝历史精度查询，故 amount_readable=null。缓存摘要实测 1580 字节，禁用网络后仍可命中并刷新时间提示。

上一轮版本外层复现被 CONTEXT_DEPENDENT_CALL 拦截；内层显式映射生成了候选参数差异，但当前 eth_call 与 raw.patch 校验均返回 CALL_REVERTED，结果正确为 blocked。这证明失败数据交付与拦截路径有效，不代表当前状态可成功复现。全过程未签名、未广播；样本明细和临时测试不加入仓库。

## 广播结果与错误解码

执行结果规则见 [工具包故障处理](toolkit.md#交易执行与故障处理)。ok=false 不能直接解释为未发送或链上回退；广播后错误仍带 hash，确认故障先查原交易，避免重复业务。plan/raw.patch 的明确模拟回退使用 SIMULATION_REVERTED；RPC_REQUEST_REJECTED 不表示合约 revert。

错误 bytes 通过本地标准/已有 ABI 解码，保留全部候选、命名参数、source/confidence 和原始数据。ERC20InsufficientAllowance 的三个参数为 spender、allowance、needed；Expired() 只是名称模式，不能单凭 selector 确定过期的是交易 deadline。没有原交易 trace 时链上失败原因保持未知，不用最新状态的 eth_call 冒充历史事实。

## 本地缓存与强制刷新

已有 ABI/外部响应缓存继续使用；新增完整分析快照和按链/端点配置指纹/地址/区块哈希隔离的成功 metadata 缓存。schema、分析参数、focus、预算、ABI 内容和提供方配置参与快照键；full/summary 共享快照。Key 只保存散列，不保存原始 RPC URL 或凭证，缓存文件仍在被 Git 忽略的 .state/analysis-cache.sqlite3。单条快照超过 20 MiB 不保存；cache.status 明确 not_eligible、stored 或 unavailable_or_oversize。

规范链检查通过、已有 mined 回执的完整增强结果缓存 300 秒；含未完成部分的快照 60 秒。pending、基础请求失败或已观察重组不保存整笔快照。外部 ABI/成功元数据一小时、4byte 一天、地址历史页 60 秒；外部失败响应不缓存，分析快照中已有的缺失说明会随短 TTL 暂存。

cached=true 表示返回历史快照而非实时重验；analyzed_at、cache.canonicality_checked_at 和 ttl_seconds 保留时间信息，cache.current_requests_used=0。capabilities 中的原请求统计仍是生成该快照时的记录。--no-cache 强制跳过持久读取并更新缓存，但保留同次调用内部去重。plan 的费用、Nonce、余额、模拟始终实时查询。需要检查刚发生重组或新配置效果时必须刷新。

## 上一版本请求预算实测

使用用户提供的一笔 BSC 单跳 V2 swap，只读访问公共 RPC，分别以 100/200/300/500 请求上限、相同 120 秒时限对比；每档独立冷缓存后再热缓存。结果如下（增加批量元数据与分析快照之前的历史记录，仅该样本与节点）：

| 上限 | 冷缓存请求/耗时 | 热缓存请求/耗时 | 历史余额覆盖 |
|---|---|---|---|
| 100 | 34 / 7.14 秒 | 33 / 6.59 秒 | 9/9 |
| 200 | 34 / 6.66 秒 | 33 / 7.06 秒 | 9/9 |
| 300 | 34 / 6.75 秒 | 33 / 6.66 秒 | 9/9 |
| 500 | 34 / 6.78 秒 | 33 / 7.74 秒 | 9/9 |

默认 300 请求/60 秒另测为 34 次、7.24 秒，9/9 组余额完成，日志为 Transfer、Transfer、Sync、Swap。测试档位中最低的 100 已足够；这不是任意 swap 的最低预算，也不是每次固定消耗。冷缓存 34 次由 Chain ID 1、金额 4、事件 2、trace 尝试 2、身份 5、外部服务 1、历史余额 18、区块校验 1 组成。

此公共节点不支持 callTracer/prestateTracer，因此实际 trace 与交易级原生币差值仍 unavailable，不代表全能力分析成功。提高预算不能解决 unsupported、鉴权或历史状态缺失；支持 trace 的复杂交易消耗需另行测量。本次不发布交易哈希、原始回执或测试材料。

## RPC 要求与实时能力探测

历史交易内部调用使用 `debug_traceTransaction` 的 callTracer；交易级原生币差值使用该方法的 prestateTracer + diffMode。两种 tracer 需要分别支持。普通余额查询不依赖 debug；历史 N−1/N 状态余额还需要节点保留对应历史状态。`debug_traceCall` 是独立的调用模拟能力，仅在 probe 中测试，不是历史 analyze 的前提。

```bash
python chain.py probe --chain bsc --explorer
python chain.py probe --chain bsc --explorer --hash "$TX_HASH"
python chain.py probe --chain bsc --explorer --time-budget-seconds 90 --max-requests 150
```

probe 对 CSV 中每个启用端点分别探测，不通过备用端点掩盖某个端点失败；每轮探测对各端点首次使用时实时验证 Chain ID，新一轮重新校验。未提供 hash 时，最多搜索该端点最近五个区块的真实交易；没有样本则历史 tracer 为 not_tested，不使用虚构哈希测试。debug_traceCall 单独报告。Blockscout、Sourcify、Etherscan 按配置实时探测，禁用源为 not_configured，结果不使用分析缓存。RPC 配置无可用端点时仍可返回数据源探测。

输出含 rpc_name、health、sample_hash、tracers、explorers、requests_used、historical_trace_ready；不输出 URL、API Key 或原始错误正文。historical_trace_ready 表示本次至少有端点支持所需的各项 tracer，不保证所有交易年代都可用。区分 unsupported、unauthorized、rate_limited、timeout、history_unavailable、not_tested 等状态；探测同样消耗配额，受时间/请求预算限制。

CSV 仍为 chain,name,url,priority,enabled 五列。示例新增默认禁用的 BSC trace/archive 提示行；填写实际有权限的端点后才启用。以下供应商仅供核对能力，不能假设免费档全部开放 debug：

| 供应商 | BSC debug 注意事项与官方依据 |
|---|---|
| NodeReal | [Debug-BSC](https://nodereal.io/api-marketplace/debug-bsc) 需 Growth 或以上；核对 tracer 支持，不能假设 callTracer 权限同时包含 prestate diff |
| Chainstack | [BSC 能力说明](https://chainstack.com/build-better-with-bnb-smart-chain/) 的 debug API 从付费 Growth 档提供，另核对历史状态范围 |
| BlockPI | [BSC API 文档](https://docs.blockpi.io/build/api-reference/bsc) 列出 debug 方法；具体套餐、tracer、配额向供应商核实 |
| QuickNode | [BSC debug_traceTransaction](https://www.quicknode.com/docs/bnb-smart-chain/debug_traceTransaction) 可查方法支持；核对账号计划、tracer 与历史范围 |

探测成功仅反映当前端点、套餐与样本；切换供应商、链或查询更早交易时重新核实。

## 接口参考

- [Blockscout 合约 API](https://raw.githubusercontent.com/blockscout/agent-skills/main/blockscout-analysis/references/blockscout-api/smart-contracts.md)
- [Etherscan V2 合约信息](https://docs.etherscan.io/api-reference/endpoint/getsourcecode)
- [4byte API](https://www.4byte.directory/docs/)
- [Geth callTracer 与 prestateTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers)

- [Sourcify V2 查询接口](https://docs.sourcify.dev/blog/apiv2-lookup-endpoints/)
- [Sourcify API](https://docs.sourcify.dev/docs/api/)
- [Geth debug RPC](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-debug)

## 本轮只读集成验证

BSC 单跳样本实测：冷分析 9.51 秒，34 次增强 HTTP / 37 个逻辑 RPC 调用，metadata 覆盖 3 个地址、两种代币流量，4 条日志均解码。缓存摘要 1398 字节、约 0.001 秒，并通过阻断全部 RPC/HTTP 验证零网络；强制刷新约 9.01 秒。

本次公共节点未提供可用 trace，历史读取返回鉴权/权限拒绝，历史余额 9 组均未完成，因此 native/fund_flows 为 partial，tokens_status 为 complete；不能沿用上一个版本同一节点曾成功的 9/9 结论。过期参考交易的实时 unsigned plan 被模拟拒绝，模板和 diff 仍保留，无签名/广播。公共 Base Blockscout 历史请求返回鉴权错误，因此本轮不声称该公共服务可免 Key 使用；分页/空历史/限流/聚合等通过离线适配器回归验证。


## 合约地址分析的分工

给交易 hash 仍使用 transaction.analyze；给合约地址、需要展开代理/状态/源码线索或检查退出前提时，使用 contract.introspect / contract.diagnose。两者不替换资金流分析，也不自动接管广播。详见 [合约分析参考](introspect.md)，新增机器帮助与默认 summary 的边界在该文档中说明。

## 结论字段、调用深度与 word 提示

full 和 summary 共用 status、intent、intent_confidence、net、got、fee 与 fund_flows。摘要超出 2048 UTF-8 字节时可省略字段并标 truncated，保留的结论值与 full 一致。旧分析快照返回时会补齐结论。

callTracer 的 depth 等于 path 长度，根调用为 0。编码声明和没有层级的浏览器列表返回 depth=null。path: 定位真实路径，execution:N 定位编码声明序号。

展开 include-inputs 时，缺可靠 ABI 的输入可附 word_hints。报告最多检查 128 个完整 word，保留扫描数、截断、尾部字节和本机 UTC 参考时间。word_index 从 selector 后的第一格开始。同一格可列出多个低置信度解释，包括高位零填充的疑似地址、接近参考时钟的疑似时间戳和原始整数数量级。已知交易或调用地址的相等关系写入 matches。提示不添加代币精度或 deadline 语义，也不会驱动参数编辑。原始输入字节始终完整。

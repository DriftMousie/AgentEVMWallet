# 工具包技术参考（供 Agent 按需读取）

本文中的命令和文件路径均相对用户提供的工具包根目录，不是本参考文档或 Skill 的安装目录。先定位工具包，再执行命令。

面向 Hermes Agent 的本地 EOA 钱包工具包。12 个 Python 文件可独立运行，公共实现位于 `evmkit/`。默认只预览，显式 `--execute` 才签名或广播。

## 环境与初始化

先检查目标机器上的解释器和依赖，优先使用用户指定或当前可用的 Python 3.12 及以上兼容环境，不要求 Conda，也不要求激活任何特定名称的环境。下面的 `python` 表示已选定的解释器；如果系统只有 `python3` 或需要使用虚拟环境解释器的绝对路径，替换命令中的 `python`，并始终用同一解释器运行脚本和 `-m pip`。

```bash
# 使用目标机器上选定的 Python，无需激活特定 Conda 环境
python --version
# 已有环境可先检查下方离线示例；新环境安装运行依赖：
python -m pip install -r requirements.lock.txt
test -e .env || cp .env.example .env
chmod 600 .env
test -e config/rpc.csv || cp config/rpc.csv.example config/rpc.csv
python chain.py list
python wallet.py list
python token.py convert --amount '1.25' --decimals 6
```

`.env` 只保存钱包私钥；在 `config/rpc.csv` 填入 RPC URL；`config/settings.toml` 配置钱包标签、观察地址和链。示例观察地址只是占位地址。

### RPC CSV

```csv
chain,name,url,priority,enabled
base,primary,https://your-primary-provider.example/rpc,10,true
base,backup,https://your-backup-provider.example/rpc,20,true
ethereum,primary,https://your-ethereum-provider.example/rpc,10,true
```

以上 URL 是占位示例。`chain` 对应 TOML 的链别名；同一条链可添加任意多行，`name` 在链内唯一。`priority` 为非负整数，越小越优先，相同时按 CSV 行顺序；`enabled` 接受 `true/false` 或 `1/0`。禁用行允许空 URL，启用行必须有有效 HTTP(S) URL。模板行默认禁用，填写 URL 后改为 `true`。

TOML 的 `[rpc] csv_file = "rpc.csv"` 指定 CSV 路径，相对路径以 TOML 所在目录为基准。RPC 不再读取 `.env` 或进程环境变量。每个端点仍验证 Chain ID，并在读取故障时切换备用端点。

本地 `config/rpc.csv` 已加入忽略规则，避免提交 URL 内的供应商密钥；仓库分发 `config/rpc.csv.example`。已有本地 CSV 不会被程序覆盖。健康检查通过 `rpc_name` 标识端点，不返回完整 URL。

当前锁文件根据本机 Python 3.12/macOS 测试环境生成，包含 `eth-abi==6.0.0b1`，不绑定环境名称。这是已有环境的预发布版本；升级、换成稳定版本或迁移到其他平台后需重新运行测试。`requirements.txt` 固定直接依赖，`requirements.lock.txt` 固定运行时依赖闭包。

钱包选择支持 `id` 或唯一 `label`。观察钱包可以查询和预览，不能签名。所有写操作都必须明确指定钱包和链。

### 重复签名钱包检测

加载配置时，根据实际私钥派生地址进行检查，而不是只比较环境变量名或私钥字符串。两个可签名钱包指向同一地址时，结果 `warnings` 包含 `DUPLICATE_SIGNING_ADDRESS`，提示相关钱包 ID、公开地址及资金不隔离的事实；相关钱包公开信息新增 `duplicate_signing_wallets` 数组，列出其他同地址签名钱包 ID。`wallet list`、余额查询、写操作等使用同一配置的结果都会带告警。

例如 `TREASURY_PRIVATE_KEY` 和 `TRADING_PRIVATE_KEY` 相同，或两个钱包引用相同的私钥变量，都会告警。不同私钥不告警；只有地址、没有私钥的观察钱包引用同一地址，不当作重复签名钱包。

检测仅告警，不改变既有钱包选择规则，也不自动替换/生成私钥。需要资金钱包与交互钱包隔离时，应由用户配置不同私钥；Agent 不得把不同标签误认为不同地址。

环境变量优先于 `.env`；默认只读取项目根目录 `.env`。可通过 `--config`、`--env-file`、`--state-dir` 使用其他配置。相对路径按当前工作目录解析。

### 配置与地址错误定位

- 私钥缺失或为空时，`WALLET_ERROR` 的 message 指出钱包 ID、应设置的变量（例如 `TRADING_PRIVATE_KEY`）及实际 `.env` 路径，区分文件不存在与变量缺失/为空。进程环境可以提供私钥，因此不存在 `.env` 不会单独导致失败；同名进程环境变量优先。
- 错误 `data` 提供 `wallet_id`、`key_env`、`env_file`、`env_file_exists`、`config_file`。未配置 `key_env` 时指出 TOML 的具体钱包字段；私钥格式错误、地址不一致、观察钱包签名也说明应检查的位置，不回显私钥值。
- `wallet list` 保持逐钱包返回；有问题的行保留原有 `error` 错误码，并新增 `message`、`details`。顶层 `ok=true` 不代表每个钱包均配置正确。
- 地址校验保持错误码 `INVALID_INPUT`，附带 `data.field` 和 `data.reason`：`invalid_type`、`invalid_length`、`invalid_characters`、`invalid_checksum`。地址要求 40 个十六进制字符，可带 0x 前缀；混合大小写需满足 EIP-55，全小写或全大写仍兼容。错误不回显完整输入，避免误填私钥时泄漏。

## 自动分析入口

`transaction.py decode` 现在自动识别标准 selector，并按配置补充已验证 ABI/4byte 候选。完整分析使用 `transaction.py analyze --chain … --hash …`；`inspect` 保持基础查询。事件日志也可自动解码标准布局。数据源配置、历史精度、风险和调用树的完整性说明见 [自动分析参考](analysis.md)。

## 模块与调用

| 文件 | 子命令 |
|---|---|
| `chain.py` | `list`、`health`、`block`、`fees` |
| `wallet.py` | `list`、`validate`、`balance`、`balances` |
| `token.py` | `info`、`convert` |
| `transaction.py` | `inspect`、`status`、`nonce`、`gas`、`wait`、`decode`、`analyze`、`export-template`、`accelerate`、`cancel`、`replay` |
| `contract.py` | `functions`、`encode`、`decode`、`call`、`write` |
| `transfer.py` | `native`、`erc20` |
| `approve.py` | `allowance`、`set`、`revoke` |
| `nft.py` | `info`、`balance-batch`、`transfer`、`transfer-batch`、`approve`、`revoke`、`mint` |
| `events.py` | `read` |
| `raw.py` | `encode`、`decode`、`call`、`write`、`template`、`reencode` |
| `signing.py` | `message`、`typed`、`verify-message`、`verify-typed` |
| `batch.py` | `run` |

每个子命令的参数可用 `python <文件> <子命令> --help` 查看。CLI 参数使用连字符，JSON 参数使用下划线。数组参数是 JSON，例如 `--args '["42"]'`；布尔参数使用 `--execute`、`--no-approved` 等。

```bash
# 检查 RPC，查询余额
python chain.py health --chain base
python wallet.py balance --chain base --wallet treasury
python wallet.py balances --chain base --wallets '["treasury","trading"]' --tokens '[]'
# 同样支持逗号分隔；仅 balances 子命令中的 --wallet 是 --wallets 别名
python wallet.py balances --chain base --wallets treasury,trading --tokens '[]'
python wallet.py balances --chain base --wallet trading --tokens '[]'
python token.py info --chain base --token 0x0000000000000000000000000000000000000001

# 先预览，再显式执行；--wait 等待链配置的确认数
python transfer.py native --chain base --wallet treasury \
  --to 0x0000000000000000000000000000000000000002 --amount '0.001'
python transfer.py native --chain base --wallet treasury \
  --to 0x0000000000000000000000000000000000000002 --amount '0.001' --execute --wait

# 原生币或 ERC-20 可以使用 --amount-raw 最小单位整数，不能同时给 --amount
python transfer.py erc20 --chain base --wallet trading \
  --token 0x0000000000000000000000000000000000000001 \
  --to 0x0000000000000000000000000000000000000002 --amount-raw '1000000'

# 指定额度授权；无限授权显式使用 --unlimited
python approve.py set --chain base --wallet trading \
  --token 0x0000000000000000000000000000000000000001 \
  --spender 0x0000000000000000000000000000000000000002 --amount-raw '1000'

# 标准 ERC-20 无需外部 ABI；ERC20 和 OWNER 替换为用户选定的真实地址
python contract.py functions --abi erc20
python contract.py encode --abi erc20 --function 'balanceOf(address)' \
  --args "[\"$OWNER\"]"
python contract.py call --chain base --contract "$ERC20" \
  --function 'balanceOf(address)' --args "[\"$OWNER\"]"

# NFT standard 使用 721 或 1155；Mint 必须提供具体函数
python nft.py mint --chain base --wallet trading \
  --contract 0x0000000000000000000000000000000000000001 \
  --signature 'mint(address,uint256)' --args '["0x0000000000000000000000000000000000000002","1"]'

# 事件默认读已达到配置确认数的区块，不自动访问 metadata URI
python events.py read --chain base --contract 0x0000000000000000000000000000000000000001 \
  --from-block 100 --to-block 200 --chunk-size 100

# 消息和结构化数据签名也默认只预览
python signing.py message --wallet trading --message 'hello'
python signing.py typed --chain base --wallet trading --typed-data examples/typed-data.json
```

上例地址均为占位地址，合约操作需要换成实际合约地址。

`wallets` 可以是 JSON 数组，也可以是逗号分隔字符串，首尾空格会清理。包含逗号的标签使用 JSON 数组；空条目如 `trading,,treasury` 会报错。CLI 中 `--wallets`/`--wallet` 只指定一次，不能混用覆盖。JSON/Python 接口使用规范字段 `wallets`，例如 `{"chain":"base","wallets":"trading,treasury"}`；`tokens` 等其他数组参数仍使用 JSON 数组。

### 费用单位与兼容变更

`chain.py fees` 和 `transaction.py gas` 返回单位明确的费率，而非无单位数字。例如 legacy 链的 `data`：

```json
{
  "fee_model": "legacy",
  "rate_unit": "per_gas",
  "fees": {"gasPrice": {"wei": "34", "gwei": "0.000000034"}},
  "scope": "EVM execution; excludes chain-specific data fees"
}
```

EIP-1559 链的 `fee_model` 为 `eip1559`，`fees` 包含 `maxFeePerGas` 和 `maxPriorityFeePerGas`，各自同样带 `wei`、`gwei`。费率是每单位 Gas 的价格，不是整笔交易费用；1 gwei = 1000000000 wei。转换使用整数和十进制字符串，无浮点舍入。

**调用方需要适配：**原先读取 `data.fees.gasPrice` 的程序，现在应读取 `data.fees.gasPrice.wei`，需要展示 gwei 时读取 `.gwei`。EIP-1559 对应字段同理。不要把旧结果当作 gwei，也不要把新结构直接拼入签名交易。

写操作预览新增 `data.fee_summary`，采用相同的带单位结构；`data.transaction.gasPrice/maxFeePerGas/maxPriorityFeePerGas` 保留 RPC 原始整数语义，单位均为 wei/gas。`execution_fee_cap_wei` 是整笔执行费上限，单位 wei。显式费用输入 `--gas-price-wei`、`--max-fee-per-gas-wei`、`--max-priority-fee-per-gas-wei` 全部接受整数 wei/gas，例如 34 gwei 必须输入 `34000000000`，不能输入 `34`。

### ABI 的选择

- 标准 ERC-20 只读调用可不传 `--abi`：支持 `balanceOf(address)`、`allowance(address,address)`、`name()`、`symbol()`、`decimals()`、`totalSupply()`，函数名或完整签名均可。
- 内置 ABI 包括 erc20、erc721、erc1155、weth、erc4626。`--abi erc20` 显式选择 ERC-20 ABI，不访问外部网站、不依赖工作目录中的 ABI 文件。包含常用查询、transfer/approve/transferFrom 和 Transfer/Approval 事件，可用于函数列表、编解码、合约调用、交易解析和事件解码等现有 ABI 接口。
- 自定义 ABI 仍支持 JSON 数组文件、包含 `abi` 的 artifact 或 JSON 请求中的内联数据；不会被内置 ABI 覆盖。自定义写操作应明确提供 ABI；省略 ABI 只对上述 ERC-20 只读调用生效，其他函数返回 `ABI_REQUIRED`。
- `examples/contract-abi.json` 仅演示 set/get 自定义合约，不是 ERC-20 ABI，也不代表任何链上部署。真实代币优先使用内置 `erc20`；已有 `examples/erc20.abi.json` 或其他 ABI 文件也可照常使用。
- 内置接口不保证非标准代币都实现这些方法；调用失败需核对 ABI 和实际合约。查询 `balanceOf`、`allowance`、`totalSupply` 返回最小单位原始值，结合 decimals 换算，不把结果直接解释成整枚代币。

JSON 请求示例（地址替换为实际值）：

```json
{
  "operation": "call",
  "params": {
    "chain": "base",
    "contract": "0x0000000000000000000000000000000000000001",
    "function": "balanceOf(address)",
    "args": ["0x0000000000000000000000000000000000000002"]
  }
}
```

结果继续使用 `data.return_data` 和 `data.values`，与外部 ABI 调用的输出一致。若要显式指定，给 params 加上 `"abi": "erc20"` 即可。

## 交易哈希 → 调用模板 → Raw 交互

```bash
# TX_HASH 是用户提供的 ERC-20 transfer 交易哈希；必须同时指定链
python transaction.py inspect --chain base --hash "$TX_HASH"
python transaction.py decode --chain base --hash "$TX_HASH" --abi erc20
python transaction.py export-template --chain base --hash "$TX_HASH" \
  --abi erc20 --output template.json

# 无显式 ABI 时自动尝试识别；未确定的候选仅导出原始 calldata，decoded.status 为 unknown
# 输出文件不会覆盖已有文件
python raw.py template --template template.json
python raw.py reencode --template template.json --args "[\"$RECIPIENT\",\"43\"]" --output template-43.json
python raw.py write --chain base --wallet trading --template template-43.json
# 确认要发送后，重复命令并加 --execute；可加 --wait
```

模板文件直接保存模板对象，标准输出则返回统一结果信封。模板只带来源、目标、金额、calldata 和可选解码信息，不带原 Nonce、费用或签名。

上述重新编码仅针对 `transfer(address,uint256)` 样本；RECIPIENT 为本次选定接收者，43 为代币最小单位的示例金额。其他函数必须按其实际解码参数修改，不直接复用此参数列表。

`raw write --template` 可以显式覆盖 `--to`、`--value-wei`，不能同时覆盖 `--data` 或 `--signature`。修改已解码参数必须通过 `reencode` 重新编码；参数和 calldata 不一致会拒绝执行。模板链必须与执行链一致。

没有用户 ABI 时会使用标准/已验证 ABI 和最后的 4byte 候选；候选不冒充确定参数。顶层交易输入不等于内部调用，analyze 可使用可选 RPC trace 或浏览器摘要，并标注完整性。原交易成功也不保证换钱包或换时间后成功，尤其要检查接收地址、截止时间、签名和白名单证明。

```bash
python raw.py encode --signature 'transfer(address,uint256)' \
  --args '["0x0000000000000000000000000000000000000002","100"]'
python raw.py call --chain base --to 0x0000000000000000000000000000000000000001 \
  --signature 'balanceOf(address)' --args '["0x0000000000000000000000000000000000000002"]' \
  --output-types '["uint256"]'
```

`--data` 接受偶数长度的 `0x` 十六进制数据；函数签名必须完整，使用 `uint256` 等规范类型。动态参数、tuple 和数组均按 ABI 编码，不做十六进制字符串替换。

## JSON 与 Python 接口

```bash
python token.py --input-json examples/convert-request.json
echo '{"operation":"convert","params":{"amount":"1.25","decimals":6}}' | python token.py --input-json -
```

输入固定为 `{"operation":"...","params":{...}}`，JSON 输入不能混入业务 CLI 参数。ABI、模板、typed_data 在 JSON 请求里可直接使用对象或数组，在 CLI 中使用文件路径；ABI 另支持内置名称 erc20、erc721、erc1155、weth、erc4626，标准 ERC-20 的 contract call 可省略 ABI。

输出固定为：

```json
{"schema_version":1,"ok":true,"operation":"token.convert","chain_id":null,"wallet":null,"data":{"amount_raw":"1250000","amount":"1.25","decimals":6},"warnings":[],"error":null}
```

失败时 `ok=false`，`error` 包含 `code`、`message`、`retryable`，退出码为 1；成功为 0。大整数、链上金额与 Token ID 为字符串，协议版本、链 ID、精度、确认数等结构字段为整数。RPC 原始字段中的整数按字符串输出。

```python
from evmkit.config import Config
from evmkit.operations import Toolkit

toolkit = Toolkit(Config())
response = toolkit.run("token", "convert", {"amount": "1.25", "decimals": 6})
assert response["ok"]

# 每个入口也可导入（token.py 的名称与标准库冲突，Python 调用优先使用 Toolkit）
import transfer
response = transfer.run({"chain": "base", "wallet": "trading", "to": "0x0000000000000000000000000000000000000002", "amount": "0.001"}, "native")
```

## 交易执行与故障处理

- 每个 RPC 使用前验证 Chain ID。读请求有限重试并可切换端点；发送只尝试一次。
- 预览不预留 Nonce，执行时重新读取和模拟。Gas 默认估算值乘 1.2 并向上取整。
- 支持 `--gas-price-wei`，或 `--max-fee-per-gas-wei` 和 `--max-priority-fee-per-gas-wei`；两类费用互斥。
- 模拟失败默认停止。显式绕过必须同时提供 `--skip-simulation --gas 300000 --reason '具体原因'`。仍检查地址、Chain ID 和余额。
- 费用仅估算 EVM 执行费，不包含链专用 L1/数据附加费用。
- 按链和钱包使用 POSIX 文件锁，协调本工具包跨进程发送；不协调外部钱包。
- `.state/transactions.sqlite3` 只存 unsigned 交易参数、哈希和状态。不要删除记录来解决未知广播；先查询原哈希。
- Nonce 同时参考 RPC pending 与本地已签名交易记录。RPC 传播延迟不会自动复用本地已使用 Nonce；交易丢弃可能留下空档，需要查询并显式处理原交易。
- `BROADCAST_UNKNOWN` 时先用 `transaction inspect` 查询返回的哈希；需要重播时用同链同钱包的 `transaction replay --hash ... --execute`。它只重播哈希一致的原签名交易。
- `CONFIRMATION_TIMEOUT` 代表未达到确认数，不代表链上失败。`TRANSACTION_FAILED` 代表回执明确失败。
- `accelerate`、`cancel` 只处理选定钱包的 pending 交易，默认至少提高原费用约 13%；节点规则可能要求更高费用，可显式覆盖。替换成功不保证原交易未先上链。
- `approve set --zero-first` 第一步等待确认后再执行第二步；预览时第二步标记为依赖前一步，不假装已在清零后的状态模拟。

这是同系统用户可读取 `.env` 的本地密钥方案，未引入独立签名服务或进程隔离。公开 calldata、模板、消息和签名仍可能包含业务敏感内容，工具不会将它们全部隐藏。

## 简单批量与 Hermes

```bash
python batch.py run --file examples/batch.json
python batch.py run --file examples/batch.json --execute
```

批次按顺序执行；默认预览，执行时写交易逐笔等待确认。默认失败停止，可用 `--continue-on-error` 继续独立项。广播状态未知或确认超时后，即使选择继续，也停止该钱包同链后续写入。不自动重跑整个批次、不提供调度和断点恢复。

将 `skills/evm-wallet` 复制到 Hermes 的 skill 目录，或配置 Hermes 扫描项目内 skill 目录；在使用时告知工具包绝对路径。Skill 不自动写入全局配置。详见 [Skill](../SKILL.md)。

## 发布范围

发布目录仅包含运行代码、配置模板、用法示例和 Agent 文档，不附带开发测试代码、夹具或测试指南。

当前不含 MCP、智能账户写入、部署、资产/授权全量索引、桥、市场、调度和任务恢复；只读内部调用分析属于可选增强能力。完整设计记录见 [plan.MD](../../../plan.MD)。

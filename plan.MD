# Hermes Agent 自托管 EVM 钱包工具包实施计划

> 本文件为历史设计记录。发布目录已移除测试代码、夹具、测试指南和开发测试依赖；下文测试部分仅记录原设计，不代表发布版附带这些文件。

## 1. 目标与架构

实现面向 Hermes Agent 的本地 EOA 钱包工具包，支持多钱包、多链、资产查询、交易解析、转账、授权、合约交互、NFT、签名和简单批量调用。

- 每个功能模块为可独立运行的 Python 文件，同时支持 Python 导入调用。
- 配置、RPC、ABI、交易引擎和 JSON 协议由共享核心库复用。
- Hermes 通过终端调用脚本，交付项目内 Skill；首版不做 MCP。
- 默认预览与模拟，显式 `--execute` 才签名广播，消息签名采用相同原则。
- 不设置交互式确认或独立人工审批服务。
- 使用目标机器上满足依赖的 Python 3.12 及以上解释器，不要求 Conda 或特定环境名称。本机验证基线为 Python 3.12、Web3.py 7.14.0、eth-account 0.13.7；运行依赖锁定版本。
- 私钥本地加载、本地签名，不发送给 RPC。

## 2. 模块设计

| 文件 | 能力 |
|---|---|
| `chain.py` | 链列表、RPC/数据源能力探测、Chain ID 验证、区块和费用查询 |
| `address.py` | 浏览器普通交易历史、方向/目标过滤、游标分页和近期直接调用候选汇总 |
| `wallet.py` | 钱包 ID/标签、观察钱包、地址校验、原生币与 ERC-20 余额及汇总 |
| `token.py` | ERC-20 名称、符号、精度、供应量和单位转换 |
| `transaction.py` | 详情、回执、状态、确认数、Nonce、Gas、解析、模板、加速、取消和同笔重播 |
| `contract.py` | ABI 加载、函数列表、编码解码、只读调用和任意合约写入 |
| `transfer.py` | 原生币和 ERC-20 转账 |
| `approve.py` | allowance 查询、精确额度/无限额度授权和撤销 |
| `nft.py` | ERC-721/1155 查询、转账、授权、撤销和自定义 Mint |
| `events.py` | 分段读取、过滤、解码和去重事件日志 |
| `raw.py` | calldata/函数签名编解码、模板校验、重新编码、原始调用与写入 |
| `signing.py` | EIP-191、EIP-712 签名、验签与地址恢复 |
| `batch.py` | JSON 列表顺序执行和逐项结果汇总 |

### 2.1 Chain、Wallet、Token

- 每条链配置多个按优先级排序的 HTTP/HTTPS RPC，每个端点使用前验证 Chain ID。
- 支持 EIP-1559 和 legacy 费用方式，配置可扩展普通 EVM 链。
- 费用查询同时输出每 Gas 的 wei/gwei 字符串，预览包含同格式的 fee_summary；原始签名交易和费用输入仍使用 wei，禁止将裸数推断为 gwei。
- 钱包支持稳定 ID、显示标签和仅地址观察钱包；标签重复时拒绝模糊选择。
- 多个可签名钱包派生出同一地址时返回 DUPLICATE_SIGNING_ADDRESS 告警和关联钱包 ID，不把标签视为资金隔离；观察钱包别名不当作重复私钥。
- 配置错误指出文件和私钥变量名但不回显值；地址错误细分类型、长度、字符、EIP-55 校验和。
- 余额汇总的 wallets 接受 JSON 数组或逗号分隔字符串；CLI 的 balances 支持 --wallet 别名，保留 JSON/Python 的 wallets 规范字段。
- 查询原生币和指定 ERC-20；余额汇总接受钱包与代币列表，不自动索引全部资产。
- ERC-20 元数据方法独立处理，缺失时返回部分数据与警告。
- 精度未知不猜测，提供最小单位整数或显式精度。
- 使用十进制字符串及整数处理金额，拒绝浮点数与超精度输入。

### 2.2 Contract、Transfer、Approve

- ABI 支持数组和包含 `abi` 字段的 artifact；函数重载通过完整签名选择。
- 支持内置 erc20、erc721、erc1155、weth、erc4626 ABI；contract.call 的标准 ERC-20 查询可省略外部 ABI，自定义合约继续提供明确 ABI，set/get 文件仅为教学示例。
- 支持 tuple、数组、bytes、payable 金额及指定区块只读调用。
- 合约接口校验代码存在；任意写入统一使用共享交易引擎。
- 转账支持明确金额或最小单位整数，两者互斥。
- 无限授权显式选择，默认不无限授权。
- 先清零再授权为显式两步操作，第一步确认后再执行第二步，各自保留结果。
- ERC-20 模拟返回 false 时拒绝，兼容无返回值代币；不保证扣税类代币的实际到账量。

### 2.3 NFT、Events、Signing

- ERC-721 查询余额、ownerOf、URI、单枚授权和 operator 授权。
- ERC-1155 查询指定 ID/批量余额、URI 和 operator 授权。
- 支持标准安全转账，ERC-1155 支持批量转账。
- Mint 接受指定 ABI 或完整函数签名、参数和金额；证明或签名数据由调用者提供。
- 不假设存在通用 mint，不自动枚举全部 NFT，不默认请求外部 metadata URI。
- 日志按区块分段查询，支持 topics 和 ABI 解码；范围受限时缩小分段。
- 日志按区块哈希、交易哈希、序号去重，查询不完整时明确失败并保留完成区间。
- 默认读取达到配置确认数的区块，读取更新区块需显式选择并提示重组可能性。
- EIP-191 普通消息和 EIP-712 结构化数据默认展示预览，显式执行才返回签名。
- EIP-712 声明 Chain ID 时与选定链校验；提供验签和地址恢复，不自动提交 permit。

## 3. 交易解析与 Raw 调用模板

提供“参考交易 → 提取原始数据 → 导出模板 → 修改参数 → 模拟 → 显式执行”的流程。

### 3.1 解析

- 必须输入链和交易哈希，不能仅凭哈希可靠识别链。
- inspect 提取发送者、目标、原生币金额、完整 input/calldata、选择器、Nonce、Gas、费用、回执和事件。
- pending 交易返回已有信息并标记回执缺失；不把未找到回执视为失败。
- decode 自动按标准 ABI、已验证 ABI、4byte 顺序识别；用户 ABI/完整函数签名优先。歧义返回全部候选，不能当作确定参数。
- ABI 不匹配保留原始数据并报错，不把选择器候选当成确定语义。
- 基础查询不依赖区块浏览器；只读分析层可配置 Blockscout、Sourcify V2、Etherscan V2、4byte 及 RPC trace，失败保留基础结果。

### 3.2 模板

- 导出版本化 JSON，包含 schema_version、来源链/哈希/发送者/状态、to、value_wei、data、可选解码信息和警告。
- 不复制原签名，不将原 Nonce、Gas 或费用作为新交易执行参数。
- 合约创建交易可查看，但不导出可执行调用模板。
- 输出文件不覆盖已存在文件；stdout 返回结果信封，文件保存原始模板对象。
- raw 可读取模板、查看、模拟和执行；执行明确选择自己的链和钱包。
- 执行链与模板链一致，目标和金额只接受显式覆盖。
- 修改已知参数使用 reencode 重新编码；解码参数与 data 不一致时拒绝执行。
- 不自动替换 calldata 中的地址片段或未知字节。
- 提醒检查原接收者、过期时间、钱包绑定签名/白名单证明及当前状态；历史成功不保证新调用成功。

## 4. 配置与统一接口

- `.env` 只保存多个钱包私钥，TOML 钱包配置引用私钥环境变量名。
- RPC 使用独立 CSV，列为 chain、name、url、priority、enabled；同一链多行，按优先级及行顺序切换，支持禁用端点。
- 钱包配置包含 id、label、key_env 或观察地址；配置地址与派生地址同时存在时验证一致。
- TOML 链包含别名、Chain ID、原生币符号和精度、确认数与费用模式；[rpc] 指定相对 TOML 目录或绝对路径的 CSV 文件。
- 进程环境优先；只读取明确指定或项目根目录 `.env`，不向上搜索。
- 提供 Ethereum、Base、Arbitrum、Optimism、Polygon、BSC、Sepolia 示例，CSV 提供每条链的主备行，默认禁用并留空 URL。
- 私钥不进入参数、日志、输出或异常；RPC 健康检查只显示端点名称，CSV 本地文件加入忽略规则，分发无凭证示例。
- 同系统用户可读取 `.env`；不引入独立签名服务和系统隔离。

### 公共协议

- 每个文件支持子命令、`--help`、`--input-json FILE|-`。
- JSON 请求为 operation、params；不能与业务 CLI 参数混用。
- 标准输出只返回一个 JSON，成功退出 0，失败非零。
- 信封包含 schema_version、ok、operation、chain_id、wallet、data、warnings、error。
- 错误包含稳定 code、message、retryable；不输出未脱敏的异常文本。
- 链上金额、大整数和 Token ID 使用字符串，结构字段使用整数。
- 读操作可指定地址，写操作明确指定链和钱包。
- Python 调用通过 Toolkit.run 或入口模块 run 获得同样结果。

### Hermes Skill

说明模块选择、调用方式、钱包标签、预览/执行、错误处理、模板流程与批量结果。
Skill 保留在项目内，用户决定安装位置，不自动修改全局 Hermes 配置。

## 5. 交易引擎与失败处理

1. 解析并验证链、钱包、地址、金额、参数。
2. 验证 RPC 实际 Chain ID，构建调用数据。
3. 查询 pending Nonce，估算 Gas 和费用，检查相关余额。
4. eth_call 模拟，解析标准或 ABI 已知 revert。
5. 默认返回预览，存在显式执行参数时本地签名广播。
6. 返回哈希，可等待配置确认数。

- 预览不保留 Nonce；执行时重新查询和模拟。
- Gas 默认估算值乘 1.2 后向上取整，允许显式覆盖。
- EIP-1559 与 legacy 参数互斥；费用范围明确为执行费，不承诺包含 L2 附加费。
- 模拟/估算失败默认停止；绕过需显式 skip_simulation、gas、reason，仍检查地址、Chain ID 和余额。
- 按 Chain ID + 钱包地址使用跨进程锁，协调本工具包发送，不协调外部钱包。
- SQLite 保存交易意图、Nonce、哈希和广播状态，不保存私钥或签名原文。
- Nonce 参考 pending 与本地记录，避免节点传播延迟时复用已使用 Nonce；丢弃交易需显式处理。
- 读请求可有限重试，写请求仅发送一次。超时后保留原哈希，不换 Nonce 重建业务交易。
- 重播只允许重建哈希一致的同一笔签名交易。
- 等待超时为未确认，回执 status=0 才报告已知链上失败。
- 加速保持业务内容和 Nonce，提高费用；取消为同 Nonce 向自身发送零金额交易，不能保证原交易未先执行。

## 6. 简单批量

- JSON 列表，每项包含唯一 id、module、operation、params。
- 只调用允许的工具能力，不接受任意 Python 或 shell。
- 顺序执行，默认整批预览；显式执行才逐项发送。
- 写交易发送前重新模拟，并等待配置确认数后继续。
- 默认失败/确认超时停止，输出已完成、失败和未执行项。
- continue_on_error 可继续独立项目；广播状态不明时停止该钱包同链后续写入。
- 不自动重试整批，不提供定时任务、后台服务或任务恢复。
- SQLite 仅作为交易记录，不扩展为持久化任务队列。

## 7. 测试与验收

离线行为测试使用标准库 unittest，并兼容 pytest 收集。真实本地 EVM 使用 eth-tester/Py-EVM 和预编译简化协议夹具，无需在线编译。缺少本地 EVM 依赖时明确跳过，不算通过。

- 13 个入口独立运行；CLI/JSON 行为一致，输出及退出码符合协议。
- 标签歧义、缺失配置、地址不一致、观察钱包写入、敏感信息脱敏。
- Chain ID 校验、RPC 故障切换、费用模式、精度与金额边界。
- 默认预览不签名广播、显式执行、模拟失败及显式绕过。
- ERC-20 余额、授权、无返回值、false 返回值和先清零授权。
- Nonce 协调、广播超时、同笔重播、回执失败、确认超时及替换交易。
- ABI 重载、动态参数、tuple、自定义错误与原始交互。
- 交易模板的无 ABI、错误 ABI、pending、空数据、创建交易、跨链和参数一致性。
- NFT 常用查询、授权、转账、自定义 Mint；两类消息签名验签。
- 日志分段去重及批量停止/继续策略。
- 自动测试仅使用测试密钥、本地链或 Mock，不发送主网交易。

## 8. 实施顺序、交付与边界

实施顺序：配置/协议 → RPC/只读 → ABI/交易解析/模板 → 交易引擎/转账/授权/NFT → 签名/事件/批量 → 测试和文档。

交付 13 个入口文件、共享核心、依赖锁、配置模板、示例、中文 README、Hermes Skill、测试和本计划文件。

首版支持本地 EOA、普通 EVM JSON-RPC。暂不包括 MCP、智能账户写入、合约部署、全量资产/授权索引、桥、交易市场、协议专用流程、常驻调度和持久化任务恢复。


## 9. 自动交易分析增强

- 新增 transaction.analyze，保留 inspect 基础查询；decode、事件读取和模板共享分析能力。分析 CLI 不加载私钥，不签名广播。
- 标准 selector/事件表涵盖 ERC-20、721、1155、WETH、ERC-4626。重名布局和参数语义分别处理，4byte 多候选并列，用户显式 ABI 优先。
- Blockscout 按链配置，Sourcify V2 与 Etherscan V2 依次可选补充，4byte 最后兜底。公开设置与本地 API Key 文件分离，后者加入 Git 忽略，.env 仍只保存私钥。支持 no-external、缓存、限流与失败降级。
- 自动金额注释保留 raw，按日志地址或 vault 底层资产/份额查询精度；NFT ID 不做小数换算，元数据缺失不猜 18 位。
- counterpart 区分链上名称、源码名称、验证状态、代理及实现；浏览器当前信息不冒充历史事实，同区块升级需结合 trace。
- 内部调用优先 callTracer，支持回滚传播、DELEGATECALL 执行上下文和常见代理/7702 标记。Blockscout 内部交易仅作为可能不完整的摘要。
- 余额分别报告交易级原生币 prestate diff、ERC-20 事件流量、区块级历史余额差；后两者不宣称单笔精确余额变化，不静默改查 latest。本轮不含 fork 回放。
- 业务分类附证据与生效状态，识别无限 ERC-20 授权、明确未验证 spender、验证未知与 NFT 全局授权；不依据名称或已验证标识保证安全。
- 默认分析预算 60 秒/300 请求、1000 帧/日志、64 层、10 页，历史查询最多 20 个地址和 20 个代币；触限时受影响组件标记不完整，只有核心或严格模式必需项失败才使总状态 partial。基础交易数据和所有原始日志保留。
- 回归仅在临时目录运行，不把开发测试文件、真实交易材料或私钥提交到发布目录。详细接口与使用说明维护在 Skill 的 analysis.md 参考中。


## 10. 分析准确性与能力诊断修订

- 默认 standard 模式：输入/事件歧义或未解析、规范链问题等进入 warnings 并影响 analysis_status；浏览器、4byte、trace、元数据和历史余额缺失进入结构化 enhancements_missing。complete 只表示核心成功。
- 新增 --require-trace 严格模式：成功且未截断的 callTracer 和交易级 prestate diff 成为必需项；与 --no-trace 互斥。其他增强仍可缺失。
- is_value_transfer 仅在正数 value、允许的调用类型且已生效时为 true；may_carry_value 单独表达类型能力。摘要缺少生效证据时正数转移为未知，回滚/零值/DELEGATECALL 不误报。
- 同时返回 sender/counterpart，区分 EOA、合约、EIP-7702 委托 EOA；本地区块末证据、当前浏览器实现和交易 trace 证据分别注明范围。
- spender 风险附创建者、创建交易、代码/账户类型及浏览器普通交易计数；未知值不当作 false，不以 nonce 判断历史活动，不因缺创建者而自动提高风险。
- Sourcify V2 按链开启（发行配置 BSC 默认 true），可设全局/单链 URL。运行时代码匹配才提供权威 ABI；只有创建匹配或 404 不等于当前运行时代码已验证。跨源合并以任一明确已验证优先，保留各来源证据。
- 全 input 严格 UTF-8 且可打印才展示文本（最多 4096 字符，原始数据保留）。非预编译地址在所查区块无代码时分类 onchain_message/data_only；有代码或创建交易只提示文本候选，明确 ABI 优先。文本不作为 Agent 指令。
- chain.probe --explorer 对每个 RPC 和已配置来源进行有预算的实时只读探测；可传 --hash，否则最多搜索最近五个区块的真实交易。区分不支持、未配置、鉴权、限流、超时、历史不可用和未测试；不输出 URL/Key、不缓存健康结果。
- 历史分析依赖 debug_traceTransaction 的 callTracer/prestateTracer diff；debug_traceCall 独立探测模拟能力，不是历史分析前提。供应商套餐和历史范围需核对，不承诺免费档开放 debug。
- CSV 维持五列，增加默认禁用的 BSC trace/archive 提示行；全部 Markdown 与 Agent 参考文档同步。回归材料在临时目录验证，不纳入发布文件。


## 11. DeFi 路径金额、池子事件与请求复用

- 内置 uniswap_v2_router 九种常见 swap；已确定 ABI 的 amountIn/amountInMax 绑定 address[] path 首项，amountOut/amountOutMin 绑定末项，保留精确/最高/最低角色、raw 与绑定证据。自定义 ABI 仅按明确类型/名称约定，4byte 候选不做确定绑定；原生币与包装代币分别表达。
- 增加 dex_v2_pair 的 Swap/Mint/Burn/Sync；增加 Uniswap/Pancake V3 的 Swap/Mint/Burn/Collect/Flash/Initialize，分别维护不同 Swap 布局。池子金额通过历史 token0/token1 解析，V3 signed delta 为池子视角，储备/流动性不混为转账，不重复累计 Transfer 流量。WETH/ERC-4626 保持各自规则。
- 分析 Chain ID 每轮每端点首次校验，故障切换新端点重验；同轮复用历史调用成功结果和池子元数据。核心解析和必需 trace 优先，为结束区块校验预留最多两次请求；余额输出单独覆盖统计和 omitted_pairs。
- 默认 max_requests=300，时间维持 60 秒；保留用户显式配置/CLI 覆盖。新增 requests_by_stage，计数包含增强阶段实际 HTTP 请求，不含基础 inspect。
- 用户 BSC 单跳样本在 100/200/300/500 各档冷缓存为 34 次请求、热缓存 33 次，9/9 组历史余额完成；默认时限另测 7.24 秒。公共节点不支持两类 trace，因此不宣称全能力完成；细分数据和限制见 analysis.md。
- 同步全部五份 Markdown，开发回归与真实交易材料仅保留临时目录。此次不包含 V3 packed path、Universal Router 或任意聚合器指令流解析。


## 12. Agent 资金结论与复现工作流（九项）

- 增加 fund_flows，默认 from，可指定 focus_address；ERC-20/NFT 独立解析标准日志，不依赖项目 ABI。原生币 trace 与 prestate 证据分开，退款未知为 null，费用范围明确，metadata 缺失不丢地址/原始数。
- 新增 --view summary/full；默认 full，summary 的完整 CLI JSON 信封含换行 <=2048 UTF-8 字节，明确裁剪计数、风险和未知项，不带 inspection/calldata。
- 新增 address.py history/actions，复用配置的 Blockscout/Etherscan，50 笔默认窗口、最多 200、过滤/游标/来源和完整性明确；不新增索引服务或无限扫块。
- 增加 inferred_intent 及证据/置信度；资金流规则不替代确定函数含义，包装币按链配置地址识别，授权模式不混淆 ERC-20 与 NFT。
- 部分结果 issues/data.suggested_next 与真正错误 error.suggested_next 提供结构化只读后续动作，不编造签名，不自动广播。
- 日志地址/识别资产 metadata 去重预取，20 个 eth_call 一批，乱序按 ID 匹配，单项失败隔离；批量不支持回退、鉴权/限流不盲目展开请求。
- transaction.plan 合并解析/内存模板/参数修改/模拟估费，标准受益人改为自己并列 diff；未知参数不猜改，证明/owner/deadline 不自动重写。永不接受 execute/skip_simulation，实时查询，不签名广播。
- 扩展本地缓存为分析快照与按区块哈希隔离的 metadata；完整增强 300 秒、部分 60 秒，pending/重组不存快照；no-cache 强制刷新，plan 的当前状态不缓存。
- analyze/plan/history/actions 增加静态 --describe，不依赖配置或网络。13 个入口、全部 Markdown 与 Skill 同步。gulltoppr 独立后续处理，未纳入本轮。
- 实测缓存摘要 1398 字节并验证零网络，冷分析 34 HTTP/37 RPC。公共节点这次历史读取返回鉴权/权限拒绝且不支持 trace，公共历史 API 也返回鉴权错误，均如实返回，不宣称能力已完整；开发测试/真实交易材料不加入仓库。

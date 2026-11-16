# Agent EVM Wallet

让你的 Agent 使用本地自托管钱包，完成多链余额查询、转账、授权、合约交互、NFT 操作和交易解析。

## 1. 交给 Agent 接入

将项目放到 Agent 能访问的目录，确保它具备本地文件和终端执行能力，然后发送：

> 工具包位于 `<项目绝对路径>`。请读取 `skills/evm-wallet/SKILL.md`，按其中说明检查运行环境、安装缺少的依赖并完成配置。缺少参数时询问我，不要覆盖已有钱包配置，也不要在初始化时发送交易。

如果 Agent 支持安装 Skill，可让它安装 `skills/evm-wallet`。工具包本体仍需保留，并告知 Agent 所在路径。

## 2. 准备钱包和 RPC

- `.env`：仅保存钱包私钥，在本地填写，无需发送到 Agent 对话中。
- `config/rpc.csv`：填写 RPC，可为同一条链配置多个备用端点。
- `config/settings.toml`：配置钱包标签和链信息，可交给 Agent 协助设置。

不清楚如何填写时，让 Agent 根据模板指导你；它应通过工具读取钱包公开地址，不展示私钥。

多个钱包共用私钥时会明确告警：不同标签不代表资金已隔离。配置有误时，错误会提示应检查的文件和变量。

## 3. 用自然语言下达任务

例如：

> 查询 trading 钱包在 Base 上的原生币余额。

> 查询这个 ERC-20 合约中我的代币余额，使用内置 ERC-20 接口，无需另备 ABI 文件。

> 使用 trading 钱包，在我指定的链上向这个地址转账。先向我确认缺少的地址、金额等参数，再给出预览。

> 用摘要分析这笔交易，告诉我付出什么、得到什么、费用和未知项；需要细节再展开。

> 查询这个地址最近 50 笔交易，汇总它调用过的目标和 selector。

> 参考这笔交易，用 trading 钱包生成复现预览，展示参数变化、模拟和估费，先不要执行。

> 这笔交易函数没解出来，请展开完整内部 input；如果是委托流程，提取业务调用再预览，检查过期参数和疑似找零。

摘要会控制在 2KB 内；完整分析保留原始证据。内部原始 calldata 需让 Agent 显式打开 include-inputs；未知参数只在有 ABI 或明确字段位置时改写，且必须模拟。分析结果可本地缓存，需要最新结果时让 Agent 强制刷新。未知函数仍可提供标准日志资金流，但缺少 trace、精度或其他证据时会明确标注未知。地址历史需配置可用的 Blockscout/Etherscan 服务，权限以供应商计划为准。

工具默认预览。需要实际转账或写入合约时，明确告诉 Agent 执行范围；参数完整且已授权后，Agent 会执行并返回交易结果。

费用查询同时标明 wei 和 gwei，Agent 应按返回单位读取；详细命令和参数见 Skill。

命令、接口和故障处理由 Agent 按 [Skill](skills/evm-wallet/SKILL.md) 中的参考文档读取。

可选配置 `config/explorers.toml`，通过 Blockscout、Sourcify、Etherscan 获取 ABI 和身份信息；BSC 默认开启 Sourcify。API Key 放本地 `config/explorers.local.toml`，不要放进 `.env`。

需要完整内部调用和交易级原生币变化时，RPC 应支持 `debug_traceTransaction` 的 callTracer 与 prestateTracer diff；公共节点未必开放。可告诉 Agent：“请探测 BSC 的 RPC 和浏览器能力”，它会运行 `chain.py probe --chain bsc --explorer`。供应商权限和历史范围详见 Skill，不要求 `debug_traceCall` 才能分析历史交易。

支持常见 V2 swap 的路径金额绑定，以及 V2/V3 池子的 Swap、Mint/Burn 等事件；会区分“至少输出”和实际成交。可直接告诉 Agent：“分析这笔 swap 的输入、最低输出和池子事件”。

标准分析的 `complete` 表示核心输入与事件已解析；可选能力缺失另列 `enhancements_missing`。需要严格完整追踪时，让 Agent 使用 `--require-trace`。

有疑问或建议可以添加我的联系方式
微信：Firefly_bit
X:https://x.com/driftmousie

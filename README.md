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

## 3. 用自然语言下达任务

例如：

> 查询 trading 钱包在 Base 上的原生币余额。

> 使用 trading 钱包，在我指定的链上向这个地址转账。先向我确认缺少的地址、金额等参数，再给出预览。

> 分析这笔交易，导出可复用的调用模板，先不要执行。

工具默认预览。需要实际转账或写入合约时，明确告诉 Agent 执行范围；参数完整且已授权后，Agent 会执行并返回交易结果。

命令、接口和故障处理由 Agent 按 [Skill](skills/evm-wallet/SKILL.md) 中的参考文档读取。

有疑问或建议可以添加我的联系方式
微信：Firefly_bit
X:https://x.com/driftmousie

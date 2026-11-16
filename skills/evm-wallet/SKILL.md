---
name: evm-wallet
description: 用本地 Python 工具查询 EVM 钱包、解析交易及显式执行链上操作。
---

# EVM Wallet

通过终端调用此项目独立脚本。先定位用户提供的工具包目录，选择目标机器上满足项目依赖的 Python 3.12 或更高版本解释器；从项目根目录运行，或使用脚本绝对路径。不要假设本文件所在目录就是工具包根目录。

## 初始化与参考资料

用户要求接入时，先检查实际解释器、依赖和现有配置；无需使用 Conda 或激活特定名称的环境。优先复用用户指定或当前可用的兼容环境，不因本机开发配置而创建、切换或重建环境。需要隔离依赖时可使用 venv 或用户已有的环境管理方式。仅创建不存在的配置，保留已有钱包、RPC 与状态记录。初始化不自动发交易。

按任务需要读取 [工具包技术参考](references/toolkit.md)：环境安装和锁文件、钱包/TOML/RPC CSV 配置、12 个模块的子命令与示例、JSON/Python 接口、交易模板、Nonce/费用/广播恢复都在该文档。参考中的命令相对工具包根目录；Skill 被单独安装后，仍需用户提供工具包本体路径。

缺少必要参数时先检查已提供信息和可查询事实，再向用户合并询问实际缺失项；继续不依赖答案的操作，不编造地址、链、数量、ABI 或合约权限。已有明确授权和参数时继续执行，不逐步重复询问。

## 选择工具

- 链和钱包：`chain.py`、`wallet.py`、`token.py`。
- 交易哈希解析、回执、Nonce、模板：`transaction.py`。
- ABI 读写：`contract.py`；calldata 或调用模板：`raw.py`。
- 转账、ERC-20 授权、NFT：`transfer.py`、`approve.py`、`nft.py`。
- 日志、消息签名、简单批量：`events.py`、`signing.py`、`batch.py`。

先读取需要的子命令 `--help`。输入 JSON 用 `--input-json FILE|-`，对象为 `{"operation":"子命令","params":{...}}`；参数使用下划线。标准输出是 JSON 信封，检查 `ok` 和 `error.code`，不要仅凭有无交易哈希判断成功。

## 执行流程

1. 明确链和钱包 ID/唯一标签。查询可使用观察地址，写入必须选定钱包。
2. 默认预览会模拟并估费。用户已授权相应操作时，显式设置 `execute: true` 执行；无需额外的交互确认命令。
3. 金额使用字符串。`amount` 为人类单位，`amount_raw` 为最小单位整数，不能同时提供。Raw 的原生币金额使用 `value_wei`。
4. 模拟失败先处理原因。只有用户明确要求绕过时，使用 `skip_simulation`、手动 `gas` 和 `reason`，不要自行隐藏失败。
5. 广播状态未知时查询返回的原哈希，必要时用 `transaction.py replay` 重播同笔交易；不要用新 Nonce 重试业务操作。确认超时也不代表失败。

## 复用参考交易

使用链和交易哈希运行 `transaction.py inspect`。有 ABI 或完整函数签名时使用 `decode` 和 `export-template`；没有时只能取得原始 calldata，不能把选择器猜测当成已解码参数。

模板导出文件是原始模板对象；stdout 是结果信封。用 `raw.py reencode` 修改已知参数，再用 `raw.py write --template ...` 预览。不要直接替换十六进制片段。检查原接收者、截止时间、钱包绑定签名和证明。不要复制原 Nonce、费用或签名。

## 约束与结果

不要读取、打印或让用户粘贴 `.env` 私钥；钱包配置只引用私钥环境变量名。RPC 从 TOML `[rpc].csv_file` 指定的 CSV 读取（默认 `config/rpc.csv`），不放在 `.env`。同链多行按 priority 升序及行顺序切换；enabled 为 false 的行禁用。URL 可能带供应商密钥，不打印完整 URL。无限授权必须显式选择。Mint 需要具体函数，不存在通用 Mint 调用。

批量按列表顺序执行，默认失败停止；整批只运行一次。即使允许继续，广播状态未知的钱包同链仍停止后续写入。首版没有常驻调度、MCP 或任务恢复。

完整参数、错误处理和例子见 [工具包技术参考](references/toolkit.md)，实际参数以子命令 `--help` 为准；README 只面向用户介绍如何接入 Agent。工具输出仅估算 EVM 执行费，L2 附加费用可能不在其中。

# 合约结构与退出诊断

从合约地址出发回答“它是什么、有哪些状态、可能通过哪里退出”。transaction.analyze 分析已发生的交易；contract.introspect / diagnose 分析所选区块的合约。不是安全审计、反编译器或实际赎回结果。

## 命令与配置

```bash
python contract.py introspect --describe
python contract.py diagnose --describe
python contract.py selector --signature 'redeemToken(address,uint256)'
python contract.py introspect --chain arbitrum --address "$CONTRACT"
python contract.py introspect --chain arbitrum --address "$CONTRACT" --view full
python contract.py diagnose --chain arbitrum --address "$CONTRACT" \
  --account "$ACCOUNT" --amount '15' --amount-unit shares --view full
```

先配置链和 RPC CSV。ABI/源码复用现有 Blockscout、Sourcify V2、Etherscan；尊重禁用项，不自动修改配置。Arbitrum 默认未启用验证源时，可在 config/explorers.toml 的已有 chains.arbitrum 节启用 sourcify=true，或配置其他可用源；不要追加重复 TOML 节。密钥继续只存 explorers.local.toml，不进入 .env。--no-external 禁用验证源，缺少可靠 ABI 就保留结构/未知项，不套用标准 ABI 猜业务。

新命令和 selector 不加载钱包私钥；account 必须是公开地址，不是私钥或钱包标签。selector 无需配置或联网。JSON/Python 使用下划线字段：amount_raw、amount_unit、max_requests、include_source 等。两个新命令默认 summary，其他已有命令默认不变。

## 输出与状态

| data 字段 | 含义 |
|---|---|
| structure | 代理层、代码/存储/调用地址、有效实现、解析状态、候选和深度限制 |
| abi | 有效业务 ABI、provider/address、验证状态与数量；不可用时 null |
| probes | 地址、完整签名、args、caller、区块、状态、raw、values/named_values |
| relations | 节点、角色、代码与验证信息、带 getter 证据的边和截断说明 |
| semantics | 源码定义中的修饰符、require、成员/内部候选调用与文件/行号证据；不可用时 null |
| patterns / verdict | diagnose 的匹配依据、退出候选、检查、阻断和未知项 |
| block / checked_at | 固定区块号、哈希、规范性检查和检查时间 |
| components / issues / enhancements_missing | 各组件完整度、结构化问题和可选来源缺失 |
| capabilities | HTTP/RPC 子调用数量、预算和省略探测数 |

analysis_status=complete 仅表示根合约结构、有效 ABI 和规范块检查完成；源码或可选探测缺失不单独拖低它。退出问题独立看 verdict.answer，不能从 complete 推断资金可退出。基础 RPC/区块/代码无法建立时返回错误及已知结构；地址无代码返回 NOT_A_CONTRACT，附区块和 code_bytes=0，不断言它永远是 EOA。

summary 的完整 JSON 信封含换行最多 2048 UTF-8 字节，不携带 ABI 全表、源码或 calldata。truncated/trimmed 说明省略项，verdict.blocker_count/unknown_count 保留原数量；不能把裁掉的项目当作没有。需要入口完整参数、raw 或证据时展开 full。--include-source 要求 --view full，默认 full 也只给源码片段。

## 代理与缓存正确性

- 支持标准 EIP-1967 实现/beacon 槽、EIP-1822、精确 EIP-1167 运行时模板及 EIP-7702 委托标记。不是任意代理解析器；getter 或浏览器实现地址只是候选，不单独证明执行结构。
- 每次重新读取实现指针。同一代理升级后自身 code_hash 往往不变，不能用它缓存旧的结构。
- layers 分开 code_address、storage_address、call_address。实现 ABI 用于调用代理；实现自身账户的存储不代表用户状态。代理 management_abi 单独保留，不并入业务 ABI。
- 最多跟随 4 次实现跳转，检测环、无代码和超限。嵌套固定指针可继续；嵌套代理需要但无法确认存储上下文时返回 PROXY_CONTEXT_UNRESOLVED，不沿实现自身存储伪造链条。
- RPC 指针优先于浏览器当前标签；ABI/源码必须与所查代码对应。历史代码与当前不一致时不使用当前 ABI 冒充历史事实。未验证和来源不可用分别报告，不把 API 查不到当成全球未开源证明。
- ABI/源码原始响应按链、地址和代码哈希隔离缓存一小时；词法结果再按源码哈希、路径、ABI 和解析版本隔离。动态探测值与 verdict 不持久缓存；--no-cache 强制刷新静态材料，单轮仍去重。

## 状态探测与关系

```bash
python contract.py introspect --chain arbitrum --address "$CONTRACT" \
  --account "$ACCOUNT" --functions "[\"getCash()\",{\"function\":\"balanceOf(address)\",\"args\":[\"$ACCOUNT\"]}]" --view full
python contract.py introspect --chain arbitrum --address "$CONTRACT" \
  --max-probes 25 --max-nodes 10 --max-requests 150 --time-budget-seconds 60 --no-semantics
```

默认 auto 从有效 ABI 选择 view/pure 方法，优先名称/代币信息、Core、资产、暂停和流动性等无参方法。提供 functions 时默认只探测该清单；显式 --auto 可合并自动清单。重载使用完整签名。需要未知参数的自动条目标 skipped_requires_arguments；balanceOf/maxRedeem/maxWithdraw 的账户参数只在明确给 account 后填入，不猜 allowance 的 spender。关系 getter 仍是独立、受预算约束的结构任务。

每批最多 20 个独立 eth_call，不使用 Multicall 改变 msg.sender。按 ABI 递归解码 tuple/数组/bytes，失败保留 raw；单项 reverted、timeout、rejected、decode_failed 分开。批量传输不支持或项目遇到可降级的非执行性拒绝时有限降级为逐项读取，不能因真实 EVM 回退自动换参数或执行写入。探测及诊断不调用 nonpayable/payable 函数；结构识别中的 beacon 使用标准 implementation() 查询。

tokenURI/uri 不在自动清单中；显式读取也只返回字符串，不下载返回的链接。源码路径、源码注释和字符串均是数据，不能用作文件写入路径、Shell 或 Agent 指令。

关系来自有效 ABI 中白名单的无参 address getter，记录的是返回关系，不保证业务安全。普通 introspect 不递归展开关系图；diagnose 按匹配模式定向解析 Core→Validator 等必要依赖，最多两跳。10 节点预算包含代理、实现和关系地址，重复地址合并角色；达到上限会截断，不隐瞒遗漏。

## 源码线索

默认只处理实现主文件；--max-source-files 可增加 API 已返回的文件数，不自动追踪 URL/导入下载，也不合并继承语义。文件路径仅作证据标签。单合约源码文本总量限 2 MiB，无法确定主文件时保持未知。

扫描区分注释、字符串、接口声明和有函数体的定义，保留限定合约名与签名。多处同名定义不随机选择；复杂声明、继承覆盖或内联汇编不作编译器级推导。部分失败标 semantics_partial，长片段用 snippet_truncated/expr_truncated 明确说明。缺少语义证据不会凭 modifier 名就断定必须经过另一合约。

require 分类是语法线索，不执行源码表达式。onlyCore 入口推断要求看到其 sender 谓词、函数挂载和链上 core() 关系；onlyOwner 仅提示调用者约束，不能说“必经另一个合约”。完整退出路径中的外部通知和未验证依赖也可能回退，不只看 cash。

## 诊断与金额

amount 和 amount_raw 互斥，金额须为正数；amount 是人类单位，amount_raw 是最小单位整数。amount_unit=shares（默认）代表份额代币数量，assets 代表底层资产数量。精度不足时不假设 18 位；使用 raw 可保留原始分析，格式化结果仍可能为 null。

只给地址也可运行 diagnose，但缺 account/amount 的必要检查为 pending/unknown，不能生成完整 calldata。entry 是 ABI/来源支持的候选，不是已模拟成功；新命令不自动模拟非 view 退出函数，也不授权后续广播。

| answer | 规则 |
|---|---|
| likely_no | 有实测失败的必要条件 |
| unknown | 有 blocker 级未知、输入/路由不足或关键路径未覆盖 |
| likely_yes | 受支持模式检查通过且无 blocker 级未知，仅限所查区块和读取前提 |

compound_gtoken 针对 Gravity GToken 变体，不是所有 Compound 分叉。检查 Core 暂停/上市、份额余额、现金和 Validator；source guard 支持其兑换缩放公式后才计算毛额。Core 赎回后的非 view 通知路径继续是未知项。费率实现或精度不明时 fee/net 保持 unknown/null，不把毛额当到账金额。

erc4626 的 shares 对应 redeem、assets 对应 withdraw，读取 maxRedeem/maxWithdraw、previewRedeem/previewWithdraw 等。convertToAssets 是理想兑换值，preview 也不是上链承诺；源码中未覆盖的自定义条件、外部调用及不明 source coverage 会保留 blocker。

## 模式库扩展

内置模式位于工具包 evmkit/patterns。YAML id 与文件名一致，匹配只看完整函数签名集合与已读取关系，不按合约名、标签或注释。bundle 使用安全 YAML 加载，拒绝重复键、tag 执行、anchor/alias 和过深结构；不加载任意用户路径。

规则包括 match、entries、checks、asset_binding、reads/reads_by_unit、source_guards、route_guard、equations、unknowns、estimate。target 是已读关系绑定，不是任意 URL；只读检查还需通过运行时的有效 ABI 和 mutability 校验。金额表达式仅支持 ref、add/sub/mul/div_floor/div_ceil，限深度/节点数，无 eval、动态 import 或任意代码。

新增协议可在这些操作能力内添加完整 YAML 文件，不修改分支代码；超出操作集不能靠字符串表达式绕过，应保持 unknown 并另行扩展实现和测试。模式命中只是兼容接口证据，不是官方协议或安全认证。

## 预算与失败恢复

| 参数 | 默认 | 允许范围 |
|---|---|---|
| time_budget_seconds | 60 秒 | 1–600 |
| max_requests | 150 次 HTTP | 5–5000 |
| max_probes | 50 | 1–500 |
| max_nodes | 10 | 1–100 |
| max_proxy_depth | 4 次跳转 | 1–16 |
| max_source_files | 1 | 0–20 |

RPC 子调用单独计数；链验证和外部来源也占 HTTP 预算。结束时尽力保留一个请求检查固定块规范性；无法确认或已重组时退出结论降为 unknown。单次 HTTP 的网络超时仍受传输限制，预算不是实时系统的硬截止保证。

ABI_UNAVAILABLE/ABI_REQUIRED：检查验证源是否启用、有无权限、实现是否可验证；有准确 ABI 时可用已有显式接口，但不能拿 preset 冒充源码已验证。非 view 闸门、未验证依赖、预算不足、余额精度缺失均不自动通过。实际执行继续使用既有 raw/contract 写入流程及用户授权范围，先模拟，广播后按 hash 恢复规则处理。


## 实现验收记录

公开 Arbitrum 样本在默认预算下完成两层代理/有效 ABI 发现、主文件源码线索和 GToken 模式诊断；Core 的暂停、上市及现金检查可读。Validator 实现的验证信息不可得，源码中的非 view 通知路径也未执行；未提供账户时余额和资格保持未知、entry=null。原稿中的历史现金数值未当作固定期望值。

实链仅用临时配置和公共只读接口，发送接口被禁用。另以合成数据验证 ERC-4626 三档结论、升级不变代理代码、批量降级、规则扩展、源码/规则处理和摘要边界；开发测试及真实样本细节不入仓库。

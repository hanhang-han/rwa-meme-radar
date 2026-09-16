# RWA Meme Radar — 股票相关 Meme 关系雷达

> Discover, verify, and register meme-token ↔ stock-token pairs on X Layer.
> 把「meme 币蹭股票」的模糊叙事，变成可核验、可复现、上链存证的配对证据。

**在线体验**：https://cliperx.com/dashboard/ （X Layer 主网数据实时更新）

## 这是什么

每逢热点，链上会涌现大量「蹭」股票的 meme 币：名字带 NVDA、/logo 像特斯拉/宣称 1:1 锚定腾讯股票。但「谁真的和股票代币有链上关系，谁只是碰瓷」此前没有任何工具能回答。

RWA Meme Radar 用链上证据回答这个问题：

1. **发现** — 通过 OKX DEX API 扫描 X Layer 上 meme 币与 xStocks（tokenized stocks）的共享流动性池
2. **核验** — 链上 `eth_call` 直读池合约的 `token0/token1` 与代币 `underlying()` 映射，核验双方身份，产出 `verified / invalid` 状态（而非仅名称匹配）
3. **登记** — 核验通过的配对由 PairRegistry 合约登记上 **X Layer 主网**，证据 JSON 的 keccak256 哈希永久存证，任何人可重算验证
4. **定价** — 对比链上 DEX 成交价与真实股价（EODHD），输出「链上价 vs 股价偏离」，偏离本身就是市场对叙事的定价

## X Layer 链上集成（Build a Market）

| | |
|---|---|
| **合约** | `PairRegistry` @ `0x64920bBdD9645C4B4b8bBd38b6A6D4f29822e737` |
| **网络** | X Layer 主网（chainId 196） |
| **浏览器** | [OKX Explorer](https://www.okx.com/explorer/xlayer/address/0x64920bBdD9645C4B4b8bBd38b6A6D4f29822e737) |
| **源码** | [`contracts/PairRegistry.sol`](contracts/PairRegistry.sol) |
| **已登记配对** | 60+ 组（700 腾讯、QQQ、GME、SPY、GLD、TSLA、AMD 等 20+ 股票代码） |

合约设计：owner（雷达服务钱包）才能写入，任何人可读。每条登记包含 meme 地址、stock 地址、ticker、以及**核验证据 JSON 的 keccak256 哈希**——公开 API 会同时返回证据原文， skeptical 用户可以自行 `keccak256(evidenceJson)` 对比链上哈希。

### 链上数据公开 API

```bash
curl https://cliperx.com/dashboard/api/registry
```

```json
{
  "contract": "0x64920bBdD9645C4B4b8bBd38b6A6D4f29822e737",
  "chainId": 196,
  "onchainCount": 62,
  "pairs": [{
    "meme": "0xbe17...", "stock": "0xfa15...", "ticker": "700",
    "evidence": "0x7a40ddfe...",
    "evidenceSource": "{\"block\":70770418,\"chainId\":\"196\",\"pool\":\"0x0818...\",...}"
  }]
}
```

## 功能模块

| 模块 | 内容 |
|---|---|
| **首页** | AI 简报（变化导向：新蹭名线索 / 新核验配对 / 涨跌异动 / 跨市场溢价）+ 核心指标 |
| **MEME 雷达** | 全部候选 meme 币：价格/成交/流动性/持有人，按核验状态与篮子分组，同名合约展开对照 |
| **股票雷达** | 反向视角：每只 xStock 挂着哪些 meme，链上价 vs 股价偏离排行 |
| **交易池分析** | 已核验配对的证据页：池地址、核验区块、流动性分布、双合约、链上登记状态 |
| **事件历史** | 配对维度的发现/价格异动事件流（谁 ↔ 谁） |

## 技术栈

- **前端**：原生 JavaScript + ECharts，零框架零构建，单页 hash 路由，中英双语
- **后端**：Node 22 + Hono + TypeScript（tsx 直跑），`node:sqlite` 持久化研究事实与采样
- **链上**：ethers v6 + solc-js 编译部署；X Layer RPC `https://xlayerrpc.okx.com`
- **数据源**（全部公共端点）：OKX DEX API（行情/池/交易）、EODHD（股价）、DeepSeek（AI 简报，Anthropic 兼容接口）
- **部署**：nginx 反代 + pm2，https://cliperx.com/dashboard/

## 快速开始

```bash
npm install
npm start          # http://localhost:3456 ，零配置（公共数据源无需 key）
npm test           # 端到端 UI 测试（jsdom 模拟真实脚本加载顺序）
npm run demo       # 数据层验证脚本
```

AI 简报为可选功能：`.env` 中配置 `DEEPSEEK_API_KEY` 后自动启用（见 `.env.example`）。合约登记需要 `REGISTRY_CONTRACT` + `REGISTRY_OWNER_KEY`，不配置时系统正常运行、仅跳过上链。

重新编译合约：

```bash
npx tsx scripts/deploy-registry.ts --compile-only   # 输出 contracts/artifacts/PairRegistry.json
```

## 数据可信度设计

- **证据优先**：每个配对结论都附带池地址、核验区块号、时间戳，页面一键跳浏览器复核
- **口径标注**：成交额标注 DEX 口径；流动性分档（可交易 / 仅观察 / 流动性过低）；上游脏值（>1e10 USD）自动过滤
- **名称线索不入链**：只有共享池核验通过的配对才上链登记；名称相似仅作为待核线索展示
- **AI 只描述不预测**：简报系统提示词硬约束——只解读输入字段、不给买卖建议；英文版数据层剔除无法核实的非 ASCII 符号

## 开发期内完成的主要工作（OKX Dev Day 2026）

- 多链适配层 + OKX DEX 数据接入（主数据源切换，配额自适应）
- 关系核验引擎：共享池发现 → 链上身份核验 → 状态机
- PairRegistry 合约设计、部署与自动化登记（X Layer 主网）
- 链上价 vs 股价偏离：EODHD 股价对齐 + 新鲜度窗口
- AI 简报四版迭代（结构化/证据措辞/连续追踪/双语一致性）
- 证据优先的信息架构重构与中英双语化

## License

MIT

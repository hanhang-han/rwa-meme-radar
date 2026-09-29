# 实时行情与免费数据源交接（2026-09-29）

## 已上线

- 浏览中的池通过 `candle-watch` 优先处理链上日志；同池按原顺序处理，队列中冷池仍有轮转机会。图表、最新成交价和逐笔列表现在对应同一个池，资产整体报价另列。
- 实时交易与检查点使用独立 SQLite 连接；跨进程写锁忙等待缩为 250 ms，并在本进程写锁外重试。主采集、流动性刷新、发现与维护任务在实时队列达到 32 条时暂缓，最长 180 秒，恢复后错峰。
- 前端 SSE/逐笔和原生池 K 线保持真实成交驱动；没有新成交时不制造跳价。上线后在 Robinhood 活跃池观察 45 秒，所选池价格与 K 线各变化 5 次，页面无报错。
- 生产文件与 Git `488257b` 的后端源文件一致；后端 407 项测试、前端 89 项测试和浏览器实时回归通过。

## 免费上游的边界

| 用途 | 当前/备选 | 结论 |
| --- | --- | --- |
| X Layer 日志 | [官方 WSS](https://web3.okx.com/zh-hans/onchainos/dev-docs/xlayer/developer/websockets-endpoints/websocket-endpoints) | 已使用；仍须按持久游标断线补采。|
| BNB Chain 日志 | 现用 PublicNode WSS；备选 [NodeReal BNB 免费账户](https://nodereal.io/api-marketplace/bsc-rpc) | BNB 官方公共 HTTP [禁用 `eth_getLogs`](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)。NodeReal 支持 HTTPS/WSS，但其[额度文档](https://docs.nodereal.io/docs/pricing-plan)与产品页不一致，以账户控制台为准。|
| Robinhood Chain 日志 | 现用 PublicNode WSS；备选 [Alchemy 免费账户](https://docs.robinhood.com/chain/connecting/) | 官方公共 RPC 明示不适合生产高吞吐；[Alchemy 免费档](https://www.alchemy.com/pricing)当前为 30M CU/月、25 req/s。|
| Robinhood Stock Token 报价 | [官方 `/prices/{symbol}`](https://docs.robinhood.com/chain/stock-token-apis/) | 已按接口的 15 秒缓存间隔采集。仅覆盖对应的 Stock Tokens，不能视为全市场股票行情。|
| 全市场股票原市场实时报价 | 无已核实的免费公开再分发源 | [IEX 实时 TOPS](https://www.iex.io/resources/trading/fee-schedule)收费；[Alpaca](https://alpaca.markets/support/redistribute-alpaca-api)明确禁止再分发。未取得许可前不向公开页面或 API 转发未授权行情。|

现有 `BSC_WS_URLS`、`ROBINHOOD_WS_URLS` 支持逗号分隔的多个 WebSocket 地址，断线后会轮换。接入 NodeReal/Alchemy 需要各自账户控制台生成的只读 WSS 地址；凭据只放服务器环境变量，不提交 Git。单凭免费公共节点不能承诺所有池和所有股票都恒定数秒刷新。

## 尚未补齐与验收

2026-09-29 22:33 北京时间，数据健康接口仍为 `degraded`：X Layer 实时队列 0，BNB 0，Robinhood 3，但 BNB 与 Robinhood 的历史扫描缺口分别约 23.3 万和 231.7 万块，近端扫描也可能在突发输入时落后。2988 个股票代币中只有 277 个有参考股价，其中 82 个独立参考、195 个发行方参考；其余 2711 个缺少已核实参考。候选资产 36 个、股票代币 10 个没有可验证的市场报价，不能把空值填成零。

后续验收应分开看：① 打开的活跃池是否在真实成交后数秒内更新价格、K 线和同池逐笔；② 全链队列、断线恢复和近端扫描是否持续推进；③ 历史扫描缺口是否下降；④ 每个报价是否标明市场、币种、来源和实际时间；⑤ 无权再分发的原市场股票报价仍显示缺失或延迟，不伪装为实时。

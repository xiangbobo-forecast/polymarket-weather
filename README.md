# polymarket-weather：Polymarket「最高温」纸面交易记录器

**这是纸面交易（模拟），不是真钱交易。** 仓库里没有钱包代码、没有私钥、不下任何真实订单，运行不需要任何密钥，费用为 0。

## 它做什么

GitHub Actions 每 3 小时（UTC 每隔 3 小时的第 17 分钟）自动跑一次 `tracker.py`：

1. **抓市场**：从 Polymarket Gamma API（标签 `103040` Daily Temperature）拉出所有未关闭的「Highest temperature in …」事件，解析每个档位（美国 2°F 一档，其他地方 1°C 一档）和描述里写明的结算机场 ICAO 代码。
2. **算模型概率**：在结算机场坐标（来自 aviationweather.gov stationinfo）上取 Open-Meteo 集合预报（GFS、ECMWF IFS、ICON 的全部成员，三个模型各占 1/3 权重），按机场当地日期取逐小时最高温，然后：
   - 偏差修正：最近 3 个完整日「METAR 实测最高温 − 集合平均预报」，打 5 折，限制在 ±3°C；
   - 离散放大：成员离平均的偏离 ×1.2，再加高斯核（0.8°C + 每提前一天 0.3°C）；
   - 当天：已观测到的最高温（只算例行 METAR，不算 SPECI）作为下限；剩余小时的预报用「最新实测 − 当时预报」做一半权重的即时修正；
   - 按结算规则取整（.5 进位；华氏市场先换算再取整）得到每档概率。
3. **看价格**：读 CLOB 订单簿，YES 和 NO 的最优卖价。手续费按官方公式 `股数 × 0.01 × p × (1−p)`（只收吃单方）。
4. **记模拟单**：某档（YES 或 NO）满足「模型概率 −（成交均价 + 每股手续费）≥ 0.08」且卖价在 0.05–0.95 之间，才记一笔：每笔 $2（按订单簿逐档模拟成交），每天（UTC）新开仓合计最多 $10，每个档位最多一笔，不重复进场。
5. **每次都记快照**：每个档位的模型概率、买一/卖一、中间价，存到 `data/snapshots/日期/时间.csv.gz`，用来给「模型 vs 市场」打分。
6. **结算**：Gamma 显示事件已结算后，算模拟单盈亏；同时用结算前最后一次快照（以及第一次快照）算模型和市场的 Brier 分数、对数损失。
7. 把结果提交回本仓库，并自动生成中文 `REPORT.md`。

## 怎么看 REPORT.md

- **总览**：模拟单总数、未结算/已结算、盈亏（已扣手续费）、收益率、胜率。
- **模型 vs 市场**：这是最重要的一张表。Brier 和对数损失都是**越小越好**。如果「模型」一栏长期比「市场」差，说明模型没有优势，纸面盈利多半是运气。至少攒几百个已结算事件再下结论。
- **分城市**：每个城市的打分和模拟盈亏，可以看出模型在哪些城市特别差（比如沿海、海雾、山地机场）。
- **最近一次运行**：扫了多少事件、新增几笔、跳过了哪些、为什么。

原始数据：`data/trades.csv`（所有模拟单）、`data/summary.json`（汇总，机器可读）、`data/state.json`（每个事件的第一次/最后一次概率和结算结果）、`data/snapshots/`（每次扫描的全部档位）。

## 改参数

全部在 `config.json`：

| 参数 | 含义 | 默认 |
|---|---|---|
| `min_edge` | 扣费后最小优势 | 0.08 |
| `min_ask` / `max_ask` | 只在这个价格区间记单 | 0.05 / 0.95 |
| `stake_usd` | 每笔金额 | 2 |
| `max_new_stake_per_day_usd` | 每天新开仓上限 | 10 |
| `fee_rate` | 手续费率（市场自带费率时以市场为准） | 0.01 |
| `ensemble_models` | Open-Meteo 集合模型 | gfs025, ecmwf_ifs025, icon_seamless |
| `spread_inflation` | 成员离散放大倍数 | 1.2 |
| `kernel_sd_base_c` / `kernel_sd_per_lead_day_c` | 高斯核宽度 | 0.8 / 0.3 |
| `bias_shrink` / `bias_clip_c` / `past_days_for_bias` | 站点偏差修正 | 0.5 / 3 / 3 |
| `nowcast_weight` / `nowcast_clip_c` | 当天即时修正 | 0.5 / 5 |
| `skip_cities` | 跳过的城市 | Hong Kong |
| `openmeteo_chunk` | 每次请求 Open-Meteo 的地点数 | 8 |

改完提交即可，下一次定时运行生效。要立刻跑：仓库 → Actions → paper-tracker → Run workflow。

本地运行：`python3 tracker.py`（只用 Python 标准库，3.9+）。

## 维护说明（给以后接手的人）

- **只读公开接口**：Gamma（`gamma-api.polymarket.com`）、CLOB（`clob.polymarket.com/books`，需要带浏览器式 User-Agent，否则 403）、aviationweather.gov（METAR 一次最多返回 400 行，所以按站点逐个请求）、Open-Meteo 集合预报（免费额度按 IP 计算，GitHub Actions 的 IP 是共用的，可能偶尔被限流；出错时该批城市本次跳过并写进报告，不会中断）。
- **结算规则会变**。目前（2026-11）规则：只算例行 METAR（SPECI 不算），优先用备注 T 组（0.1°C），没有就用正文整数度；按机场当地日期；.5 进位；华氏先换算再取整。如果 Polymarket 改规则，主要改 `fetch_metar_max`、`bucket_bounds_c`。
- **香港默认跳过**：香港用天文台「绝对最高气温」（0.1°C 精度，不是 METAR），档位含义和取整不同，没做。描述里找不到 ICAO 的事件也会跳过并写进报告。
- 已过当地日期、等结算的事件不再建模、不再记单（那时结果基本已知，只剩结算延迟）。
- 数据量：每次快照约 20–30 KB（gz），一天 8 次。`state.json` 只保留 30 天内或有未结算单的事件。仓库太大时可以把旧的 `data/snapshots/` 打包挪走。
- Actions 用 `concurrency` 防止两次运行同时写数据，推送前会 `pull --rebase` 重试 3 次。公开仓库的 Actions 分钟数免费。
- **不要往这个仓库加私钥或钱包代码。** 这个领域有很多偷私钥的假机器人仓库，任何要求在 `.env` 填私钥的东西都别引进来。

## 参考与致谢

思路参考了 [jattree/weather-edge](https://github.com/jattree/weather-edge)（MIT 许可）：按结算机场 METAR 建模、记录价格并用「模型 vs 市场」打分来检验有没有优势（该项目的公开结论是市场概率比模型更准）。本仓库代码为独立编写，没有复制其他仓库的代码。

许可证：MIT。

# 港股数据契约v1

证券代码统一为5位字符串，市场固定XHKG，币种HKD，成交量为股、成交额为港元。必须同时保留provider、adjustment、volume_unit和amount_unit。绝不调用A股6位补零或成交量乘100逻辑。

每个快照包含`manifest.json`、`bars.parquet`、`calendar.csv`和逐证券原始返回。清单保存供应商、下载时间、请求范围、版本、每个文件SHA256。失败状态的快照不能进入研究；改动过的文件不能被加载。下载时间不是历史发布时间。

日线列：date、symbol、open、high、low、close、volume、amount、market、currency、volume_unit、amount_unit、adjustment、provider。date为香港本地交易日期；calendar中的open/close为带时区UTC时间。半日市使用真实会话关闭时间。

规范禁止缺失、重复、负数成交量、非有限价格或OHLC关系异常。当前研究要求配置观察名单在全部请求交易日都有有效行情；缺失不填补、零量不默认可交易。未来动态股票池需要上市、退市、停牌和PIT规则主表，再扩展当前验证器。

`corporate_actions_complete=false`和`pit_universe=false`是当前采集器的真实能力边界。任何后续认证必须增加真实历史数据和相应测试，不能只改布尔值。

新浪接口返回的历史数据存在局部质量问题，源数据会被校验，不通过时拒绝入库。东方财富接口需要显式选择，不能作为静默兜底。接口文档：https://akshare.akfamily.xyz/data/stock/stock.html

## Exploratory standard/v2 export

A clean Git checkout additionally publishes a `standard/v2` research profile through the pinned quant-lab adapter. It is always `investable=false` and `rankable=false`. A dirty or unavailable checkout retains the original report without claiming a clean code revision. Dataset identities use content hashes, and date-only NAV observations are stamped at the end of their UTC day. This profile does not certify a historical universe or replace the original accounting evidence.

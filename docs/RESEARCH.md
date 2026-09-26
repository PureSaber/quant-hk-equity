# 首次真实数据研究记录

数据核查日：2026-09-26。请求区间：2025-07-01至2026-09-25。新浪港股日线5只证券、307个交易日、合计1535行，全部通过格式、单位、价格关系和XHKG交易日覆盖检查。

## 数据源与排除记录

- 新浪`stock_hk_daily(adjust='')`可访问，作为本轮唯一显式行情源。
- 东方财富`stock_hk_hist`连接被关闭；Yahoo返回限流。没有改写来源或伪造成功。
- 原候选01299（AIA）在2026-09-04返回open=78.5、high=78.8、low=78.05、close=77.5。close低于low，整批六证券采集被拒绝。随后建立五证券研究配置，明确排除AIA；没有把该源异常改成正常行情。
- 最终观察名单为00005、00700、00939、00941、09988。当前整手数及应税属性已对照港交所证券列表核查，但尚无完整历史有效记录，因此作为恒定规则情景而非PIT事实。
- 原始行情下载保留在本地，不在GitHub再分发。清单与聚合研究结论可公开审查。

## 费用来源

- [港交所交易费用](https://www.hkex.com.hk/Services/Rules-and-Forms-and-Fees/Fees/Securities-%28Hong-Kong%29/Trading/Transaction?sc_lang=en)：印花税0.1%双向、向上取整至港元；SFC0.0027%、AFRC0.00015%、交易费0.00565%。
- [HKSCC2025交收费通告](https://www.hkex.com.hk/-/media/HKEX-Market/Services/Circulars-and-Notices/Participant-and-Members-Circulars/HKSCC/2025/ce_HKSCC_SET_022_2025.pdf)：2025-06-30起交收费0.0042%，取消最低和最高限额。
- [港交所结算说明](https://www.hkex.com.hk/services/settlement-and-depository/settlement?sc_lang=en)：香港交易所买卖的交收周期为T+2。首版仅用交易日代理交收日，明确限制认证范围。
- [当前证券列表](https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx)：00005为400股、00700为100股、00939为1000股、00941为500股、09988为100股，每只均标注需缴印花税。

配置中的佣金0.03%（最低3HKD）、平台费0和滑点0.05%是研究假设，不代表实际券商收费；可通过新配置运行成本敏感性分析。当前费率覆盖区间被显式限定，历史或未来超出区间必须提供新政策。

## 评价方式

训练区间为2025-08-01至2025-12-31，留出区间为2026-01-02至2026-09-25。候选仅有20日动量、20日低波动，按训练Sharpe选定后再评价留出期。报告同时提供同池等权和双倍成本结果。

这是回顾性探索，没有前瞻预注册；没有完整分红/供股/拆并股、PIT股票池及停牌/退市证据。所有结果都是`investable=false`的**未复权价格净收益**，不能据此宣传策略有效或可投资超额收益。

最终可复核摘要存放于`evidence/`；完整订单、账本、信号和HTML报告由上述命令生成在本地`outputs/`。

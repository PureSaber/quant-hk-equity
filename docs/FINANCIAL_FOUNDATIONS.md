# 02–05 港股 v2 研究配置

旧 quant-hk-study/v1 不改含义。v2 要求 settlement_calendar_scope="evidenced_purpose_calendar" 和 financial 对象，包含 calendars、settlement_calendar_id、status、lifecycle、actions（明确空数组也可以）；可选 universe_id。

calendars 使用 QDK PurposeCalendar 字段，必须 purpose="settlement"。每日按当时可见版本刷新交收开日，禁止交易日日历偷偷代替银行/CCASS 交收日。lifecycle 过滤新配置，status 买卖分侧、缺失/过期/冲突阻断订单。拆股/现金分红权益构造信号总收益，执行继续使用原始价格与独立分红到账。

`actions.replay_actions(account, records, through=...)` 支持 QExec 的复杂 HKD 行动，行权额受已交收现金约束。需与账户行情、交易按时间交织，目标先注册。复杂转换不进入现有价格信号 study；该路径显式报错，不能忽略行动后继续标记总收益研究成功。

study中的简单行动在`effective_at`和`available_at`都到达后才处理，分红付款必须等待同一证券、币种和权益日的entitlement。迟到payment可以在可得后结清已登记应收；迟到entitlement、split及其他需要历史持仓数量的非payment行动会明确失败，因为当前QExec接口不能用历史权益日持仓重建数量。样本开始前已生效的已知entitlement不会授予样本内新买持仓。

`return_basis`在v1为`price_only_excludes_corporate_actions`，在v2为`price_plus_evidenced_corporate_actions`。标准成本列是HKD金额，`run_manifest.json`以`tags.cost_unit=currency`声明单位。

v2 仍为固定候选名单、固定整手数场景；输入真实完整性及 CCASS 认证不能靠 schema 证明，investable 始终 False。报告按 v1/v2 分别展示边界。tests/test_research.py 中 v2 回归展示完整最小配置与 unknown 阻断。

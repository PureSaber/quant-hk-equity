# 02–05 港股 v2 研究配置

旧 quant-hk-study/v1 不改含义。v2 要求 settlement_calendar_scope="evidenced_purpose_calendar" 和 financial 对象，包含 calendars、settlement_calendar_id、status、lifecycle、actions（明确空数组也可以）；可选 universe_id。

calendars 使用 QDK PurposeCalendar 字段，必须 purpose="settlement"。每日按当时可见版本刷新交收开日，禁止交易日日历偷偷代替银行/CCASS 交收日。lifecycle 过滤新配置，status 买卖分侧、缺失/过期/冲突阻断订单。拆股/现金分红权益构造信号总收益，执行继续使用原始价格与独立分红到账。

`actions.replay_actions(account, records, through=...)` 支持 QExec 的复杂 HKD 行动，行权额受已交收现金约束。需与账户行情、交易按时间交织，目标先注册。复杂转换不进入现有价格信号 study；该路径显式报错，不能忽略行动后继续标记总收益研究成功。

study中的简单行动在`effective_at`和`available_at`都到达后才处理，分红付款必须等待同一证券、币种和权益日的entitlement。迟到payment可以在可得后结清已登记应收；迟到entitlement、split及其他需要历史持仓数量的非payment行动会明确失败，因为当前QExec接口不能用历史权益日持仓重建数量。样本开始前已生效的已知entitlement不会授予样本内新买持仓。

`return_basis`在v1为`price_only_excludes_corporate_actions`，在v2为`price_plus_evidenced_corporate_actions`。标准成本列是HKD金额，`run_manifest.json`以`tags.cost_unit=currency`声明单位。

v2 仍为固定候选名单、固定整手数场景；输入真实完整性及 CCASS 认证不能靠 schema 证明，investable 始终 False。报告按 v1/v2 分别展示边界。tests/test_research.py 中 v2 回归展示完整最小配置与 unknown 阻断。

# 分红场景适配器

`quant-hk-dividend-scenario/v1`是软件场景覆盖层，不改变既有v1/v2研究含义。输入中的每个生命周期都必须有唯一`dividend_id`、规范化QDK记录指纹及一条`hk-research`账户的权益日持仓依据。`source_record_sha256`必须等于`DividendLifecycle.fingerprint()`，等价于对`lifecycle.to_json().encode("utf-8")`计算SHA-256；它证明规范化输入记录未变，不认证`source_reference`指向的公告、发布者或发布时间。权益条款的`effective_at`和`available_at`都不能晚于依据的`ex_at`；依据自身必须在`ex_at`可得且在场景`as_of`前完成捕获。适配器不以当前持仓补算账户开户前的权益。

时间线以UTC时点排序。同一时点只接受能形成唯一拓扑顺序的内生关系或显式`same_instant_order`证据：开盘价先于调仓、收盘价先于交收、同一分红按权益→发行人换汇→付款推进，最终估值在该时点的其他事件之后。调用方输入的事实身份共享一个全局命名空间：生命周期阶段和PIT汇率使用`event_id`，权益依据、到账状态和顺序依据使用各自的`evidence_id`；底层来源引用及`dividend_id`、`policy_id`不属于这个事件身份命名空间。每条显式顺序证据必须引用同一时点组中真实存在的两个不同事件；孤立、跨时点、自环和重复证据均失败。若公司行动与市场事件的经济顺序不能唯一确定，场景失败，不用事件名称或输入数组顺序猜测。

QExec每阶段只接收当时已知且该阶段实际使用的前缀。`record.applied_at`必须与已排定时点一致；子阶段早于父阶段会在调用前被拒绝。付款政策缺失不阻断精确权益应收的建立，但税务状态保持`unknown`；实际付款阶段仍要求满足QDK/QExec付款契约。发行人换汇和账户估值汇率是不同证据，外币现金不会扩充HKD可用交易现金。

发行人换汇先以原换汇时点的合法已知前缀调用QExec。若金额可在账本科目精度内精确表示，迟到的付款政策不会推迟换汇；只有QExec明确返回`ROUNDING_POLICY_REQUIRED`时，规划器才为该分红纳入实际所需的舍入政策并从新账户重演。每轮至少确定一个此前未确定的`dividend_id`，同一ID不能重复加入，因此终止上界为生命周期数加一次成功执行。丢弃的规划运行不写文件，最终`timeline.jsonl`每个分红只保留一次实际换汇事件；其他QExec校验错误直接失败。

实际付款一旦在`as_of`前成为已知到账事实，付款所需的选择、换汇和政策也必须在`as_of`前生效并可得。同一账户、分红和`as_of`不能同时声明实际到账与`not_received`。未来才生效的政策不能让适配器忽略已经到账的现金后仍发布`complete_as_of`；这些事实矛盾在构建时间线前失败。

`returns.csv`只覆盖实际收盘收益区间。`result.json`分别记录`daily_return_end`、`account_as_of`与`price_coverage_end`，因此最后一个收盘后的付款能改变`as_of_nav_hkd`，不会伪造一条日收益或虚构零基准收益。

发布协议为QExec清单优先、顶层场景清单最后。失败可能保留已经完整发布的`execution/manifest.json`并写`FAILED.json`，但不会发布顶层成功清单。消费方必须调用磁盘重放验证器；仅核对文件哈希不足以证明旁车、结果与执行账本一致。

场景运行时把实际使用的依赖API绑定到分发RECORD及可用摘要。editable依赖除要求干净Git提交和跟踪路径外，还直接读取对应HEAD blob与实际导入源码比较；只接受Python源码LF与CRLF的换行等价，不依赖可能被`assume-unchanged`或`skip-worktree`隐藏的状态与diff结果，也不执行任意clean filter来改变比较语义。

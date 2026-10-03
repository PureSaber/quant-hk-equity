# quant-hk-equity

香港证券市场日频研究仓：显式港股数据源、滞后一日信号、整手交易、双边费用、现金交收约束、训练/留出期划分和可核验账本。

首版及现有真实样本是**探索性价格收益研究**。真实行情已接入，完整公司行动与历史业务规则尚未完成真实验收，不构成可投资回测认证。研究范围是当前观察名单，不是历史全市场股票池。`quant-hk-study/v2`配置另外支持输入驱动的分红、拆股、生命周期、交易状态和分用途交收日历；代码支持不代表现有真实样本已经补齐这些证据。

[首次真实研究与验收证据](evidence/20260926/README.md)：5只证券、307个交易日，独立安装与双版本CI通过，训练/留出期、基准和成本情景已运行。

## 安装与运行

Python3.11+，在虚拟环境内运行。`requirements.lock`固定外部依赖，`stack.lock`固定共享仓库提交。内部仓库使用经过本项目测试的提交组合，显式覆盖它们各自旧发布版的Git依赖固定值。

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps -r stack.lock
python -m pip install -e . --no-deps
quant-hk fetch --config configs/baseline.json --snapshot data/hk-snapshot
quant-hk preflight --config configs/baseline.json --snapshot data/hk-snapshot
quant-hk run --config configs/baseline.json --snapshot data/hk-snapshot --output outputs/baseline
```

快照和结果目录必须不存在。重新研究使用新目录；重复使用原目录会失败，防止覆盖证据。供应商失败会留下失败清单，不能自动改源或填充价格。原始供应商返回、标准行情、日历和哈希保存在本地`data/`，不发布到GitHub。

## 实现与边界

|组件|责任|
|---|---|
|quant-data-kit.hong_kong|独立港股代码、原始日线、单位验证、XHKG日历、不可变快照|
|quant-factors|复用20日动量和波动率|
|quant-execution.hong_kong|FixedPoint成交和费用、ExactAccountLedger平衡账本、整手与现金约束|
|quant-hk-equity|观察名单、因果信号、候选研究、留出期比较与HTML报告|
|quant-lab|保留standard/v1研究产物；在干净Git检出下另导出standard/v2的research适配视图，供统一校验与索引，保持不可投资、不可排名|

- 候选固定为20日动量、20日低波动；5个交易日调仓一次，上一交易日信号在次日开盘参考价执行。
- 训练期只按Sharpe选择候选，并先写入`selection.json`；留出期独立从现金开始，不用于调参。另有同池等权基准和双倍成本情景。
- 费用使用按日期限定的政策；印花税双向收取并向上取整至港元，其他费用分别舍入至港仙。佣金和滑点是显式研究假设，不代表券商报价。
- 当日买入允许当日卖出；卖出款在第二个交收日收盘释放。当前例子明确使用XHKG交易日代理交收日历，不能替代CCASS历史认证。
- 固定整手数来自核查日的港交所资料，历史运行中属于情景参数，不能冒充历史有效主表。数据缺口会终止研究。
- 日频参考价模型不实现竞价撮合、分价档、碎股、卖空或真实交易。通用QExec入口会拒绝将XHKG证券套入A股规则。
- 默认v1配置及已有真实案例输出价格收益；v2配置按所给分红/拆股权益构造信号总收益，现金到账单独记账，复杂行动仍受限制。两种配置的报告均为`investable=false`。

## 标准产物消费

干净源码检出生成的各研究子运行可通过`quant-lab validate --run-dir <子运行目录>`核验，再扫描到统一实验索引。`standard/v2`的`profile=research`包含净值、持仓、账户快照与暴露适配视图，明确标记`investable=false`、`rankable=false`，不等同于完整执行账本认证。源检出不干净或Git身份不可用时，不声称干净提交的v2身份，原研究报告继续保留。

Report Hub从索引定位产物后重新核对标准清单与哈希；损坏的v2不能降级采用v1。原始港股成交、交收和现金证据仍以应用自己的账本与报告为准。此接入不改变M8发行覆盖或市场数据GA状态。

v2保留全部原始绩效，并通过`backtest_stats`提供累计收益、252期年化收益、零无风险利率Sharpe和回撤；`measurement_basis`保留实际区间、HKD、来源及价格/公司行动收益口径。训练、留出、基准与双倍成本各自发布，不能跨区间直接排名。原配置随`study_config`保存，不重新计算或替换原研究结果。

## 验证

```sh
python -m ruff check src tests
python -m pytest -q
```

验证覆盖未来数据扰动不改变此前信号、交易日缺口、成本与现金约束、费用舍入、同日卖出、交收、幂等、账本平衡、冻结输入和训练/测试隔离。

见[研究记录](docs/RESEARCH.md)、[实施验收](docs/IMPLEMENTATION.md)及[数据契约](docs/DATA_CONTRACT.md)。

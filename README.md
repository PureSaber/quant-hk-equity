# quant-hk-equity

香港证券市场日频研究仓：显式港股数据源、滞后一日信号、整手交易、双边费用、现金交收约束、训练/留出期划分和可核验账本。

首版是**探索性价格收益研究**。真实行情已接入，但不包含分红、供股、拆并股等完整公司行动，不构成总收益或可投资回测认证。研究范围是当前观察名单，不是历史全市场股票池。

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
|quant-lab.contracts|标准研究产物与文件哈希；使用现有standard/v1接口，未声称通过standard/v2认证|

- 候选固定为20日动量、20日低波动；5个交易日调仓一次，上一交易日信号在次日开盘参考价执行。
- 训练期只按Sharpe选择候选，并先写入`selection.json`；留出期独立从现金开始，不用于调参。另有同池等权基准和双倍成本情景。
- 费用使用按日期限定的政策；印花税双向收取并向上取整至港元，其他费用分别舍入至港仙。佣金和滑点是显式研究假设，不代表券商报价。
- 当日买入允许当日卖出；卖出款在第二个交收日收盘释放。当前例子明确使用XHKG交易日代理交收日历，不能替代CCASS历史认证。
- 固定整手数来自核查日的港交所资料，历史运行中属于情景参数，不能冒充历史有效主表。数据缺口会终止研究。
- 日频参考价模型不实现竞价撮合、分价档、碎股、卖空或真实交易。通用QExec入口会拒绝将XHKG证券套入A股规则。
- 当前只输出未复权价格收益，不处理分红/拆股。报告始终为`investable=false`。

## 验证

```sh
python -m ruff check src tests
python -m pytest -q
```

验证覆盖未来数据扰动不改变此前信号、交易日缺口、成本与现金约束、费用舍入、同日卖出、交收、幂等、账本平衡、冻结输入和训练/测试隔离。

见[研究记录](docs/RESEARCH.md)、[实施验收](docs/IMPLEMENTATION.md)及[数据契约](docs/DATA_CONTRACT.md)。

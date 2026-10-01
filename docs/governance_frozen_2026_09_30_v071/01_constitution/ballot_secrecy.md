# 密封立场与投票保密（Sealed Position and Ballot Secrecy）

状态：CONFIRMED

## 1. Co-sponsorship Freeze

Co-sponsorship 是 ballot 前形成的实质支持意见。

在对应 ballot 开启前：
1. co-sponsorship window 关闭；
2. co-sponsorship 状态冻结；
3. Representative 不再修改本轮 co-sponsorship；
4. 其他 Representative 不得获知该 co-sponsorship 状态。

Co-sponsorship 的内部贡献权重属于 Evaluation Firewall，不向 Representative 披露。

## 2. Sealed Ballot

Ballot 期间，各 Representative 的投票彼此保密。

Representative 不得看到：
- 谁已经投票；
- 谁投了什么；
- partial tally；
- 当前领先选项；
- 当前支持比例；
- 尚未投票者的任何立场推断信息。

## 3. No Rolling Tally Disclosure

Orchestrator / Chair 可为程序执行接收并保存 ballot，但在 ballot close 之前不得向 Representative 公开任何 partial tally 或趋势信息。

只有 ballot 完全关闭后，才发布当前程序允许公开的最终结果。

## 4. Co-sponsorship 与最终 Ballot 的关系

Co-sponsorship 不构成不可撤销的最终 ballot contract。

Representative 可以在 ballot 前认为多个方案各自有实质价值；当这些方案后来进入互斥 option set 时，最终 ballot 必须按当前互斥规则选择合法选项。

因此，同一 Representative 同时 co-sponsor 彼此互斥的 amendments：
- 不自动构成程序错误；
- 不打断会议；
- 不触发现场警告；
- 不取消其 co-sponsorship；
- 由 Chair 在会后程序记录中标记，供 Audit Conference 后续分析。

该行为是否代表无差别 co-sponsorship、合理的多方案认可或其他模式，由后续审计判断，而不是在会议中即时训练 Representative。

# 会后审计申请（Post-Meeting Audit Petition）

状态：CONFIRMED

议事决议完成后，在身份仍然保持盲态的情况下，所有代表——包括 Consultative Representatives——拥有平等的审计申请权。

## 允许的申请对象
必须针对程序或制度问题，而不是“我的实体方案输了”。

可包括：
- procedural error；
- procedural ambiguity；
- rights asymmetry；
- agenda bias；
- information asymmetry；
- deadlock risk；
- premature convergence；
- procedural redundancy；
- Chair overreach risk；
- procedural gaming。

## 建议结构
- involved procedure
- observed problem
- why procedural rather than substantive disagreement
- institutional consequence
- references / evidence to inspect

单个代表即可提出申请。

多个申请应聚合成同一 AUDIT CASE 的不同 issues。

提交审计申请不会自动暂停原决定、重新开会或推翻原决定。

是否重新召开由人类决定。

所有 petition 冻结后，审计层方可揭盲。

## 会后密封提交窗口

实现上可将 Consultative Representative 的可选 minority report 与全体 Representative 的
audit petition 收集为同一次隔离、密封的会后提交，以减少重复模型调用。所有提交完成后，
Orchestrator 必须严格按以下顺序公开/冻结：
1. minority/advisory material；
2. audit petition window closure；
3. audit petitions。

每位 Representative 都必须显式返回是否请求审计。`requests_audit=false` 只有在该代表的
有效密封提交已经落盘且 petition 列表为空时才表示“不请求”；缺失、无效或 provider
不可用不得被解释为“不请求”。

petition 冻结后，Chair 可在不读取真实模型/人格映射的条件下，向 Human 报告每个
Representative ID 是否请求审计以及所提程序问题。该报告本身不启动 Audit Conference。

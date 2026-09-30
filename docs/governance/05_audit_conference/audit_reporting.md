# 审计报告结构

状态：CONFIRMED — 轻量约束

审计报告不需要像议事决议那样形成严格统一文本，因为它不直接具有制度修改效力。

Chair 应尽量保留：
1. audit scope；
2. confirmed findings；
3. 主要 institutional interpretations；
4. 仍存在的 disagreement；
5. recommendations；
6. do-not-change / observe-more items；
7. 需要 Human 判断的问题。

对重要 recommendation，机器记录应保存例如：

```yaml
recommendation_id: R-01
summary: "..."
supporters: [AUD-01, AUD-03]
dissenters: [AUD-02]
reservations: []
evidence_refs: [D-03, D-11]
confidence_by_member:
  AUD-01: 0.8
  AUD-02: 0.4
  AUD-03: 0.7
```

这些字段用于防止 Chair 在自然语言 synthesis 中无意改变支持结构。

必须区分：
`Finding != Assessment != Recommendation`

但无需为 recommendation 设绑定性通过门槛。

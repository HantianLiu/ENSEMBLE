# 长期审计指标建议

状态：PROVISIONAL

审计不应主要依赖轶事，应积累结构化事件数据。

建议事件表：
- representative_events
- proposal_events
- amendment_events
- chair_ruling_events
- vote_events
- backtrack_events
- verification_events
- audit_events
- cost_events

可评估：
- proposal survival / acceptance；
- later falsification；
- useful minority objection；
- unique useful contribution；
- objection precision / recall / value；
- calibration（有后验真值时）；
- tokens per useful contribution；
- latency per resolved case；
- pairwise agreement；
- pairwise error correlation；
- persona effect；
- model effect；
- model × persona interaction；
- runtime configuration effect；
- premature convergence；
- redundant discussion；
- human intervention rate；
- co-sponsorship coverage / concentration；
- conflicting co-sponsorship rate；
- semantic duplicate amendment rate；
- atomic-item fragmentation indicators；
- behavior -> future influence correlation；
- behavior -> ACTIVE retention correlation；
- behavior -> Primary Drafter selection correlation；
- institutional advantage after controlling for substantive contribution；
- cross-meeting adaptive behavior signals；
- anti-gaming firewall side effects。

不得把“建议最终被人类接受的比例”直接当作委员质量，因为这会激励模型猜测人类偏好。


## Strategic-Behavior Metrics

策略性行为指标只用于 Audit / Human，不向 Representative 公布。

指标的目的不是建立新的单一“作弊分数”，而是寻找：
- 可重复的行为模式；
- 制度收益；
- 与知识贡献不相称的收益；
- 跨会议适应性变化。

应避免把单次异常行为直接解释为策略性博弈。

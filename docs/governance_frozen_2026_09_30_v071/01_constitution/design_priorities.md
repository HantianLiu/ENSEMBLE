# 设计优先级（Design Priorities）

状态：CONFIRMED

Project_ENSEMBLE 不声称自身是“最优”多模型架构；“最优”的目标函数本身依赖任务与评价标准。

当前制度采取保守的知识治理取向：

`Safety / Correctness > Epistemic Integrity > Procedural Robustness / Traceability > Token / Latency Efficiency`

成本与速度是重要但次级的优化对象。它们只能在已经满足可接受的正确性、安全性、知识完整性与程序完整性约束的方案集合中优化。

因此：
- 不为省 token 静默删除验证或审计；
- 不为保持会议连续而静默改变 electorate；
- 不把不确定性包装成共识；
- 不让摘要替代不可变原始记录；
- 无法安全决定时，优先显式不确定、保留异议、PAUSED 或 Human escalation。

内部 `pragmatic_minimalist` 配置（人类可见标签：`Monitor / 监管者`）的职责是：在满足制度质量底线的可行方案集合中寻找更简单、更低成本的方案，而不是决定这些质量底线是否值得保留。

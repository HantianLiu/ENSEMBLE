# 审计会议 / 政协会议协议

状态：CONFIRMED

## 1. 定位

审计会议是揭盲、平权、扁平的元治理会议。

它不是：
- 第二院；
- 实体案件上诉庭；
- 自动 reopening 机关；
- 自动修宪机关。

## 2. 参与结构

`1 base model = 1 audit member`

不使用 deliberation persona 乘积。

所有委员平权。

## 3. 输入

可包括：
- meeting records；
- proposal/amendment history；
- Chair procedural ruling log；
- vote history；
- advisory/minority material；
- Chair compliance report；
- Chair observation；
- Think Tank opinions；
- audit petitions；
- token/cost/latency；
- 后验结果；
- 揭盲后的 Representative Registry；
- governance-private Drafting Alignment / selection / atomic-item records；
- anti-gaming / strategic-behavior event logs。

## 4. 揭盲

只有在原会议 resolution、minority/advisory material、audit petitions 均冻结后，审计层获得身份映射。

## 5. Phase 1 — Independent Data Mining

每个模型独立读取相同材料，不看其他模型 findings。

优先发现数据结构，不急于改革。

每条 finding 建议包含：
- observation；
- evidence；
- confidence；
- possible interpretation；
- needs_more_data。

`Observation != Interpretation`

## 6. 发布全部 Findings

Phase 1 全部提交后统一公开。

## 7. Phase 2 — Preliminary Assessment

各模型独立形成初步判断：
- 接受/拒绝哪些 finding；
- 哪些可能重复；
- 哪些问题重要；
- 可能改进方向；
- 哪些事项不应立刻修改。

`发现问题 != 必须改革`

## 8. 有限自由讨论

之后进行 bounded free deliberation。

### 顺序
首位发言模型随机选择，之后轮转，避免固定 first/last mover advantage。

### 发言额度
每个模型总共有最多 **5 个 speaking slots**。

每个 slot 必须选择：
- substantive statement；或
- `PASS`。

**PASS 也消耗 1 个 speaking slot，不返还额度。**

达到 5 个 slots 后，该模型不能继续发言。

实质发言应包含新证据、新分析、实质反驳、制度建议或风险分析，避免单纯重复。

## 9. Final Audit Opinion

自由讨论结束后，每个模型独立形成 Final Audit Opinion。

最终意见提交后，不根据其他模型的最终意见再改写自己。

建议区分：
- findings；
- interpretations；
- change suggestions；
- do-not-change suggestions；
- observe-more suggestions；
- unresolved disagreement；
- confidence。

## 10. 不进行绑定性制度表决

审计建议不是修宪决议。

报告可以相对松散，但对每一项重要 recommendation 应尽可能保留机器字段，例如：
- supporters；
- dissenters；
- reservations；
- evidence_refs；
- confidence。

“3/3”“2/3”“1/3”只描述支持结构，不产生自动制度效力。

## 11. Audit Chair

Chair 不作为第四名委员参加制度意见竞争。

Chair：
- 管理隔离与发布；
- 管理 speaking slots；
- 统一向 Human 提问；
- 等同广播 Human 回答；
- 汇总 Final Opinions；
- 生成自然语言报告。

不得制造虚假共识。

## 12. Strategic-Behavior Review

审计会议必须把 **策略性行为与规则套利** 作为固定观察维度，而不是仅在有人投诉时才检查。

审计委员应区分：
- 可观察行为；
- 制度优势；
- 是否存在足够的实质知识贡献解释；
- 是否形成跨会议适应性模式。

不得把主观“作弊意图”作为必要结论。

审计可检查但不限于：
- blanket / conflicting co-sponsorship；
- atomic-item fragmentation；
- semantic duplicate amendments；
- timing / backtracking exploitation；
- amendment-category gaming；
- low-information drafting-credit accumulation；
- 某类行为与后续 ACTIVE / Primary Drafter / influence 的异常相关性。

普通会议中对此类模式原则上只记录、不提示、不现场惩罚；审计阶段再进行解释与制度建议。

完整规范见 `strategic_behavior_audit.md`。

## 13. Audit Context Isolation

Audit Member 必须使用与其 Representative 实例隔离的新 session / runtime context。

审计完成后，不得把完整 governance knowledge 自动带入下一场议事 Representative。

## 14. Human Review

Human 可对建议作：
- ACCEPT
- REJECT
- EXPERIMENTAL
- DEFER

Audit Conference 无制度写权限。

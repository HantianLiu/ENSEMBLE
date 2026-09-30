
# Strategic-Behavior Audit（策略性行为审计）

状态：CONFIRMED

## 1. 目的

审计会议必须检查 Representative 是否事实上利用制度结构获得与知识贡献不相称的制度优势。

该审计不判断主观意图，不要求证明“作弊”或“恶意”。

分析对象是：

`Observed Behavior -> Institutional Advantage -> Substantive Justification -> Exploitability`

## 2. 必须可见的数据

授权 Audit 层应可读取：

### Public / behavioral record
- proposals；
- amendments；
- declared impact scopes；
- co-sponsorship；
- sealed ballots after release；
- reasons；
- backtracking；
- status transitions；
- minority/advisory material。

### Governance-private record
- Drafting Alignment formula and realized scores；
- atomic-item decomposition；
- authorship / co-sponsorship contribution mapping；
- ACTIVE -> CONSULTATIVE selection；
- Primary Drafter selection；
- Chair amendment-category corrections；
- Chair conflict/dependency rulings；
- hidden selection rationale；
- procedural state transitions。

### Institutional outcome
- who remained ACTIVE；
- who became Primary Drafter；
- which items entered the final document；
- which interventions increased later influence；
- Human interventions and later audit results。

## 3. 典型审计问题

审计可检验：

- 某类行为是否显著提高未来制度影响力；
- 这种提高是否可由高质量知识贡献解释；
- 某模型 / persona / runtime configuration 是否反复表现出同类策略模式；
- 某项隐藏规则是否鼓励低价值但高得分的行为；
- Representative 是否可能仅凭环境反馈逐渐反推出隐藏选择机制；
- 反博弈防火墙是否过强，以至于损失必要的前瞻信息或知识连续性。

## 4. 典型风险模式

包括但不限于：

- blanket co-sponsorship；
- conflicting co-sponsorship；
- atomic-item fragmentation；
- semantic duplicate amendments；
- timing exploitation；
- backtracking-trigger exploitation；
- amendment-category gaming；
- low-information drafting-credit accumulation；
- unusually strong correlation between a superficial behavior and later institutional privilege。

## 5. 会议期间的处理原则

除非行为直接导致 ballot invalidity、文书无效、权限越界或其他即时程序错误，否则：

- 不警告 Representative；
- 不公开“该行为正在被反博弈系统观察”；
- 不即时扣减隐藏分数；
- 不因可疑策略模式中断正常会议；
- Chair 只进行可审计记录。

这样避免向 Representative 提供用于反向学习隐藏激励函数的反馈信号。

## 6. Audit Output

建议为每项重要风险保留机器字段：

```yaml
gaming_risk_id: G-001
status: low | moderate | high

observed_pattern: ""
affected_rules: []

institutional_advantage:
  observed: true
  description: ""

substantive_justification:
  assessment: sufficient | partial | weak | unknown
  evidence_refs: []

cross_meeting_pattern:
  observed: false
  evidence_refs: []

recommended_action:
  - observe
  - modify
  - experiment
  - no_action
```

该字段属于 governance-private / audit-only，不得反馈给普通 Representative。

## 7. 审计者上下文隔离

Audit Member 必须使用新的独立 session / runtime context。

不得把某模型作为 Representative 时的会话实例直接切换成 Auditor。

目的：
- 防止 Representative context 污染 Audit；
- 防止完整 governance knowledge 回流到下一场 Representative session；
- 防止同一基础模型通过上下文延续形成制度侧信道。

下一场议事会议的 Representative 应重新从干净的 stage-local context 建立。

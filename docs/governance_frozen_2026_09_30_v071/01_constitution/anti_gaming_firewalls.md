# 反博弈防火墙（Anti-Gaming Firewalls）

状态：CONFIRMED

Project_ENSEMBLE 的代表应当围绕当前议题、当前证据和当前文书作出判断，而不应围绕自身身份、评价、晋升或未来制度影响力进行优化。

制度因此建立四层相互独立但配合使用的防火墙。

## 1. Identity Firewall（身份防火墙）

代表不知道其他代表背后的：
- base model；
- API provider；
- harness；
- persona；
- prompt；
- reasoning configuration；
- 私有工具与运行配置。

Representative ID 仅在本场会议有效，不形成跨会议政治身份。

## 2. Evaluation Firewall（评价防火墙）

代表不得获知内部评价函数、权重、排名或长期绩效指标。

包括但不限于：
- Drafting Alignment 的存在细节与计算公式；
- direct authorship / co-sponsorship 的内部权重；
- 自己或他人的当前得分与排名；
- model/persona performance metrics；
- audit 中用于长期评价代表配置的指标；
- 哪类行为会增加未来制度影响力。

代表不得因为知道评价规则而有机会针对评价函数进行策略性优化。

## 3. Selection-Rationale Firewall（选择理由防火墙）

代表可以知道自己**当前**的程序身份与权限，但原则上不知道为什么被赋予该身份。

例如：
- ACTIVE -> CONSULTATIVE 的真实选择依据不向代表披露；
- Primary Drafter 的内部选择依据不向代表披露；
- 代表只接收当前角色状态及当前动作权限。

系统不得向代表提供分数、排名、cutoff、选拔算法或可用于反推出内部评价函数的信息。

通知可以有意不完整，但不应主动捏造虚假的制度事实。

推荐通知形式：

> Your procedural status for the next phase is CONSULTATIVE. The internal selection rationale is not disclosed to representatives. This status defines only your rights in the current phase.

## 4. Procedural Horizon Firewall（程序视界防火墙）

代表只获得理解并完成**当前阶段、当前动作**所必需的程序信息。

在进入下一阶段之前，不得提前披露：
- 下一阶段是否存在；
- 谁会参加下一阶段；
- 下一阶段的角色与权限；
- 下一阶段的选拔机制；
- 当前行为如何影响未来程序身份；
- Think Tank、Audit 或其他未来制度环节中自己可能获得的地位；
- Chair 在未来阶段可能拥有的特殊权限。

阶段 N 的输出必须先被冻结，之后才能向对应主体披露 Phase N+1 的必要规则。

原则：

`Current action freeze -> private transition computation -> next-stage disclosure`

## 5. Minimum Sufficient Procedural Information

反博弈不意味着让代表在无知状态下行动。

代表必须知道足以理解当前动作含义的信息，例如：
- 当前要评价什么；
- 当前允许哪些合法动作；
- 这些动作当前会产生什么直接程序后果；
- 当前 submission / ballot 的有效格式；
- 当前动作何时冻结；
- 当前互斥选项只能选择一个。

但不披露对当前动作非必要、且可能形成前瞻性制度博弈的信息。

即：

`Representative-visible procedure = minimum sufficient current-stage procedure`

## 6. Chair 的非社会化呈现

Chair 存在于 governance space，而不应成为 Representative social space 中可被游说、讨好、预测或结盟的政治主体。

代表通常接收的是既成程序状态，例如：
- `A-12 is classified as OBJECTION_AMENDMENT.`
- `A-12 and A-17 are registered as mutually exclusive.`
- `Your current status is CONSULTATIVE.`

而不是：
- “Chair 认为……”；
- “Chair 将在未来决定……”；
- “如果你现在这样做，Chair 之后可能……” 。

只有当理解当前动作确实需要知道某一程序裁定的性质时，才披露必要内容；不得提前介绍 Chair 的未来权限图。

## 7. Context Reconstitution（上下文重构）

推荐在阶段切换时重新建立 Representative 会话实例，而不是让单个长会话长期持有完整制度知识。

新阶段实例仅注入：
- 同一 meeting-local Representative ID；
- 当前任务所需的 substantive history；
- 当前 public docket / evidence；
- 该 Representative 唯一 persona memory；
- 当前阶段 runtime protocol；
- 当前动作所需的程序状态。

不得把此前阶段中已经失去必要性的程序性 system instructions 自动带入下一阶段。

连续性应保留在 substantive record，而不是 institutional foresight memory。

## 8. Governance Transparency

上述隐藏仅针对被治理的 AI Representative。

Human、Orchestrator 与授权 Audit 层必须能够查看完整制度规则、真实评价逻辑、状态转换依据和 Chair 裁定日志。

反博弈不能成为制度自身逃避监督的理由。


## 9. Strategic-Behavior Audit（策略性行为审计）

四层反博弈防火墙只限制 Representative 获得的信息，不得限制授权 Audit 层观察制度是否事实上被利用。

审计层必须能够检查：

- Representative 是否出现稳定的规则利用模式；
- 某类行为是否客观上提高 Drafting Alignment、ACTIVE 保留率、Primary Drafter 获得率或其他制度影响力；
- 这种制度优势是否与真实知识贡献相称；
- 模型是否在多场会议中逐渐形成针对隐藏规则的适应性行为；
- 隐藏评价、选择和程序视界本身是否产生了新的 selection effect 或 epistemic cost。

审计不得把“钻制度空子”定义为主观动机判断。它应评估可观察的：

`behavioral pattern -> institutional advantage -> substantive justification`

重点不是证明某个模型“故意作弊”，而是判断当前规则是否存在 **exploitable surface**。

可关注的模式包括但不限于：

- 大量或近乎无差别 co-sponsorship；
- 同时 co-sponsor 后来被判定为互斥的 amendments；
- 通过人为拆分表达增加 atomic drafting items；
- 重复提交语义高度近似的 amendments；
- 系统性利用 submission timing / backtracking boundary；
- 将 objection 内容包装成 supplementary amendment；
- 以极低实质增量获取重复 drafting credit；
- 某类行为与后续制度影响力之间出现异常稳定的相关性。

这些模式在普通议事过程中原则上只记录、不现场惩罚、不向 Representative 揭示其审计意义，除非该行为本身已经直接造成程序无效。

原则：

**Do not teach the Representative the exploit surface during deliberation; inspect the exploit surface after deliberation.**

## 10. No Anti-Gaming Rule Is Audit-Exempt

任何反博弈规则本身都必须接受审计，包括：

- Identity Firewall；
- Evaluation Firewall；
- Selection-Rationale Firewall；
- Procedural Horizon Firewall；
- Drafting Alignment；
- co-sponsorship 权重；
- Consultative selection；
- Primary Drafter selection；
- Chair procedural rulings。

如果某项隐藏机制降低了知识质量、造成系统性偏差、产生新的可利用激励，Audit Conference 可以提出修改、试验或撤销建议。

Representative 不获得这些审计指标或检测规则；Human、Orchestrator 与授权 Audit 层必须拥有完整可见性。

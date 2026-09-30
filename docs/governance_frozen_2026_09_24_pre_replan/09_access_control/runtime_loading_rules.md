# Runtime Loading Rules

状态：CONFIRMED

## Deliberation Representative

任何时刻仅加载：
1. `01_constitution/common_representative_rules.md`；
2. 自己唯一的 persona runtime memory；
3. **一个**与 current meeting state 对应的 stage-local runtime protocol；
4. 当前公开 docket / evidence / substantive history；
5. 当前动作所需的 impersonally stated procedural state。

Representative **不得加载**：
- 完整 `02_deliberation/deliberation_protocol.md`；
- 未来阶段 runtime protocols；
- 其他 persona memory；
- Representative Registry；
- Drafting Alignment formula / scores / ranking；
- selection algorithm / cutoff；
- Chair 的未来权限说明；
- Chair private observation；
- Think Tank / Audit 的未来权限与评价方法；
- 其他 Representative hidden reasoning。

若本场启用 Research Desk，所有 Representative 在所有阶段均可读取、检索和引用已经
完成校验的公共 evidence packet 数据库。上下文组装器可以按当前 claim 检索相关 packet，
但不得以 context-window 优化为理由取消 Representative 对其他公共 packet 的按需访问权。
默认注入采用有界相关性视图：每次最多 16 个有效 packet、64,000 个 Unicode 字符，并明确
显示有效总数、实际选中数与省略数。省略仅表示本次 prompt 未自动复制，不表示证据不存在；
完整 snapshot、packet、来源原文和文献包仍是公开权威记录。任何后续按需文件工具必须通过
同一公开路径授权与访问审计，不得借“压缩”隐藏或删除公开证据。
不得加载 `audit_private/research/` 中的原始查询、候选集、淘汰记录、筛选依据或其他
请求者的调用轨迹。

推荐每次阶段切换执行 Context Reconstitution：新建会话，只注入当前阶段必要的 substantive state 与 protocol surface。

## Deliberation Chair

加载：
- Chair runtime memory；
- 完整 governance deliberation protocol；
- anti-gaming firewalls；
- current meeting state；
- proposal/amendment docket；
- sealed ballot state；
- provenance；
- governance-private scoring / selection rules。

Chair 在 petition freeze 前不得访问 Representative ID -> model/persona 的真实映射。

Chair 可知道完整程序，但必须按 minimum sufficient disclosure 向 Representative 暴露信息。

## Think Tank Librarian

仅在实体议事已经冻结、且该阶段被正式启动后加载：
- Think Tank runtime memory；
- procedurally certified resolution；
- original task；
- evidence/provenance；
- Chair handoff brief（相应 review 时）。

普通 Librarian Representative 在此前阶段不得提前加载 Think Tank runtime memory 或知道其后续特殊权限。

## Audit Member

仅在 audit 正式启动后加载：
- audit runtime memory；
- audit protocol；
- 揭盲后的 audit evidence；
- Phase 1 仅自己的工作区；
- Phase 2 全部已提交 findings。

不加载 deliberation persona prompts。

## Audit Chair

加载：
- audit chair runtime memory；
- audit protocol/state；
- submitted findings/assessments/discussion/final opinions；
- Human messages。

不得作为额外 audit member 参与立场竞争。


## Audit Strategic-Behavior Access

Audit Member / Audit Chair 可加载：
- governance-private Drafting Alignment / selection records；
- atomic-item decomposition；
- co-sponsorship and ballot history；
- Chair procedural ruling logs；
- strategic-behavior event logs；
- full anti-gaming rules。

这些信息为 audit-only，不得被回写进普通 Representative runtime。

Research Desk 的完整查询、候选、筛选与 cache/freshness 记录也属于 audit-only。

Audit 必须使用新 session，不得把 Representative conversation state 直接升级为 Auditor state。

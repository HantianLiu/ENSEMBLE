# Project_ENSEMBLE 制度总览

## 1. 制度目的

Project_ENSEMBLE 是面向知识生产、科研分析与方案决策的多模型协作制度。

当前目标是改善：
- 决策质量；
- 知识完整性；
- 可验证性；
- 可追溯性；
- 多模型异质信息利用；
- 长期制度可审计性。

系统暂不以“AI 自行完成所有计算研究或代码工程”为目标。

## 2. 正式会议类型

### 2.1 议事会议（Deliberation Meeting）

形成具体任务的共同文本，包括：
- 总则；
- 细则；
- amendment；
- vote；
- minority/advisory material；
- Chair 程序检查；
- Librarian Think Tank 知识检查；
- 向人类提交的正式执行/移交文书。

### 2.2 审计会议 / 政协会议（Audit Conference）

元治理会议，分析：
- 程序实际效果；
- 模型行为；
- persona 行为；
- runtime configuration；
- 成本与效率；
- 后验验证；
- 制度性缺陷。

审计只向人类提出建议，不直接修改制度。

## 3. 非会议执行层

具体执行可以是：
- 代码实现；
- 研究计划；
- 文献工作；
- 实验方案；
- 数据处理；
- 报告撰写；
- 其他人机协作任务。

ENSEMBLE 只规定议事结果如何被**清楚地移交**，不要求所有任务具有统一 execution schema，也不假设执行者一定是 AI。

## 4. 参与者

### 4.1 Representative

`Representative = Base Model × Persona`

四种内部配置及其人类可见职位标签：
- `systems_integrator` — Builder / 建构者
- `pragmatic_minimalist` — Monitor / 监管者
- `exploratory_synthesist` — Cartographer / 制图者
- `librarian` — Librarian / 智库长

这些双语职位名只属于前端描述；内部配置、提示词和治理职责保持不变。

### 4.2 Chair

Chair 由人类指定模型。它不是普通代表，负责：
- workflow；
- ballot/procedure；
- amendment 文书分类与冲突映射；
- provenance；
- meeting record；
- final documentation；
- human communication。

除规则明确的 primary-drafter tie-break 外，Chair 不参与实体投票。

### 4.3 Librarian Think Tank

在 Chair 完成程序性检查后，对修正后的正式文本进行知识完整性检查，只给出意见，不自动改变议事结果。

### 4.4 Audit Member

审计会议采用：

`1 base model = 1 audit member`

不使用 persona 乘积。

## 5. 权力分配

- 实体方案形成：Deliberation Assembly
- amendment 文书分类、冲突映射、程序裁定：Chair（可审计）
- 程序完整性：Chair
- 知识完整性意见：Librarian Think Tank
- 制度审计：Audit Conference
- 是否重开会议、是否采纳制度修改、Chair 选择与异常最终裁决：Human

## 6. 最终交付链

`Assembly substantive result`
→ `Chair procedural review/correction`
→ `Think Tank epistemic review`
→ `Chair execution/handoff brief`
+ `Think Tank review opinion`
→ `Human review`

Think Tank 不具有自动退回权。

## 7. 反博弈体系

Project_ENSEMBLE 不要求 Representative 了解完整制度，而要求其拥有完成当前动作所需的最小充分程序知识。

四层防火墙：
1. Identity Firewall：不知道别人是谁；
2. Evaluation Firewall：不知道自己如何被评价；
3. Selection-Rationale Firewall：不知道为何获得当前身份；
4. Procedural Horizon Firewall：不知道尚未进入的未来阶段和未来权限。

目标是让 Representative 优化当前问题本身，而不是优化自己的制度位置。

完整 Governance Constitution 对 Human、Orchestrator 和授权 Audit 可审计；隐藏不意味着制度自身不可监督。


## 反博弈与审计闭环

Representative 受 Identity / Evaluation / Selection-Rationale / Procedural-Horizon 四层防火墙约束，只获得当前任务所需信息。

授权 Audit 层则获得完整 governance-private 记录，并固定执行 Strategic-Behavior Audit，以判断制度是否事实上产生可利用的规则表面、适应性博弈或与知识贡献不相称的制度优势。

原则：
`Representative cannot optimize the hidden game; Audit can inspect whether the hidden game is being exploited.`

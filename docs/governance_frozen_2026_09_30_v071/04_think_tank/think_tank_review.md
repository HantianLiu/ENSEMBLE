# Librarian Think Tank — 知识完整性检查

状态：CONFIRMED

## 1. 组成

主要实体表决完成且 Chair 已完成程序性检查后，每个基础模型的 Librarian 组成临时 Think Tank。

即使某 Librarian 已转为 CONSULTATIVE，在 Think Tank 阶段恢复这一特定复核职责，以保证模型阵营对称。

## 2. 顺序

Think Tank 只审查经过 Chair procedural review/correction 后的文本。

程序性问题前置，避免对一个可能因计票或文书错误而变化的旧文本进行知识审查。

## 3. 权限性质

Think Tank 拥有：
`epistemic / transactional review`

不拥有：
`substantive appellate authority`

可指出：
- 原始需求遗漏；
- 关键定义缺失；
- 假设丢失；
- claim 缺证据；
- 证据与结论强度不匹配；
- 条款存在知识性矛盾；
- 必要验证缺失；
- 文献/benchmark 缺失；
- provenance 中知识链断裂。

## 4. Think Tank 不自动退回

无论意见严重程度如何，Think Tank 都不自动：
- reopen meeting；
- trigger revote；
- return resolution；
- rewrite normative text。

它只形成检查意见。

是否重新召开议事会议，完全由 Human 决定。

## 5. 人类移交中的第二份文件

Chair 负责形成主要 execution/handoff brief。

Think Tank 阅读 Chair brief 和 procedurally certified resolution 后，独立形成：
- epistemic completeness opinion；
- omission/clarification notes；
- execution/research cautions；
- 是否值得 Human 考虑 reconvene 的理由。

`reconvene_reason` 可以记录建议重开或不建议重开的理由：当
`reconvene_worthy=true` 时理由必须存在；当 `reconvene_worthy=false` 时，理由可以保留，
用于说明为何现有 finding 只需在执行、交接或 Human 监督中处理，也可以为 `null` 以兼容
已经完成的旧审查。否定建议的理由不得因 schema 修复而丢失。

该文件与 Chair 主文书并列呈递给 Human，不能由 Chair 合并成单一“共识报告”。

## 6. 独立附录与可读性

每名智库长分别提交完整意见。系统只可进行机械装订、目录索引和数量统计，不得生成
联合实体意见，也不得由 Chair 消解少数意见。每份意见作为独立审计附录呈递给 Human。

智库长自己的文书必须面向人类读者，依次表达：简要结论、重要发现、证据与实际影响、
建议。语言应清晰、简洁、非官僚化；不得重复会议流程、堆砌机器字段，或依赖未解释的
内部术语。不设置硬性字数上限，但冗长本身不构成审查质量。

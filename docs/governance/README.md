# Project_ENSEMBLE — Governance Package v0.7.1

状态：**DRAFT / 当前制度基线**

新文献报告会议可在初始化时冻结 `literature_writing_policy = "v071"`；
其模块写作与科学审阅遵循
[`02_deliberation/literature_writing_v071.md`](02_deliberation/literature_writing_v071.md)。
下文原有文献报告段落描述 v0.7.0 的历史流程，只适用于未启用新政策的会议。

`Project_ENSEMBLE` 是一个面向知识生产、研究分析与方案决策的多模型审议系统。当前重点是改善 AI 的**决策与知识形成能力**，而不是让 AI 自动承担完整的计算、研究或代码交付链。

当前制度只承认两种正式会议：

1. **议事会议（Deliberation Meeting）**：形成具体任务的共同方案与正式文书；
2. **审计会议 / 政协会议（Audit Conference）**：对制度、程序、模型与人格表现进行元治理分析，并向人类提出建议。

具体执行不构成第三种会议。议事完成后，由 Chair 形成主要执行/移交文书，Librarian Think Tank 独立阅读并形成检查意见；二者作为两份独立材料一起提交人类。执行形式可以是代码、研究计划、报告、实验方案、数据处理任务或其他工作，不强制套用统一模板。

命令行虽然提供 research-only session 入口，但它只是一次不产生代表或表决的
Research Desk 查询服务，不构成第三种正式会议。

议事会议可以选择 `literature_review` 交付物，并从既有会议派生。此时“总则”改指研究
总纲：四名覆盖全部职位的规划代表独立拆分问题，Chair 可追溯聚类，全体代表进行一次
限量结构审阅。冻结后，各模块依次完成轮换问题组、Research Desk 证据问答、起草、
专业审阅、全体限量审阅和 3/4 确认；争议模块保留并列意见。全文经一次全体审阅后
无门槛发表。源会议保持冻结；其公开 Research Desk 资产复制到派生会议并保留逐文件
哈希谱系。该模式仍是议事会议，不新增票席或正式会议类型。

---

## 核心结构

### Representative

`Representative = Base Model × Persona`

内部仍使用四个稳定 persona 配置；人类界面使用非人格化的双语职位标签：

| 内部配置 | 人类可见职位 |
|---|---|
| `systems_integrator` | Builder / 建构者 |
| `pragmatic_minimalist` | Monitor / 监管者 |
| `exploratory_synthesist` | Cartographer / 制图者 |
| `librarian` | Librarian / 智库长 |

职位标签只属于展示层，不改变 prompt、内部枚举值、治理职责或既有会议记录。

每个入选基础模型完整遍历四种人格。

### Chair

Chair 不是普通代表。Chair 由人类为每场会议指定模型，负责程序裁定、文书流转、计票、provenance、最终记录和人类通信。

Chair 原则上无实体投票权；唯一已明确的例外是：**primary drafter 选举在全员二选一后仍平票时，由 Chair 投决定票。**

### Librarian Think Tank

主要实体表决完成并经过 Chair 的程序性检查后，各模型的 Librarian 组成临时智库，执行知识完整性检查。

Think Tank 只提交检查意见，**无论如何都不自动退回议事会议**。是否重新召开、重新讨论或采取其他措施，由人类决定。

### Research Librarian / Research Desk

会议初始化时可选择启用共享的外部文献调研服务。Research Desk 不增加票席，不作实体
判断；它把明确事实性 claim 规范化，分别检索支持、反证、适用范围限制和 canonical
alternatives，并返回可追踪的 evidence packet。若 `CLEAR` 共识门槛不足但支持证据仍然
可用，则保留 packet、将共识降为 `QUALIFIED`，并在来源资格满足时保留
`SOURCE_BACKED` knowledge status；完整查询与筛选轨迹只进入审计层。

### Audit Conference

审计会议采用扁平结构：

`1 base model = 1 audit member`

不进行 persona 乘积展开。审计建议没有直接修宪效力，报告可以比议事决议更松散，但必须保留可机器读取的支持/异议结构。

---

## 人格隔离

- `03_roles/`：供人类与 orchestrator 阅读的完整制度说明；
- `07_runtime_memory/`：实际注入各参与者上下文的独立角色文件。

议事 Representative 只加载：
1. `01_constitution/common_representative_rules.md`；
2. 自己唯一对应的 persona runtime memory；
3. **当前阶段唯一的 stage-local runtime protocol**；
4. 当前公开 docket / evidence / substantive state。

Representative 不读取完整 deliberation protocol，也不提前加载未来阶段规则。不得加载其他 persona 的 runtime memory。

---

## 反博弈核心

ENSEMBLE 当前固定四层防火墙：
- Identity Firewall；
- Evaluation Firewall；
- Selection-Rationale Firewall；
- Procedural Horizon Firewall。

原则是：Representative 知道当前任务和当前合法动作，但不知道其他主体身份、内部评分、为何获得当前状态，以及尚未进入的未来程序。

Co-sponsorship 在 ballot 前冻结并保持密封；ballot 采用 sealed collection，禁止 ballot close 前公开 partial tally。

完整制度仍对 Human / Orchestrator / Audit 可审计。

Audit Conference 现在固定包含 **Strategic-Behavior Audit**：审计层可查看对 Representative 隐藏的评分、选择、atomic-item、co-sponsorship 与程序裁定数据，以判断是否出现规则套利、适应性博弈或与知识贡献不相称的制度优势。普通议事阶段原则上只记录这些模式，不向 Representative 提示其审计意义。

## 当前仍未定案

主要剩余问题见 `10_open_questions/open_questions.md`。当前最重要的未决项包括：
- Clause Split 的永久正式阈值（当前试行 `ceil(N_ACTIVE/4)`，待 Think Tank 审阅）；
- Motion to Suspend 的永久支持门槛（当前试行 `ceil(N_ACTIVE/3)`，待 Think Tank 审阅）；
- Drafting Alignment 试行 atomic-item 粒度在 Think Tank 审阅后的永久化决定；
- 三名以上 primary-drafter 并列时的选举程序；
- malformed / schema-invalid API 输出的处理方式；
- Think Tank 多 Librarian 意见的最终汇总方式。


## v0.7 implementation note

The reference implementation adds role-specific reasoning controls and a shared,
non-voting Research Desk while preserving the conservative priority: safety/correctness
and epistemic/procedural integrity take priority over token and latency efficiency.
See `../../architecture/design_priorities.md`.

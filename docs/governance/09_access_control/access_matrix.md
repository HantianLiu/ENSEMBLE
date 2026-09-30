# 访问矩阵

状态：CONFIRMED

| 资源 | Representative | Librarian during deliberation | Deliberation Chair | Think Tank | Audit Member | Audit Chair | Human |
|---|---:|---:|---:|---:|---:|---:|---:|
| Common Representative Rules | ✓ | ✓ | ✓ | 可读 | 可读 | 可读 | ✓ |
| 自己 Persona Memory | ✓ | ✓ | ✗ | Librarian task only | ✗ | ✗ | ✓ |
| 其他 Persona Memory | ✗ | ✗ | 原则上无需 | ✗ | ✗ | ✗ | ✓ |
| Current Stage Runtime Protocol | ✓ | ✓ | ✓ | 按阶段 | 按阶段 | ✓ | ✓ |
| Future Stage Protocols | ✗ | ✗ | ✓ | ✗ | ✗ | ✓ | ✓ |
| Full Deliberation Protocol | ✗ | ✗ | ✓ | 按需 | ✓ | ✓ | ✓ |
| Current Public Docket | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Other Representatives' co-sponsorship before ballot close | ✗ | ✗ | sealed state only | — | 后续审计可读 | 后续审计可读 | ✓ |
| Partial Ballot Tally before close | ✗ | ✗ | sealed procedural state | — | 后续审计可读 | 后续审计可读 | ✓ |
| Drafting Alignment Formula / Scores / Ranking | ✗ | ✗ | ✓ | ✗ | ✓ | ✓ | ✓ |
| Strategic-Behavior detection rules / gaming metrics | ✗ | ✗ | ✓（记录，不反馈） | ✗ | ✓ | ✓ | ✓ |
| Strategic-Behavior event logs | ✗ | ✗ | ✓（记录） | ✗ | ✓ | ✓ | ✓ |
| Selection Rationale / Cutoff | ✗ | ✗ | ✓ | ✗ | ✓ | ✓ | ✓ |
| Representative Registry before petition freeze | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| Representative Registry after unblinding | ✗ | ✗ | ✗ | 按授权 | ✓ | ✓ | ✓ |
| Human operational contact (email) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| Chair Private Observation during meeting | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ | ✓ |
| Chair Observation after petition freeze | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Other Representative hidden reasoning | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Research Desk 公共 evidence packet 数据库 | 所有阶段均可读取、检索、引用 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Research Desk 原始请求者/调用阶段 | 自己的请求 | 自己的请求 | 程序记录 | 按授权 | ✓ | ✓ | ✓ |
| Research Desk 查询、候选与筛选轨迹 | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |
| Research Desk 模型与 reasoning effort | ✗ | ✗ | ✗ | ✗ | 按授权 | ✓ | ✓ |

权限必须由 Orchestrator / filesystem / tool namespace 强制执行，不依赖 prompt 自律。

Research Desk 不是表中的新投票角色。它的公开 packet 位于共享 evidence 层；完整检索
轨迹位于 `audit_private/research/`，不得因 packet 被共享而一并公开。


## 审计上下文隔离

Audit Member 必须使用独立于其议事 Representative 的新 runtime session。

Audit 获得的完整 governance-private 信息不得自动进入后续 Representative session。

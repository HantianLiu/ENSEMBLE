# 尚未定案的问题

实现时不得静默自行决定以下事项。

## 1. Atomic Drafting Item 的粒度 — 已进入试行

Drafting Alignment 公式已确定：

`S_r = D_r + 0.5 * C_r`

但“一个 atomic item 到底多大”会直接影响得分。

需要最终确定：
- item 必须在提交时预先原子化，还是可由 Chair 在 provenance 中规范化；
- 一个长条款拆成多个 item 的客观标准；
- 如何防止通过人为切碎条目增加 contribution count。

Human 于 2026-09-18 授权采用 B1.1 的机械粒度规则试行，因此本项不再阻塞状态
转换。该规则仍须接受 Think Tank 审阅；重点审查 D0 编号粒度、adopted application
的 retention 推定以及整稿 co-sponsorship 向内部 items 传播是否产生系统性偏差。
Think Tank 只能提出意见，不得自行追溯改写已冻结结果；永久规则仍由 Human 决定。

## 2. Primary Drafter 三名以上并列 — 已定案

两名并列时由全员二选一，平票由 Chair 公开投决定票。三名及以上并列时，全体合格
代表提交不得弃权的密封排序；第一偏好决定前二，第二晋级席并列时对并列者加赛，最后
两名进行密封二元表决，仍平票时由 Chair 公开投决定票。不得向代表公开 Drafting
Alignment 分数、排名或选拔理由。本项不再是 OPEN。

## 3. 同一窗口 Amendment 的顺序 — 已临时解决

一般 amendment 采用 sequential processing，且每次表决后更新 current context。已确认的
互斥集合是例外：先按 conflict-set 程序共同处理；部分互斥时，兼容片段二元表决，
互斥片段 multi-option 后进入 top-two 二元表决。

Human 已决定先采用窗口冻结后的可复现随机排序。随机 seed、确定性算法和
最终顺序必须留痕；不得使用 submission timestamp 或 Chair 主观排序。该规则
可在后续 Audit 后重新评估，但不得凌驾于已确认的 conflict-set 优先规则；当前实现
不再把此分支视为 OPEN。

## 4. Clause Split 阈值 — 已进入试行

试行门槛为 `ceil(N_ACTIVE/4)`；当前 9 名 ACTIVE Representative 时为至少 3/9。
永久门槛及实际 split 质量由 Think Tank 审阅后再由 Human 决定。

## 5. Motion to Suspend 支持门槛 — 已进入试行

试行门槛为 `ceil(N_ACTIVE/3)`；当前 9 名 ACTIVE Representative 时为至少 3/9。
永久门槛仍待 Think Tank 审阅和 Human 决定。

## 6. API 可达但输出无效 — 下一版本规则已定案

“连接失败 + 3 次 retry 后会议暂停”已确定。

采用两层、逐项目、有界的结构化响应恢复；外层完整响应与每个独立无效项目分别最多
三次。合法项目保持冻结，非结构关键的无效发言最终记为空提交或缺失票，并按该项目的
结果关键性决定继续或暂停。详细实现约束见根目录 `TODO.md` 的 v0.7.0 治理包。

## 7. Audit Discussion 的提前终止 — 已定案

每个 audit member 最多 5 个 speaking slots，PASS 也消耗一个 slot，已确定。

若全部合格审计成员均提交有效 PASS，且不存在 finding、question、material request 或
可能改变结论的缺失提交，则立即以 `AUDIT_CLOSED_ALL_PASS` 结束讨论。非实质性建议可
进入附录而不阻塞；任何尚未解决的实质性事实、程序或结果影响问题均阻止提前关闭。

## 8. Human Re-convene 的最小范围

Think Tank 无自动退回权已经确定。

若 Human 根据 Think Tank/Audit 意见决定重新召开议事会议，尚未正式规定：
- 从总则重新开始；
- 仅 reopen 某些 clauses；
- 仅请求 clarification；
之间的默认选择规则。

## 9. Execution/Handoff 的最小必备字段

已经确定不建立统一代码交付制度，也不要求自主执行。

仍可考虑是否规定极小的共同字段，例如：
- task goal；
- approved constraints；
- provenance refs；
- contested items；
- Think Tank cautions。

当前允许按任务自由组织。

## 10. Pause 发生在 ballot 中途时的 ballot atomicity — 已定案

Human 决定（2026-09-17）：恢复任何中途中断的 ballot 时，由 Chair 对已有密封票
逐份执行完整性检查：

- 完整的票原样保留，不再次调用该 Representative；
- 不完整或不合法的记录不可覆盖，原文件继续留档，但不计入 ballot；
- 不完整记录对应的 Representative 重新投票；
- 尚无有效记录的 Representative 重新投票；
- 不得仅因恢复而作废完整票或要求全员重投。

“完整”采用机械、可审计标准：记录必须是符合当前 ballot schema 的有效 JSON，
`ballot_id` 必须匹配，`representative_id` 必须属于本次合格名单并与记录路径一致，
choice 必须是合法选项；若某选项要求理由，则理由字段也必须有效。Chair 必须保存
恢复审查记录，但 ballot 关闭前不得向 Representative 公开谁已投票、票数、选择或
partial tally。

## 11. Think Tank 多智库长意见 — 已定案

各智库长独立提交，分别作为审计意见附录。系统只做机械装订、索引和数量统计，不形成
联合实体意见，Chair 也不得综合掉异议。每份意见采用“简要结论—重要发现—证据与影响—
建议”的人类可读结构，不设置硬性字数上限。本项不再是 OPEN。

## 12. 文献调研报告的模块正文形成与采纳 — 已定案试行

Human 已逐项确定模块证据轮次、均衡轮换、模块串行调度、两轮限量审阅、3/4 模块确认、
争议模块并列呈现、全文综合、无条件发表、Think Tank 事实性异议过滤和逐条可读性 patch
审核。完整规则见 `02_deliberation/literature_review_continuation.md`。本项不再以
`LITERATURE_MODULE_DRAFTING_POLICY_NOT_CONFIGURED` 阻塞；试行结果仍可在后续政协或
Think Tank 审阅后修订，但不得追溯改写已冻结会议。

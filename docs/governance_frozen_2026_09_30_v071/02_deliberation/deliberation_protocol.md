# 议事会议协议

状态：CONFIRMED；少数参数见 `10_open_questions/open_questions.md`。

## A. 总则阶段（General Principle）

### A1. 初始草案
所有初始 Representative 均参与总则制定。

随机选择一名 Representative 独立产生初始总则草案。

### A2. 初轮立场
Representative 可：
- SUPPORT；
- OPPOSE + 提交 amendment。

### A3. 修正案类型

1. **Objection Amendment（异议性修正案）**：删除、修改或否定现有规范性命题、目标或约束。
2. **Supplementary Amendment（补充性修正案）**：在不否定现有规范性命题的前提下增加内容。

提案人必须声明类别。

若类别存在争议，由 Chair 作出**程序性分类裁定**。Chair 必须保存：
- proposer 原始分类；
- Chair 最终分类；
- Chair 的简要理由；
- 受影响的后续程序。

该裁定进入 audit evidence，供后续政协再次评价。

### A4. Co-sponsorship 与密封冻结
Representative 可 co-sponsor 初稿或具体 amendment。

共同支持异议性 amendment，视为对其目标原文相应部分的反对。

Co-sponsorship 必须在对应 ballot 开启前冻结，并在 ballot close 前对其他 Representative 保密。不得公开 co-sponsor 列表、partial tally 或由 co-sponsorship 推断出的支持趋势。

Co-sponsorship 对 Drafting Alignment 的贡献权重为 direct authorship 的 50%，但该权重、Drafting Alignment 公式与其后续制度作用均属于 governance-private 信息，不向 Representative 披露。

同一 Representative 可在 ballot 前同时 co-sponsor 后来被判定为互斥的 amendments。该行为不打断会议、不触发现场纠正；Chair 仅在程序记录中标记，供后续 Audit Conference 评价。最终互斥 ballot 仍要求给出单一合法选择。

### A5. 不进行人格化辩论
讨论对象是文书、命题、证据和程序，不是提出者本人。

### A6. 不允许 amendment-on-amendment
Amendment 不递归修改 amendment。

若需根据既有 amendment 产生新文本，应在下一允许窗口提交新 amendment。

### A7. Amendment Impact Scope 与 Chair Conflict Map

每个 amendment 提交时必须声明至少：
- target clause / target item；
- intended operation；
- claimed affected scope；
- proposer 已知的 incompatibility / dependency。

代表负责报告自己的**影响范围**；Chair 负责据此建立本轮 operative conflict/dependency map。

Chair 的工作属于文书与程序流转，不赋予其实体优先权。任何 Chair 对 scope、conflict、dependency 的修正都必须留下 procedural ruling log，供审计复核。

### A8. Sequential Amendment Processing

Amendment 按顺序处理。

每完成一个 amendment decision：
1. 更新 current draft/context；
2. 更新后续 amendment 的适用范围和 conflict/dependency 状态；
3. 若后续 amendment 因当前文本变化而不再可按原样表决，Chair 应标记为 `REBASE_REQUIRED`、`SUPERSEDED` 或相应程序状态，而不是允许两个互相冲突的 amendment 分别成为已通过文本。

因此制度不允许出现“两个已分别独立通过的 amendment 在最终合并时才发现互相冲突”的正常状态。

同一提交窗口内多个 amendment 在窗口冻结后采用随机顺序。Orchestrator 必须：
- 在所有本窗口 amendment 冻结后才生成随机 seed；
- 保存 seed、确定性排序算法和最终 amendment ID 顺序；
- 使排序结果可由保存的 seed 重放验证；
- 不以提交时间、Chair 主观判断或 proposer 身份影响顺序。

当前参考算法为：对每个 amendment ID 计算
`SHA-256(seed_bytes || NUL || amendment_id)`，按 digest 升序排列，以 amendment
ID 作为理论上的 digest 碰撞 tie-break。保存的最终顺序是该窗口的权威顺序。

### A9. 冲突选择

对同一 current text 存在竞争方案时：
- 单一 objection amendment 对 current text：binary vote；
- 多个互斥方案：先 multi-option，top two 再 binary。

Human 决定（2026-09-18）：本节的冲突集合程序优先于 A8 的随机逐项处理。
冻结随机顺序仍用于确定冲突集合首次被触发的位置、集合内兼容片段的处理顺序，
以及集合处理完成后其余 amendment 的顺序，但不得把已确认互斥的方案拆成彼此独立的
顺序二元表决。

若 amendments 仅部分互斥，Chair 必须先生成可审计的 conflict decomposition：

1. 将可以同时成立的内容隔离为 compatible fragments；
2. 将不能同时成立、回答同一规范问题的内容组织为一个或多个 exclusive choice sets；
3. 每个 fragment 必须记录原 amendment provenance、目标范围和可直接应用的完整文本；
4. compatible fragments 按冻结随机顺序分别对 current text 进行 binary vote；
5. 每个 exclusive choice set 进行 multi-option，选出 top two 后进入 Type II；
6. 每个 choice set 必须包含 `STATUS_QUO`，不得强迫代表采纳任一新方案；
7. 若 multi-option 的第二名 cutoff 出现同票，以本窗口已冻结的 docket seed 对 option ID
   做确定性 SHA-256 排序；该规则只决定进入 top two 的名单，不授予 Chair 裁量权；
8. 最终二元票平票时维持 current text，Chair 不得以实体偏好破平票；
9. Chair 只执行拆分、分类和文书应用，不得选择获胜内容。

若自动复核认定只是重叠但兼容，而非互斥，则恢复 A8 的随机逐项程序，无需 Human
咨询。只有 schema 无法验证、来源无法追溯或无法形成合法 choice set 时才关闭式暂停。

### A10. 投票门槛、禁止弃权与密封票箱

高门槛：`ceil(3N/4)`。

Deliberation ballot **不允许 abstain**。每个有投票权的 ACTIVE Representative 必须给出合法选项。

所有 ballot 均采用 sealed collection：在 ballot close 前，Representative 不得看到其他 Representative 是否已投票、具体票型、partial tally、领先选项或任何实时归票趋势。

Orchestrator / Chair 可以接收和保存 ballot，但不得在 ballot close 前公开 partial tally。ballot close 后才统一发布当前阶段允许公开的结果。

同一密封提交窗口内，彼此不依赖的 Representative 调用可以按 `provider:model`
组成 bounded model lanes 并行执行。不同模型 lane 可以并行；同一模型 lane 必须遵守会议初始化时
冻结的并发上限，未知上限按 1 处理，并按固定 persona 顺序入队。并行只改变调用调度，不改变
密封门闩：窗口关闭前不得公开提交状态、正文、选择或 partial tally，冻结文件必须按 roster
顺序确定性生成。已经完成 schema 校验并单独落盘的密封提交在同窗口其他调用失败时保留，恢复时
只重做缺失或无效提交。该规则不允许并行处理会改变彼此输入的连续 amendment，也不允许把首轮、
解释轮和决选轮等有依赖关系的不同 ballot round 同时开启。

若二元表决未达到 3/4：
1. 每位 Representative 可一次陈述投票理由；
2. 进入 Protective Vote；
3. `> N/2` 可决定。

Protective Pass 必须标记：
- `PROTECTIVE_PASS`；
- 未达到 3/4；
- vote pattern；
- 主要异议。

有 status quo 的平票保留 status quo。无 status quo 且无法决出时，提交人类处理。

### A11. Representative Unavailability

API/provider 不可用时：
1. 初始请求失败；
2. 最多进行 3 次 retry；
3. 三次 retry 后仍无法连通，则该 Representative 标记 `REPRESENTATIVE_UNAVAILABLE`；
4. **会议立即进入 PAUSED，不以缩减 roster 继续进行。**

人类决定何时、如何恢复。

格式错误但 API 可用的输出如何处理，仍为 OPEN。

### A12. 总则迭代

目标结构：`D0 -> D1 -> D2 -> D3 final`。

每轮遵循：
`amendment submission -> Chair document/conflict processing -> conflict-set voting where applicable -> otherwise sequential voting -> traceable update`

Chair 不得通过 merge 创造新的规范性内容。

### A13. 最终批准

D3 进行 final yes/no ratification。反对者必须说明理由。

最终通过要求 3/4。未通过时 PAUSED 并提交人类。

来源状态区分：
- `SUPERMAJORITY_PASS`
- `PROTECTIVE_PASS`
- `HUMAN_OVERRIDE`

### A14. 制度冲突时的 Chair 咨询权

若当前程序同时适用的制度条款发生冲突，或制度未给出足以唯一执行当前动作的
规则，Chair 不得自行创设实体或程序默认值。Chair 应：

1. 冻结当前可恢复状态，不开启受影响的新 ballot；
2. 向 Human 提交结构化咨询事项，列明冲突条款、受影响对象、合法候选处理方式；
3. 记录当前 roster、各候选门槛的公式、比较符号和换算后的最低票数；
4. 等待 Human 给出具名于本次咨询事项的决定；
5. 将 Human 决定、适用范围、采用的门槛及理由保存为不可变 procedural ruling；
6. 仅按该 ruling 恢复受影响的程序，不将一次性决定扩张为全局默认规则。

在咨询关闭前，Human 可以每次提出一个自然语言程序问题。Chair 可以解释：
- 当前分类及其依据；
- 各选项的程序后果；
- 已记录门槛的含义；
- 是否存在把“内容重叠”误判为“互斥”的可能。

Chair 的解释不构成 Human 决定，也不得表达实体方案偏好。Human 可以正式要求 Chair
重新审查 scope / conflict / dependency 分类；复核必须生成新的不可变 ruling，不得覆盖
原裁定。Human—Chair 咨询问答每轮只包含一条 Human 问题，并保存为 human-private
记录，不自动向 Representative 发布。

咨询权不是 Chair 的实体裁量权。Chair 可以说明冲突和执行后果，但不得替 Human
选择哪一项规范性政策。

### A15. 中途中断 ballot 的恢复

ballot 在关闭前因 API、进程或其他执行故障中断时，Chair 必须先按当前 ballot
schema、ballot ID、合格名单、合法选项及选项专属必填字段，逐份检查已有密封票：

1. 完整合法的票保留，恢复时不得重新调用该 Representative；
2. 不完整或不合法的原记录不可覆盖，也不得计票；
3. 不完整记录对应的 Representative 以及没有有效记录的 Representative 重新投票；
4. Chair 保存 governance-private 完整性审查记录；
5. ballot 关闭前继续禁止公开提交者、票数、选择或 partial tally。

若 Human 对程序冲突的 ruling 改变了 ballot 问题、选项、合格名单或其他定义，新表决
必须使用新的 ballot ID；旧记录因不匹配新 ballot 而仅留档、不计入新表决。

---

## B. Drafting Alignment 与代表状态转换

### B0. Governance-Private Selection Rule

本节 B 的评分公式、排名、cutoff、Primary Drafter 选拔逻辑和状态转换依据均属于 **Evaluation Firewall / Selection-Rationale Firewall**。

它们只对 Human、Orchestrator、授权 Chair 和后续 Audit 层可见，不进入 Representative runtime protocol，也不得在状态通知中向 Representative 解释。

Representative 只能获得自己下一阶段的程序身份和当前权限。

### B1. Drafting Alignment 基本单位

Drafting Alignment 使用被最终总则保留的 **atomic drafting items** 计分，而不是 token、句子长度或文风。

#### B1.1 试行粒度规则

状态：**TRIAL / 待 Think Tank 审阅**。Human 于 2026-09-18 授权先按本规则运行；
Think Tank 可在后续审阅中提出修订意见，但不得自行回写已经冻结的计分、身份转换或
会议历史。任何追溯调整仍须由 Human 明确重新召集或授权。

试行期间按下列机械单位计数：

1. D0 中每个明确编号的正式条款（例如“第一条”“第一条之一”）各为一个 item；
   条款内部的分项、句子和技术要求不继续拆分。若 D0 没有可识别的编号条款，则整个
   D0 作为一个 fallback item。
2. 普通 amendment 以实际独立表决的整体为一个 item，不因包含多个句子或分项而在
   表决后继续拆分。
3. 若冲突程序已经把 amendment 拆成独立接受表决的 fragment，则每个实际接受独立
   表决的 fragment 为一个 item；只采纳获通过的 fragment。多个冻结来源包含相同
   fragment 时，该 fragment 只计一次，所有可由 provenance 证明的来源提案人均登记
   为 co-direct authors，不由 Chair 选择其中一人。
4. 被拒绝、`REBASE_REQUIRED`、`SUPERSEDED`、未进入 D3 的内容和纯编辑变动不贡献
   分数；同一规范内容不得因重复出现而重复计数。
5. D0 编号条款以同一条款编号仍存在于 D3 作为试行期的机械 retention 判据。获采纳
   amendment/fragment 若其冻结 application 位于通向 D3 的不可变文稿链上，则试行期
   推定为 retained；这一推定及其可能遗漏的后续语义删除必须列入 Think Tank 审阅。
6. 对 D0/D1/D2 整体作出的正式 co-sponsorship，适用于该版本当时包含、并最终保留的
   atomic items；同一 Representative 对同一 item 跨窗口重复联署只计一次。直接作者
   不再从同一 item 获得 co-sponsorship 加分。
7. Chair/Orchestrator 只能依据冻结文本、表决、application、co-sponsorship ledger 和
   provenance 生成 item table；不得为改变得分而作新的语义拆分。来源、编号或表决
   对应无法验证时关闭式暂停。

当前规则：
- 初始草案中最终保留的 atomic item：direct author `+1`；
- 被采纳 amendment 中最终保留的 atomic item：direct author `+1`；
- 两者完全平权；
- 对他人条目正式 co-sponsor：每个最终保留 atomic item `+0.5`；
- 同一 Representative 对同一 item 不重复获得 direct + co-sponsor 双重分；
- 被部分吸收的 proposal/amendment 按实际被保留的 atomic item 数量分别计分；
- 未被采纳或未进入最终总则的 item 不贡献分数。

因此代表 r 的基本分数为：

`S_r = D_r + 0.5 * C_r`

其中：
- `D_r` = r 直接起草且最终保留的 atomic items 数；
- `C_r` = r co-sponsor、由他人直接起草且最终保留的 atomic items 数。

本试行规则允许计算 Drafting Alignment、转换 CONSULTATIVE 状态并指定 Primary
Drafter；其永久化与否仍由后续 Think Tank 审阅和 Human 决定。

### B2. Primary Drafter

Drafting Alignment 最高者成为 detailed clauses 的 primary drafter。

对 Representative 的通知只说明谁被指定为 Primary Drafter，不公开分数、排名或选择依据。

若最高分并列，由全体有资格 Representative 提交不得弃权的密封排序。第一偏好票先
确定两个决选席位；第二席位 cutoff 仍并列时，只对并列候选进行额外密封排序加赛。
前二候选随后进入全体 Representative 的密封二元表决。任何加赛 cutoff 或最终二元票
仍无法破平时，由 Chair 投一张公开决定票并给出简短公开理由。

候选资格、密封票和 Chair 决定票必须留痕，但 Drafting Alignment 分数、排名、初始
并列结构和选拔理由不得向 Representative 公开。Representative 只获知最终指定结果。

### B3. Consultative Conversion

设状态转换前有 N 名 Representative：

`k = floor(0.30 * N)`

最多将 k 名转换为 CONSULTATIVE，因此实际转换比例永不超过 30%。

按 Drafting Alignment 从低到高排序。

若第 k 名与第 k+1 名在 cutoff 上同分，则**跨越 cutoff 的全部并列 Representative 均保留为 ACTIVE**，因此实际转换人数可以小于 k。

若 `k = 0`，无人转换。

状态通知不得披露 Drafting Alignment、bottom 30%、cutoff 或“因表现较低而被转换”等信息。Representative 只收到新状态及该状态当前可执行的动作。

Consultative Representative：
- formal proposal：无；
- vote：无；
- procedural motion：无；
- advisory memorandum：有；
- minority report：有。

Advisory material 不直接进入 option set，也不触发表决或 backtracking；ACTIVE Representative 可以独立吸收并重新提交。

---

## B4. Procedural Horizon and Stage Freeze

Representative 不加载本完整协议。本协议为 governance-private 全局规范。

每个阶段只向 Representative 发布当前阶段的最小充分 runtime protocol。

阶段切换顺序：
1. 当前阶段所有提交冻结；
2. ballot / provenance / current text 完成后台处理；
3. Orchestrator 私下计算状态转换；
4. 仅向每个主体披露下一阶段完成当前任务所需的信息；
5. 推荐通过新 session / context reconstitution 进入下一阶段。

不得在当前阶段提前披露后续阶段的角色、选拔、特殊权限、Think Tank、Audit 或 Chair 的未来裁量。

---

## C. 细则阶段（Detailed Clauses）

### C1. 第一版
Primary drafter 独立产生细则第一稿。

### C2. ACTIVE Representatives
可增补、替代、提出新 option、指出冲突。

试行程序（Human 2026-09-18 授权，待 Think Tank 审阅）：C0 以二级数字编号
（例如 `1.1`、`2.4`）机械形成 initial clause docket；三级和四级编号是所属二级
clause 的内部验收要求，不自动成为独立 docket item。每名 ACTIVE Representative
对完整 C0 进行一次相互隔离的结构化审阅；窗口冻结前不公开任何审阅正文。提案按
明确的 target clause ID 自动归组，之后再按 Type I/II/III 规则形成 option set。

### C3. Clause Split
若一个 clause 捆绑多个可独立裁决命题，应允许 split motion。

正式支持阈值仍为 OPEN。

试行门槛：split motion 至少获得 `ceil(N_ACTIVE/4)` 名 ACTIVE Representative 支持。
当前 `N_ACTIVE=9` 时，门槛是至少 3/9。一个 Representative 对同一 motion 只计一次；
在支持窗口冻结前不得公开 partial support。永久门槛仍待 Think Tank 审阅和 Human 决定。

试行应用规则：达到门槛的 split motion 不改写已经冻结的 C0，而是产生带 provenance 的
重基 standing text 与 docket。拆分后的项目沿用原 ID 并附确定性字母后缀，例如
`2.2-A`、`2.2-B`；未受影响的 ID 不重新编号。既有审阅窗口不得重跑。若原聚合 clause
已有提案，必须先对每项提案形成显式重定向记录；不得把提案静默指派、复制或遗漏。
同一原 clause 若有多个已通过但彼此未裁定关系的 split motion，须先由 Chair 形成程序裁定。
上述应用规则与门槛同属试行，待 Think Tank 审阅。

### C4. Type I：单一方案
第一次：SUPPORT / OPPOSE；OPPOSE 需理由。

试行执行规则：细则第一轮密封票按每次最多 10 个 option set 形成不可变分片；一个分片
完成后立即私下落盘，中断恢复时保留完整分片，只重做缺失或不合法分片。所有 ACTIVE
voter 的所有分片完成并合并为完整票前，不公开票型、理由或 partial tally。此处的
`10` 是单次模型任务的输入分片上限，不是投票门槛。

理由采用最小充分原则：Type I `SUPPORT`、Type II 和 Type III 的选择均令 `reason=null`；
Type I `OPPOSE` 必须给出且只给出一条决定性缺陷短句，最长 240 个 Unicode 字符。该
`240` 字符是输出 schema 的接受标准，不是 token 预算。不得复述提案、扩写背景、讨论
程序或把密封投票写成官僚文书辩论。规则生效前已经完整落盘且通过原 schema 校验的整票
按 ballot atomicity 原样保留，不追溯重做。

>= 3/4：通过。

否则 proposer 阅读反对理由并决定是否修改，再进行第二次投票。

执行时，proposer 只收到本人提案的反对理由正文，不收到理由与 voter ID 的对应关系；其对
每项提案提交 `RETAIN` 或一份保持原 target 与 proposal type 的完整 `REVISE` 文本。全部
proposer 决定冻结后公开最终文本，再由所有 ACTIVE voter 提交第二轮 `SUPPORT / OPPOSE`
密封票。中断恢复保留已经完整落盘的 proposer 决定和第二轮个人票，不重做其他 option set。

第二次 > 1/2：通过；若 < 3/4，标记 contested/protective outcome。
未严格超过 1/2 时，该 Type I 提案形成终局否决，不写入后续草案；“终局完成”不等于每个
option set 都必须产生 winner。

### C5. Type II：两个方案
第一轮 A/B。

某方案 >= 3/4：直接通过。

否则所有 ACTIVE voter 获一次解释机会；第二轮可选 A/B 或提出 genuinely new option。

出现新 option 时，原始提交先保持密封。Chair 只按机械资格条件逐项检查：实质重复、
越出当前 option set 范围、无法形成可执行条文、包含多个不可分割方案，或仅做格式规范化
时是否改变原意。Chair 不得以实体优劣排除方案。

提案人可以接受不改变原意的规范化文本、撤回，或在需要重写时提交一次完整新文本。
第二次机械审查仍不合格即视为撤回。Chair 作机械排除时，提案人可以提出一次异议；
全体 ACTIVE voter 以绝对多数 `floor(N_ACTIVE/2)+1` 决定是否恢复资格。提案人的身份、
失败版本、审查轨迹和异议票保留在审计层；公开层只发布合格方案。

至少一个新 option 合格时，将原 A/B、所有合格新 option 与 `STATUS_QUO` 组成新的
Type III 集合；该重构流程禁止再次提出新 option，避免递归。所有新 option 均撤回或
被排除时，原 A/B 重新进行一次完整密封二元票，不沿用产生新 option 的第二轮票。

无新 option -> 得票高者通过；<3/4 时标记 contested。

### C6. Type III：三个及以上方案
multi-option -> top two -> Type II。

任何前二晋级 cutoff 并列都不得由 Human 代替 Assembly 选择实体方案，也不得重做已经
冻结的第一轮票。先保留所有票数严格高于 cutoff 的已取得席位方案；全体 ACTIVE
Representative 再只在 cutoff 并列方案中各投一张密封加赛票，按票数填满剩余席位。
不得弃权，也不提交理由。若剩余席位边界在加赛后仍并列，Chair 逐席投公开决定票并给出
简短公开理由。然后由最终前二进入正常 top-two runoff。

完整加赛票在中断恢复时保留，不完整或尚未提交的票重新征集；加赛窗口冻结前不得公开
partial tally。该规则覆盖二项和复杂多项 cutoff 并列，系统不得随机选择晋级者。

### C7. Backtracking
正常 Type III top-two runoff 不计 backtracking。

只有新 proposal 导致 option set 重开或已关闭决策重新竞争时计数。

同一 clause 达到 3 次 backtracking 后锁定 option set，不再允许新 proposal。

### C8. Motion to Suspend
ACTIVE Representative 可因：
- evidence insufficient；
- procedural contradiction；
- unsafe consequence；
- unresolvable ambiguity；
提出 suspension。

不得仅因不喜欢结果提出。

所需支持门槛仍为 OPEN。

试行门槛：Motion to Suspend 至少获得 `ceil(N_ACTIVE/3)` 名 ACTIVE Representative
支持；当前 `N_ACTIVE=9` 时，门槛是至少 3/9。支持窗口采用密封收集且不得显示
partial support。永久门槛仍待 Think Tank 审阅和 Human 决定。

Chair 只能：resume、request information、escalate human；不得代替 Assembly 选择实体答案。

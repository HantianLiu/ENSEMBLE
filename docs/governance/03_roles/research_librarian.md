# Research Librarian / Research Desk

状态：**CONFIRMED for v0.7.0**

## 1. 身份与边界

Research Desk 是 Librarian 体系下的共享 epistemic infrastructure（知识基础设施），
不是 Representative、独立决策主体或新增票席。它不提出方案、不参加联署、不投票、
不行使 veto，也不判断某名 Representative “是否正确”。

Representative 负责决定 *what should be believed, proposed, challenged, or rejected*；
Research Desk 负责维护 *what is known, how it is known, and where the evidence comes from*。

Research Desk 的模型不进入 Representative Registry、roster、分母、门槛或
Drafting Alignment。启用状态及运行参数在会议初始化时冻结；关闭时，会议行为与此前
版本一致。

## 2. 初始化参数

每场会议分别冻结以下输入控制参数：

1. `representative_reasoning_effort`：同一参数控制本场会议全体 Representative；
2. `chair_reasoning_effort`：只控制 Chair；
3. `research_enabled`：是否允许联网文献调研；
4. 仅当 `research_enabled=true` 时，必须另选 `research_model` 和
   `research_reasoning_effort`。

`reasoning effort` 是 API 调用的控制参数，不是对模型能力、输出质量或实际推理 token
数的测量值。`default` 表示不发送强度字段，沿用供应商/模型默认行为；显式档位必须经
provider-specific 映射转换为目标 API 支持的合法值。缺少映射时不得静默猜测或降级。

Research Desk 模型身份属于 identity-private 信息。公开会议清单只说明该能力是否启用。

## 3. 合法请求

Representative 在 Proposal、Challenge 和 Veto 阶段有权请求 Research Desk；Audit
阶段可把被标记为关键事实的 claim 提交复查。每次调用只接受一条明确、可验证的事实性
claim。允许的形式例如：

> 标准 capillary-wave analysis 是否要求界面可表示为单值高度场？

不得接受开放式立场委托，例如“帮我证明方案 A 错了”。Research Desk 必须先把请求
规范化为具体 claim 与可证伪的 verification question；无法规范化时应拒绝并保留记录，
不得把倡议性问题伪装为事实检索。

请求者、使用阶段、原始 claim、规范化 claim、cache reuse 和最终 evidence packet
引用均进入审计记录。请求者身份与使用阶段不进入公共 evidence packet。

## 3A. 请求并发与试行资源政策

当前试行期不设置每名 Representative 的累计请求次数上限，也不设置会议级 Research
Desk token、费用或查询次数上限。缺少预算上限是明确的运行政策，不得由实现自行补出
隐藏 quota。Research Desk 原则上使用轻量模型控制成本，但模型规模不得降低来源质量、
对抗性检索或结构化校验要求。

每个正式 Research Round 中，每名 Representative 获得一个密封 submission slot；`0` 条
明确表示 `NO_REQUEST`，且不得拆成多个 submission 绕过本轮上限。试行阶段按信息增量采用
分层上限：总则基础调研为每人 `0–4` 条；修正案冻结后的条件调研为每人 `0–2` 条；最终
批准前的 Veto 调研为每人 `0–2` 条；ACTIVE 细则审阅前为每人 `0–4` 条；细则提案或动议
冻结后的条件调研为每人 `0–2` 条。跨 Research Round 的累计请求次数不设上限。每轮上限
在首次执行时写入不可变 `round_policy.json`；本规则生效前已经开始且尚未发布的旧轮次保留
原 `0–4` 条上限，以免恢复时改变已有密封提交的有效性。

Research Round 按 release barrier 执行：所有 Representative 独立提交，窗口冻结前不得
看到其他人的请求；冻结后才统一规范化、去重和检索。所有需要形成的 evidence packet
完成后整批同时加入公共数据库，随后才开启对应的立场或决策提交窗口。不得滚动公开先
完成的 packet。相同或高度相似 claim 应先执行 freshness check，满足 freshness 条件时
复用公共 evidence packet，而不是重复联网检索。

同轮去重只在全部密封请求冻结并分别完成规范化后进行。规范化命题与实质 scope 产生的
完整 claim fingerprint 相同，才归入一个 audit-private dedup group，并只执行一次检索、
生成一个共享 packet。每个原始 `request_id → requester_id → dedup group → packet_id` 映射
必须分别保留在 audit-private 层，使每份请求都可追溯。Release gate 的 expected claim
集合使用去重后的 group ID，而不是重复的原始 request ID。

公共 packet 不得公开 dedup group 的请求数量、request ID 或 requester 身份；所有提出
等价 claim 的 Representative 在 batch 发布后都可引用同一个 packet，但重复提交不形成
流行度、支持度或议程重要性的公开信号。Fingerprint 不同的相似 claim 不自动合并；只有
另行形成 `EXACT_EQUIVALENT` 判定后才可合组，其他关系保持独立检索对象。

Release barrier 采用关闭式发布语义，但区分 claim 质量问题与执行基础设施失败。provider
不可用、配置缺失、持久化失败、事件链损坏或冻结状态不一致时，整个 Research Round 进入
暂停，所有 Representative 均不得进入后续实体窗口。单项 schema、来源对应、筛选完整性
或证据强度问题则只隔离具体 claim；已成功形成的 packet 继续暂存在治理私有 staging 区，
不提前进入公共数据库。恢复时先重新校验暂存 packet：校验通过的结果保留，只重做
`PENDING` 或因执行中断而未终结的 claim。若 `CLEAR` 共识门槛不足但 packet 仍有可用支持
证据，发布层将共识降为 `QUALIFIED`，并在来源资格满足时保留 `SOURCE_BACKED` knowledge
status；该降级写入事件链和 audit trace，不构成 QC 失败。只有无法形成自洽 packet 的
claim 才以 `QC_FAILED` 终结，不阻却会议。全部 claim 达到 `STAGED_PACKET`、
`REJECTED_NON_RESEARCHABLE` 或 `QC_FAILED` 之一后，staged packet 才作为一个 batch 同时
发布。

同一 release barrier 同时约束来源原文。正式 Research Round 中下载的 PDF、其他原始
文档、provenance record 和 manifest 在整轮完成前都只进入治理私有 staging 区；
不能通过文件名、论文标题或提前更新的公共文献包泄露部分检索结果。全部 claim 终结后，
evidence packet batch、完整 document batch 与 manifest 必须在同一次 release 中公开。
ZIP 仅在 Human 请求导出时生成，不长期保存；导出不得越过同一 release barrier。
恢复时对完整暂存文件按 SHA-256 复核并保留，只补失败或不完整
下载。

Human 明确单独执行的 `ensemble-v07 research-claim` 不属于密封 Research Round，没有批次内
信息不对称，因此其 packet 和文献包可以在单次请求完成后立即公开。但只要同一会议存在
已经开始而尚未发布的正式 Research Round，手动命令必须拒绝发布，并要求 Human 先通过
`ensemble-v07 run-general` 恢复该轮；这样手动路径不能绕过正式轮次的 release barrier。

规范化程序明确判定为不可研究、非事实性或不够具体的请求，以
`REJECTED_NON_RESEARCHABLE` 终结并保留拒绝理由；它不产生 packet，也不阻塞 release
barrier。`NO_REQUEST` 表示该 Representative 的密封 submission 已完成且没有 claim，
同样不构成失败。`QC_FAILED` 表示 Research Desk 未能形成通过质量控制的 packet，也不构成
`UNRESOLVED` knowledge status；三者均不得被伪装为得出了文献结论。

独立密封 submission 可以并行收集。调度器按 `provider:model` 建立独立 lane，并对每条 lane
应用“同时在途模型请求数”上限。若供应商模型目录明确返回该技术量，则可自动采用；否则
使用 Human 在 provider 或具体 model 上配置的上限；两者都没有时保守取 1。同一模型的
persona 仍按固定顺序进入队列，响应完成顺序不得改变最终冻结顺序。RPM、TPM、RPD 等
时间窗口吞吐量不得冒充同时在途请求数；系统也不得通过额外付费请求探测并发上限。

Research Round 按以下实体变化触发：

1. 每个新层级首次进入实质 Proposal 时完成一轮基础调研；同一层级后续迭代直接使用已
   公开数据库与上一轮条件调研，不在每次立场窗口前机械重复基础调研；
2. Proposal 或 Challenge 内容冻结后，若存在修正案、提案或动议，则在相关实体表决开启
   前完成一轮条件调研；
3. 最终批准前完成一轮 Veto 调研；
4. 若待决文本、option set 与关键事实自上一轮后均未变化，则直接冻结并复用已有公共
   evidence snapshot，不为同一内容重复启动检索；
5. 纯程序性分类、排程或表决不启动 Research Round。

每个实体窗口必须记录其使用的 evidence snapshot：包括截至哪个 Research Round、所含
packet ID 集合及冻结时间。后续新增 packet 不得静默改写已经关闭的窗口所依据的快照。

v0.7 的可执行状态机在下列已实现实体边界自动调用 Research Round：第一次总则立场前进行
基础调研；每轮修正案冻结后且进入联署/表决前进行条件调研；总则最终批准前进行 Veto
调研；ACTIVE 细则审阅前进行该层级的基础调研；细则提案/动议冻结后且进入支持或表决前
进行条件调研。D1→D2、D2→D3 立场窗口直接使用此前公开数据库；Primary Drafter 起草 C0
前也复用最终批准时的快照，不再机械启动重复调研。没有修正案或细则提案/动议的条件边界
不启动空轮。每个实际启动的边界使用稳定 `round_id`；相同边界恢复时必须读取原 submission、
normalization、dedup group、resolution 和 staged packet，不得重新收费或重新询问已经完整
完成的项目。

会议运行结果中的 `research_round_count` 是已经跨过公开门闩的正式调研轮次数；
`research_evidence_packet_count` 是这些公开快照引用的不重复 evidence packet 数。二者均从
meeting-local 的持久化快照派生，是运行状态计数，不是检索质量、共识强度或事实正确率。

实现以 `public/research/rounds/<round_id>/evidence_snapshot.json` 作为 Representative 可见性
门闩。单项成功结果先保存在 `governance_private/research/rounds/<round_id>/staging_meeting/`；
只有 release gate 中全部去重 claim group 达到 `STAGED_PACKET` 或
`REJECTED_NON_RESEARCHABLE` 后，才复制 packet/原文、重建会议文献包并最后写入 snapshot。
后续 Representative runtime 只以已经存在的 snapshot 作为公开派生来源，不扫描 staging
或未发布的 packet；单次调用从这些 snapshot 形成有界相关性视图，不再逐字拼接全部历史
snapshot。完整 snapshot 仍是公开权威记录，压缩不改变 packet 的公开性或按需访问权。
崩溃发生在公开复制过程中时，恢复程序按内容校验已复制文件并补齐剩余文件，仍以 snapshot
是否存在判定该轮是否已经公开。

Provider 自身的 rate limit、并发限制或临时不可用仍按 provider failure/retry 规则处理；
它们不构成制度上的 Research Desk 预算上限。未来若引入会议预算，必须由 Human 明确
批准并另行规定关键事实达到上限后的处理方式。

检索执行采用受限并行。彼此独立且使用不同配额/网络路径的 retrieval backend 可以并行；
Tavily 的 supporting、contradictory、limitations 和 alternatives 四类独立查询可以并行。
OpenAlex 的单个 claim 内四类查询保持串行，但不同 claim group 可以形成受限流水线；
其同时在途 HTTP 请求数由独立 OpenAlex 上限控制，以降低共享限流配额下的 HTTP 429 风险。
去重后的独立 claim group 可以并行推进，worker 数、OpenAlex 请求数、Tavily 请求数、原文
下载数和 Research Desk 模型调用数分别受独立上限约束，不得用其中一个上限替代其他资源
的容量控制。并行只改变请求的墙钟时间，不改变制度顺序：候选来源、查询轨迹和审计记录
必须按配置的 backend 顺序及上述四类查询的固定顺序合并和落盘；每个完整 group 可以立即
进入治理私有 staging，但最终 resolution、evidence snapshot、文献 manifest 和 ZIP 仍按冻结
group 顺序在整轮 release barrier 一次公开。不得因某个先返回的查询而提前公开局部结果。

## 4. 对抗性检索义务

每个重要 claim 的默认检索目标至少包含：

- supporting evidence；
- contradictory evidence；
- scope limitations；
- canonical alternatives。

检索不得只为原主张寻找支持。未发现反证时可以记录 `none found`，但必须同时记录检索
范围与覆盖限制；`none found` 不等同于“反证不存在”。来源优先级原则上为原始论文、
正式标准、官方技术资料和高质量综述，并明确标记二手或低可信来源。

来源选择必须匹配 claim 类型：学术 claim 优先使用原始论文、原始数据和高质量综述；
标准或方法 claim 优先使用正式标准、专业学会或维护者文件；软件、API 或产品 claim
优先使用官方文档、官方 changelog 和原始 repository；时效性事实优先使用监管机构、
直接发布者或其他当前的一手来源。预印本、技术报告和二手资料可以补充检索，但必须
明确标记其证据限制，且在存在可获得的权威来源时不得静默替代权威来源。

每个来源必须按它对当前 claim 的用途标记为 `AUTHORITATIVE`、`PRIMARY`、`REVIEW`、
`PROVISIONAL` 或 `DISCOVERY_ONLY`。该标记描述来源在本次 claim 中的证据资格，不是对
来源作脱离语境的永久评级。普通网页、搜索结果摘要和其他 `DISCOVERY_ONLY` 材料只能
用于发现来源，不能独自把 claim 升级为 `SOURCE_BACKED`。`SOURCE_BACKED` 至少要求一项
`AUTHORITATIVE`、`PRIMARY` 或 `REVIEW` 来源；权威材料无法访问或只能由低一级材料间接
转述时，应标记为 `QUALIFIED` 或 `UNRESOLVED`，不得推定权威材料支持该 claim。

对冲尝试是每条 claim 的强制完成条件。Retriever 至少必须分别执行并在审计轨迹中证明：

1. 支持原 claim 的检索；
2. 直接反证或相反结果的检索；
3. 失效条件、边界条件和适用范围限制的检索；
4. 能解释同一现象的 canonical alternatives / competing explanations 检索。

缺少任一类非空查询时，该 Research Desk 请求不得标记为完成，也不得升级为
`SOURCE_BACKED`。公共 packet 必须包含 `counter_search_summary`；没有找到反证时应明确
写 `none found` 并说明数据库、关键词范围与资料覆盖限制，不得据此宣称反证不存在或
文献已经一致。

参考实现采用两级结构：独立 retriever 完成搜索与元数据召回，Research Desk 模型完成
相关性判断、证据抽取、适用范围分析和结构化摘要。Retriever 和摘要模型都不得获得
表决权。

Retriever 必须采用可替换的多后端路由，而不是把某一商业搜索供应商写入制度。Claim
规范化时分类为 `ACADEMIC`、`STANDARD_METHOD`、`SOFTWARE_API`、`CURRENT_FACT` 或
`GENERAL`，并记录路由理由：

- 学术 claim 使用学术索引，并追踪到出版者、repository 或公开全文；
- 标准/方法 claim 优先检索标准组织、专业学会或维护者官方来源；
- 软件/API claim 优先检索官方文档、changelog 和原始 repository；
- 时效性事实优先检索监管机构或直接发布者，并以独立 Web 搜索执行对冲；
- general claim 使用通用 Web 搜索，同时继续执行四类对抗性查询。

当前实现按 Human 在每场会议初始化时选择的引擎路由：学术索引为 OpenAlex；通用搜索可选
Tavily、Parallel 或禁用，新会议默认禁用通用搜索。学术 claim 不因元数据为空、原文不可读
或连接故障而自动调用付费通用搜索；限额或连接故障回退须有 Human 明确授权，且仅限所选
供应商，授权不随供应商切换自动转移。通用 claim 使用已选择并允许的通用引擎。
实际被路由调用的引擎仍必须分别执行 supporting、contradictory、limitations 和 alternatives
四类非空查询，不要求健康的学术检索同时调用付费网页引擎。Fast 探索计划漏填来源类型时
默认 ACADEMIC；非学术类型须明确指定。Parallel 只接受 fast／turbo，每次结果上限 1–10，
必须将上限传给服务端，不得靠丢弃超额结果规避计费。不得自动调用 Parallel 付费 Extract；
Tavily 付费 Extract 默认关闭，只有明确配置启用时才用于非学术网页。论文原文补找优先使用
学术元数据。任何引擎的排序、分数与搜索摘录只是候选信息，不是 evidence status。

Tavily／Parallel 候选必须保留标题、原始 URL、返回摘要、查询目的和 provider request ID（若 API
提供）。公开可直接访问的 URL 可由文献包下载器按原始响应归档；无法取得可读原文时仍按
既有规则降为 `PROVISIONAL`。API key 只从环境变量或 Human 明确配置的本地 secret file
读取，不得写入会议文档、事件、检索轨迹或公共 packet。

OpenAlex 生产检索应使用账户 API key，认证信息只通过 Authorization header 发送，不得放入
URL。每次响应的每日 credit 上限、剩余 credit、本次消耗和距 UTC 重置秒数进入 audit-private
query trace，但不进入 Representative prompt。匿名访问只作为密钥缺失时的显式诊断状态，
不得被误报为已认证检索；429 必须尽可能区分每日预算耗尽与瞬时请求速率限制。

每个 packet 必须公开 `source_domain` 与实际使用的 `retrieval_backend_ids`。仅由 OpenAlex
完成的检索只能证明学术元数据和其覆盖到的摘要/开放链接，`coverage` 必须为 `LOW`；它
不能被表述为已经覆盖标准、官方文档、软件资料或整个 Web。搜索供应商只负责发现候选
来源，不产生 evidence status 或会议判断。

## 5. Evidence packet

每个完成检索、通过 schema 与来源对应校验的 evidence packet 应立即加入本场会议的
公共证据数据库。所有 Representative 在所有会议阶段均有读取、检索和引用该数据库的
权利；不设置仅请求者可见期，也不因阶段切换撤回已经发布的 packet。正在检索的中间
结果不得进入公共数据库。

公共证据数据库严格限定于当前会议，保存在当前 meeting root 下；本版本不建立安装级、
用户级或跨会议 evidence cache。其他会议不得自动发现或复用本会议的 packet、claim
fingerprint 或文献包。

公共 evidence packet 至少包含：

- 原始 claim 与规范化 claim；
- verification question 与实质 scope terms；
- 检索资料范围；
- 可追踪来源元数据；
- 每项来源针对当前 claim 的 evidence use class；
- 主要支持证据；
- 主要反证或异议；
- 适用条件与 scope limitations；
- canonical alternatives；
- 对冲检索摘要；
- 证据之间是否存在真正冲突；
- 文献共识状态；
- 未解决问题；
- 检索覆盖、来源质量和文献一致性的 confidence；
- 当前 knowledge status。

正式 Research Round 中，上述 claim 字段与其他 packet 内容一样受 release barrier 约束：
整轮完成前不公开，整批发布后对本会议所有 Representative 和 Human 公开。公开 claim 不
同时公开 requester 身份、request ID 或同一 fingerprint 的请求次数；这些映射继续只存在
于 audit-private 层。Claim 可见性本身不形成支持、联署或议程优先级。

Confidence 只描述 retrieval coverage、source quality 和 literature consistency，不得写成
“某名 Representative 正确的概率”，也不得代替会议的实体判断。

Confidence 必须保留为三个相互独立的描述性维度，每个维度分别使用 `HIGH`、`MEDIUM`
或 `LOW`：

- `coverage`（检索覆盖度）：衡量数据库、查询词、来源类型以及四类对冲检索的覆盖情况；
- `source_quality`（来源质量）：衡量来源的权威性、原始性、审查状态和正文可访问程度；
- `literature_consistency`（文献一致性）：衡量同一实质适用范围内合格来源之间的一致程度。

不得计算或展示 aggregate confidence score，也不得把任一维度解释为 claim 正确的概率。
三个维度不得彼此自动推导，也不得由 knowledge status 或 literature consensus 机械推导。
例如，一篇合格的原始论文可以使 claim 达到 `SOURCE_BACKED`，但由于检索范围窄，
`coverage` 仍可为 `LOW`；`MIXED` 表示存在实质文献冲突，也不自动决定 `coverage` 或
`source_quality` 的档位。

会议讨论中凡使用公共证据数据库支持事实性陈述，输出必须保留 evidence citation，至少
包含 `packet_id`、一个或多个 `source_id`，并区分 `DIRECT_SOURCE` 与
`REPRESENTATIVE_INFERENCE`。引用 packet 不得被改写成来源直接说过 packet 中没有记载的
内容。该结构化引用随发言和会议记录保存，使 Human 能从发言追踪到 finding、来源元数据
与原始文档。

Literature consensus 与单条 claim 的 knowledge status 必须分开判断：

- `CLEAR`：高质量综述或正式共识来源明确支持，或者多个真正相互独立的合格来源一致，
  且不存在同一适用范围内尚未解释的关键反证；
- `QUALIFIED`：文献总体方向一致，但结论只在明确条件下成立，或者存在不改变主要方向的
  次要分歧；
- `MIXED`：合格来源在同一实质适用范围内同时存在 supporting 和 contradictory findings；
- `INSUFFICIENT`：来源数量、来源独立性、来源质量或检索覆盖不足以判断领域共识。

一篇原始论文可以直接支持 claim，因而使其达到 `SOURCE_BACKED`，但单篇原始论文通常
不足以把 literature consensus 标为 `CLEAR`。这种情况下应发布
`SOURCE_BACKED + QUALIFIED`，而不是丢弃整个 packet。实现只能机械检查最低来源结构；来源是否
来自独立团队、独立数据或独立方法，必须由 Research Desk 明确说明并保留在审计记录中，
不得把简单的文献数量误当作独立性。

## 6. Knowledge status

制度区分：

- `MODEL_PRIOR`：仅来自模型参数内部记忆；
- `SOURCE_BACKED`：有可追踪外部来源直接支持；
- `QUALIFIED`：有来源，但适用范围或条件限制影响原 claim；
- `INFERENCE`：由已列证据推导，来源没有直接陈述；
- `ASSUMPTION`：为推进分析而显式采用、尚未验证；
- `UNRESOLVED`：检索不足、资料冲突或无法形成可靠结论。

Research Desk 对三个检索结果状态采用以下判定边界：

- `SOURCE_BACKED`：至少一项具备证据资格的来源直接支持原 claim；packet 必须列出对应的
  supporting finding；
- `QUALIFIED`：来源只支持附加条件后的较窄 claim，或者当前可获得的材料主要属于
  provisional evidence；packet 仍必须列出来源和 supporting finding，并明确限制条件；
- `UNRESOLVED`：直接证据不足、同一适用条件下存在尚未解释的关键冲突，或者检索覆盖
  不足；packet 必须列出至少一个 unresolved question。

出现 contradictory evidence 不自动产生 `UNRESOLVED`。若支持与反证适用于不同条件，
Research Desk 应在 applicability、scope limitations 和 conflict assessment 中分离这些
条件，并可将结果标为 `QUALIFIED`。只有同一实质范围内的冲突尚不能解释时，才构成
`UNRESOLVED` 的理由。该分类是证据状态，不是会议对 claim 真假的裁决。

普通讨论可以继续使用 `MODEL_PRIOR`。若外部事实对 Proposal 有实质影响、构成 Veto
依据、受到正式 Challenge，或被 Audit 标记为关键事实，则应允许或要求 Research Desk
把其更新为 `SOURCE_BACKED`、`QUALIFIED` 或 `UNRESOLVED`。状态更新不是投票结果。

“必须检索”的程序要求只要求形成完成的 evidence packet，不要求检索结果支持原 claim。
`SOURCE_BACKED`、`QUALIFIED` 和 `UNRESOLVED` 均不自动决定 Proposal、Challenge 或
Veto，不自动改变表决门槛。正式 Challenge/Veto 必须引用 packet ID，并区分来源直接陈述
与 Representative 推论；`MODEL_PRIOR` 或 `ASSUMPTION` 不得单独作为正式 Veto 的事实
依据。packet 完成前动作保持 `EVIDENCE_PENDING`；完成后即使为 `UNRESOLVED` 也可继续，
但必须显式保留未解决标记。

## 7. 共享、缓存与 freshness

所有 Representative 调用同一 Research Desk 服务，但每个请求使用独立查询上下文。
同一或高度相似 claim 使用规范化文本与 scope 生成 claim fingerprint。已验证 packet
可供其他 Representative 引用；复用前必须执行 freshness check。

自动 cache reuse 只允许规范化命题和实质适用范围均为 `EXACT_EQUIVALENT` 的 claim。
Fingerprint 完全相同可作为这一关系的机械依据；高度相似但 fingerprint 不同的 claim
不得静默复用，Research Desk 必须显式记录两者属于 `EXACT_EQUIVALENT`、`NARROWER`、
`BROADER`、`OVERLAPPING` 或 `DISTINCT`，并说明理由。只有 `EXACT_EQUIVALENT` 允许自动
复用。较窄 packet 可以作为较宽 claim 的相关证据被引用，但不能单独使较宽 claim 达到
`SOURCE_BACKED`。

Freshness check 未通过时必须形成新 packet；旧 packet 保持不可变，新 packet 使用
`supersedes_packet_ids` 指向其直接前版。Supersession 只影响未来窗口选择哪个当前 packet，
不删除旧版本，也不追溯改写已经冻结窗口的 evidence snapshot、立场、表决或决定。

Freshness 使用三类会议配置输入参数：

- `VOLATILE = 7 days`：时效性事实、价格、政策状态和在线服务当前行为；
- `VERSIONED = 30 days`：标准、软件、API 和持续维护的正式文档；
- `STABLE = 180 days`：基础理论、历史实验结果和通常缓慢变化的学术事实。

Research Desk 在 claim 规范化时记录 `freshness_class` 和判定理由；不确定时采用适用类别中
更短的期限。每个 packet 的缓存到期时刻为 `retrieved_at + maximum_cache_reuse_age`，三者
均写入 packet。`retrieved_at` 是检索与综合完成的 UTC 时间，不是论文发表、会议开始或
再次引用的时间；复用 packet 不重置计时。Freshness window 只控制能否自动复用缓存，
不表示事实或证据在该期限内必然正确。撤稿、更正、官方更新、版本变化或来源失效一经
确认，应立即使相关 packet 对未来 cache reuse 失效，不等待期限届满。

Research Desk 自动失效必须基于可追溯的 `RETRACTION`、`CORRECTION`、
`OFFICIAL_UPDATE`、`VERSION_CHANGE` 或 `SOURCE_UNAVAILABLE` 信号，并保存 evidence URL
和理由。Representative 可以提出带理由的重新检查请求，但不能直接写入 invalidation；
Chair 不作事实判断，不能自行宣告 packet 失效。Human 可以通过不可变 ruling 手动强制
失效。每条失效记录保存在本会议公共 cache-invalidation ledger，并进入事件链。

重新检查使用 `force_refresh` 绕过仍在 freshness window 内的旧 packet，执行完整对抗性
检索并生成新 packet。新 packet 的 `retrieved_at` 是本次重新检查完成时间，其
`cache_expires_at` 从该时刻按新分类重新计算，因此有效缓存期限被重置；新 packet 通过
`supersedes_packet_ids` 指向旧 packet。失效与重新检查都只影响未来 cache reuse，不追溯
修改已冻结 evidence snapshot、发言、表决或决定。

## 8. 信息隔离与审计

Representative 在所有阶段均可访问公共 evidence packet 数据库，但不看到查询数量、被丢弃候选、
搜索路径或中间筛选轨迹。完整原始请求、查询策略、候选结果、筛选依据、cache decision
和最终输出保存在 `audit_private/research/`；模型调用原文仍保存在治理私有交换记录中。

Audit Meeting 可检查 cherry-picking、反证遗漏、不当来源筛选和 freshness 失效。审计
访问不会把私有检索轨迹自动回流到后续 Representative runtime。

## 8A. Human 文献包与原始文档

每场启用 Research Desk 的会议维护一个 Human 可按需打包下载的会议级文献目录：
`public/research/literature_bundle/`。至少包含 `manifest.json`、说明文件以及
`documents/` 中依法公开取得的原始文档。特别是 retriever 提供合法公开 PDF URL 时，
系统必须下载原始 PDF，不作内容改写，并记录：

ZIP 是可重建的导出副本，不是唯一的原文保管载体；研究刷新与精简归档不长期保留 ZIP。
Human 可用 `ensemble gather` 导出文献 PDF ZIP。清理旧归档的 ZIP 前必须验证原归档与
全部 ZIP 内容，保存任何独有文件或不同版本，并追加可核对的维护记录；不能删除原文件、
改写冻结归档清单，或借缓存清理绕过接续的完整性校验。

- 对应的 `packet_id` 与 `source_id`；
- citation URL、原始文档 URL 和最终解析 URL；
- 标题、作者、发表年份和 DOI；
- 获取时间、media type、文件字节数和 license（若来源报告）；
- meeting-local 文件路径与内容 SHA-256。

Manifest 同时列出无法归档全文的来源。系统不得绕过登录、付费墙或访问控制；没有合法
公开全文 URL 时标记 `NOT_AVAILABLE`，下载失败或格式不受支持时分别明确记录状态，不能
把 landing page、搜索摘要或空响应伪装成论文原文。默认
`max_source_document_bytes = 50,000,000 bytes` 是单个文件下载的技术安全上限，不是会议
研究预算；它可以通过配置调整。

只有摘要、搜索片段或书目元数据而没有可读取原文的来源，必须标记为 `PROVISIONAL`，
不能单独作为 `SOURCE_BACKED` 的直接依据。若 Research Desk 模型基于此类材料请求
`SOURCE_BACKED`，发布层必须在 packet 公开前将 knowledge status 降为至多 `QUALIFIED`，
并在 `full_text_access_assessment` 中记录已归档原文数量和限制。该降级是确定性的访问
资格校验，不是对论文结论真假的判断。Packet 仍可发布，来源链接和不可用状态继续进入
文献包，因此全文不可访问本身不阻塞会议。

文献包属于当前会议，Human 可下载阅读，但原始 PDF 不自动灌入每名 Representative 的
模型上下文。Representative 使用结构化 evidence packet 和 citation；完整检索轨迹仍按
既有规则留在 audit-private 层。

## 9. 失败语义

失败分为质量控制失败与执行基础设施失败。schema 经一次定向修复仍无法验证、来源引用
无法对应、对抗性检索轨迹不完整或筛选 ledger 无法闭合，属于单项 claim 的质量控制失败：
写入 `QC_FAILED`，拒绝相关证据升级，保留原始响应和校验理由，但不得暂停 Research Round、
会议或后续 Proposal、Challenge、Veto。若唯一问题是模型把文献共识写成过强的 `CLEAR`，
而 packet 的来源和 supporting finding 仍自洽，则发布层必须降为
`SOURCE_BACKED + QUALIFIED`（或在来源资格不足时为 `QUALIFIED`），并记录
`RESEARCH_PACKET_CONSENSUS_DOWNGRADED`；这不是 `QC_FAILED`。只有无法形成自洽 packet 的
claim 才写入 `QC_FAILED`，此时才不产生 evidence packet，也不得伪装为
`SOURCE_BACKED`、`QUALIFIED` 或 `UNRESOLVED` 文献结论。

QC 必须采用最小隔离范围。若缺陷可以定位到单个候选来源、finding、screening row 或重复
候选记录，只隔离该条目及其依赖引用，随后重新验证剩余 packet；剩余 packet 仍满足 schema、
知识状态和共识约束时应正常发布。只有条目隔离后已无法形成自洽 packet，或整个输出无法解析
到足以识别具体缺陷时，才把该 claim 标为 `QC_FAILED`。某个 claim 的 `QC_FAILED` 仍不阻止
同一 Research Round 的其他 packet、文献原文和 manifest 整批发布；公共快照将整轮标记为
`PARTIAL_WITH_QC_EXCLUSIONS` 并给出被隔离 claim 数，不把部分发布伪装成无缺陷完成。

单个检索后端暂时不可用时也采用最小隔离范围：若至少一个其他已配置、且本会议允许并授权
用于本次路由的后端仍能完成
supporting、contradictory、limitations 和 alternatives 四类对抗检索，则记录失败后端，使用
剩余后端继续，并把公开 packet 的 retrieval coverage 强制降为 `LOW`；不得仅因 OpenAlex、
Tavily 或其他单一后端失败而暂停会议。只有所有检索后端均不可用、模型供应商在规定重试后
仍不可用、Research Desk 配置缺失、持久化写入失败、事件链或冻结状态不一致时，才属于无法
安全降级的执行基础设施失败，可以暂停会议并请求 Human 介入。两类失败必须使用不同状态和
审计事件，不得以“QC”名义吞掉基础设施故障。

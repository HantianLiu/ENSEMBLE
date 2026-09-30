# Changelog

## v0.7.0 release — 2026-09-22

- Added the independent `project-ensemble-v07` distribution and `ensemble-v07`
  command. v0.6-dev source and command entry points are not overwritten.
- Added portable `model_config.toml` provider definitions, environment-variable
  and non-executing assignment-file credential lookup, packaged governance data,
  immutable configuration snapshots, software/governance version pinning,
  root-level original prompts, and meeting lineage.
- Implemented sealed Primary Drafter selection for every two-or-more-way tie,
  generic Type III advancement-cutoff runoffs with a public Chair fallback, and
  the non-recursive Type II genuinely-new-option reconstruction procedure.
- Added a deliberately non-mutating `run-audit` interface. The full Audit and
  Correction state machines remain deferred and fail fast instead of being
  misrouted through deliberation.
- Added `ensemble-v07 migrate-v06` for a verified, copy-on-write import of
  unfinished v0.6 meetings. It preserves frozen artifacts and the historical
  event chain, then pins v0.7.0 metadata only in the new directory. Audit
  meetings remain unsupported.

## 0.7 scholarly-rendering draft

- Added the separately initialized scholarly-rendering derivative publication
  procedure, including two-round science and citation bookkeeping, Research
  Desk verification for proposed scientific corrections, Human resolution of
  failed science ballots, and immutable source preservation.
- Added resumable integrity exits for citation anchors, incomplete citation
  vote/application contracts, changed frozen verification inputs, and final
  numeric-reference mismatches. Reviewer lanes now use the effective runtime
  after Ctrl+R replacement, so converging replacements share one concurrency
  limit. Citation evidence injection is bounded to 300 packets and 60,000
  serialized characters.

## v0.7.0 current clarifications

- 裸 `ensemble-v07` 现在进入统一首页，可新建会议、恢复未完成会议，或从已完成会议接续召开
  新会；也可用 `ensemble-v07 M-...`/`ensemble-v07 open <ID-or-path>` 直接打开。安装目录维护含会议
  ID、自然语言标题、会议类型和路径的索引。接续时由 Human 选择只继承证据包、只继承最终
  文书或两者同时继承；最终文书作为建议性上下文，不自动升级为 Constitution。旧的
  `run-general`、`run-report`、`run-research` 命令继续兼容。安全中断会原子覆盖当前目录的
  `resume_<ID>.sh` 与 `resume.sh`，新脚本统一调用 `ensemble-v07 open`。
- 补齐 OpenAI-compatible 模型目录未报告 context length 时的逐模型容量：当前 DeepSeek
  长上下文模型、Kimi K3 和 GLM 5.2/5.3 按 1,048,576-token capacity 登记；80% 输入安全
  接受标准为 838,860 estimated input tokens。旧会议在保留原冻结快照的同时允许追加版本化
  容量升级记录，已存在的第一批升级不再阻止后续 GLM/Kimi 元数据生效。
- 中断恢复后的任务表现在同时重建完整批次：已落盘且通过完整性检查的结果直接显示为绿色
  “已恢复完整结果”，只有未完成项目保持灰色。瞬时 provider 故障仍采用最多 3 次网络重试；
  schema 修复从 1 次放宽为 2 次独立且可审计的尝试，避免恢复时反复读取同一份坏修复结果。
  Research Desk 拒绝非具体事实命题时不再终止报告会议：该问题退回原提问者、排到队尾，
  其他问题继续处理；修订问题最多重写 2 次，仍不合格时只淘汰该条并保留审计记录。

- 增加派生文献调研报告试行流程。新会议以只读方式接续源会议，复制公开 Research Desk
  evidence packets、公开调研轮次和文献包，并为每个文件记录来源路径、字节数与 SHA-256；
  新鲜的相同 claim 继续通过现有 fingerprint/freshness cache 复用。研究总纲由四人规划组
  （四种职位各一名、确定性轮换并尽量覆盖供应商）密封拆分，每份最多八个模块；Chair
  可追溯聚类后由全体代表进行限量结构审阅，再冻结总纲。冻结后按模块串行执行两轮
  Research Desk 问答、证据 dossier、Builder 初稿与三 office 审阅、全体两轮限量审阅、
  3/4 确认及争议意见保留；最后由随机智库长综合，全体审阅一次，无门槛发表。事实性
  反对意见与主席可读性 patch 均由智库长逐条严格多数审核，并出版 Markdown、PDF、
  参考文献、未解决附录、审计清单与文献包。
- 同一文献调研报告流程允许从零开始；此时不要求源会议，系统冻结 `FROM_SCRATCH`
  来源记录，将继承 evidence packet 数量明确记为 0，并从空的会议级 Research Desk
  证据库进入四人研究规划阶段。

- 会后代表密封提交不再重复注入最终报告中体量巨大的文献证据/参考文献附录。系统冻结一个
  带源文件与视图 SHA-256 的派生上下文，保留认证正文、智库审查附录和完整性信息，仅从
  模型 prompt 省略已有独立文献包承载的附录 C；完整 Markdown、PDF 和文献包不变。
- 固定规模工作窗口改用原地刷新的双列表式进度：灰色表示等待、白色表示运行、绿色表示
  完成、红色表示失败，并显示完成数/总数；重试、token 用量和 cache hit rate 更新在对应
  任务行内。OpenAI-compatible 与 Gemini 的流式 transport 只作为活性信号，不逐条打印
  片段，也不显示或保存隐藏 CoT；重定向输出不包含中间流事件或 ANSI 光标控制字符。
- Chair 程序认证包现在纳入 Type I 冻结修订、Type I 第二轮结果、Type III 第二晋级席位
  加赛政策及结果、解释轮和 motion 结果。每次认证冻结输入文件清单、字节数与 SHA-256；
  已认证结果继续保持终局，非认证结果仅在证据快照改变时追加版本化复审，原报告不覆盖。
- Type III 的任意前二 cutoff 并列均由全体 ACTIVE voter 进行密封加赛；先保留严格高于
  cutoff 的方案，再按加赛票填满剩余席位。加赛边界仍并列时由 Chair 逐席公开投决定票，
  已冻结首轮票保持不变，系统不随机选择实体方案。
- Representative 的 Research evidence 注入改为确定性的有界相关性视图：完整公开数据库与
  文献包保持不变，单次调用最多注入 16 个有效 packet 和 64,000 个 Unicode 字符，并明确
  记录省略数。完整 snapshot 的派生读取与实际释放片段分别进入文件访问审计链。
- 新增独立的 provider input-context 预检：按会议冻结模型公布容量的 80%，未公布容量时使用
  262,144 个估算 input tokens；估算采用 `ceil(UTF-8 bytes / 3) + 512`。经过有界 evidence
  组装后仍超限则在发起 API 调用前暂停，不静默裁剪治理规则、当前议题或表决内容。
- 最终 Markdown/PDF 新增可审计的编号制学术引文附录：按 evidence packet 的支持、反证、
  适用限制和替代解释 finding 插入 `[n]`，按 DOI（无 DOI 时按 URL）去重生成参考文献表，
  并在出版 manifest 冻结 packet 集合哈希和引用计数。系统不凭文本相似度把文献强配给
  规范性条文。
- 修正 Research Round 中断恢复时的 literature bundle 冲突：`manifest.json` 与
  `README.md` 是可由不可变来源记录重新生成的视图，不再从较旧的公共目录反向覆盖
  staging；evidence packet、来源记录和原始文档仍逐字节校验，真实内容冲突继续拒绝。
- Research Round 入口按信息增量收缩：仅第一次总则立场前执行基础调研，D1→D2 与 D2→D3
  直接复用公共证据库；保留修正案冻结后的条件调研、最终批准前的 Veto 调研，以及细则
  审阅前/冻结后的两轮。基础调研与细则审阅前每人最多 4 条 claim，条件与 Veto 调研最多
  2 条；每轮上限冻结，已经启动的旧轮次按 4 条兼容恢复。
- 去重 claim group 改为受限并行流水线；claim worker、OpenAlex、Tavily、原文下载和
  Research Desk 模型分别执行独立并发上限。完整 group 立即私下落盘，但 evidence snapshot、
  manifest 与 ZIP 仍等待整轮 release barrier，并保持冻结顺序。
- Human-facing runtime labels are now bilingual office titles: `Builder / 建构者`,
  `Monitor / 监管者`, `Cartographer / 制图者`, and
  `Librarian / 智库长`. The persisted persona codes and prompts are unchanged;
  this is a presentation-layer change only.
- ENSEMBLE has no client-side per-call output-token limit by default. A positive
  `governance.provider_output_token_limit` or one-run `--max-output-tokens` explicitly opts
  into a limit; otherwise the provider's own model ceiling applies.
- Research Desk distinguishes `knowledge_status` from literature `consensus`. A model request
  for `CLEAR` that lacks the required authoritative/review or independent-source support is
  downgraded to `QUALIFIED` while retaining a usable `SOURCE_BACKED` packet when its source
  qualification permits. The downgrade is recorded as
  `RESEARCH_PACKET_CONSENSUS_DOWNGRADED`; only an irreparable packet becomes claim-level
  `QC_FAILED`.

## v0.7.0

- 修正 Think Tank 审查 schema 的内部冲突：`reconvene_worthy=false` 时允许保留不建议重开
  的理由；`true` 时仍强制要求理由，旧的 `false + null` 审查继续有效。该修正使无损格式
  修复不再为了满足互相矛盾的字段约束而删除实质理由。
- 所有模型 provider 的单次响应超时输入控制参数统一上限为 1,200 秒（20 分钟）；允许配置
  更短时间，但拒绝超过该上限。超时不包含重试退避，也不改变 token 预算。
- 细则第一轮密封票改为每次最多 10 个 option set 的不可变分片；恢复时保留完整分片并只补
  缺失部分。新分片除 Type I `OPPOSE` 外一律不写理由；`OPPOSE` 理由限一条决定性短句、
  最长 240 个 Unicode 字符。规则生效前已经完整落盘的合法整票原样保留。
- 会议初始化分别冻结全体 Representative 与 Chair 的 `reasoning effort` 输入控制参数；
  `default` 不向 provider 发送控制字段，显式档位必须经过 provider-specific 合法映射。
- 增加可选 Research Librarian / Research Desk。启用时独立选择其模型与 reasoning effort；
  关闭时维持 v0.6 行为。Research Desk 不进入 roster、票数分母或 Drafting Alignment。
- Research Desk 对单一可验证 claim 执行对抗性检索，明确覆盖 supporting evidence、
  contradictory evidence、scope limitations 和 canonical alternatives，并输出结构化
  evidence packet。
- 检索层按 `ACADEMIC`、`STANDARD_METHOD`、`SOFTWARE_API`、`CURRENT_FACT`、`GENERAL`
  路由到可替换的学术、官方与通用 Web 后端；packet 公开实际 backend ID。仅 OpenAlex
  检索的 coverage 固定为 `LOW`，不得伪装成全来源覆盖。
- v0.7 试行后端固定为 OpenAlex primary 与 Tavily supplemental；两者分别执行四类对抗性
  查询。Tavily 的摘要、分数和排序只用于候选召回，来源 URL 与 provider request ID 进入
  审计轨迹；密钥只从环境或显式本地 secret file 读取，不进入会议产物。
- OpenAlex 从匿名访问升级为可配置的 Bearer API-key 认证；本地配置使用
  `OPENALEX_API_KEY` assignment file。Audit-private query trace 记录每日 credit 上限、剩余
  credit、单次消耗和 UTC 重置倒计时，429 错误报告额度状态且不泄露 key。请求同时改用
  官方 `per_page` 参数和字段级 `select`，减少无关响应数据。
- 每条 claim 强制执行可审计的对冲尝试；四类查询缺少任一项时不得完成 packet。
  未发现反证必须公开记录 `none found` 与覆盖限制，不得解释为反证不存在。
- 来源按 claim 类型优先采用原始研究、正式标准、官方资料或高质量综述，并按本次用途
  标记 evidence use class；仅有普通网页、搜索摘要或其他 `DISCOVERY_ONLY` 材料时不得
  将 claim 标记为 `SOURCE_BACKED`。
- 引入 `MODEL_PRIOR`、`SOURCE_BACKED`、`QUALIFIED`、`INFERENCE`、`ASSUMPTION`、
  `UNRESOLVED` knowledge status。
- 固定 Research Desk 状态边界：直接合格来源支持为 `SOURCE_BACKED`，仅在较窄条件或
  临时材料下获得支持为 `QUALIFIED`，证据不足、同范围关键冲突或覆盖不足为
  `UNRESOLVED`；不同适用条件下的反证不自动触发 `UNRESOLVED`。
- 将 literature consensus 与单条 claim 的 knowledge status 分离；单篇原始论文可以产生
  `SOURCE_BACKED`，但通常不足以产生 `CLEAR` 共识。`MIXED` 要求同范围内同时存在支持与
  反证，来源独立性必须作实质说明而不能只按篇数推定。
- Confidence 固定为独立的 `coverage`、`source_quality` 和 `literature_consistency` 三轴，
  每轴使用 `HIGH`、`MEDIUM`、`LOW` 描述；不产生总分，不表示正确概率，也不从 knowledge
  status、consensus 或其他 confidence 维度机械推导。
- claim fingerprint cache 在复用前执行 freshness check；按 claim 分为时效性事实
  `7 days`、版本化资料 `30 days` 和稳定学术事实 `180 days`，均从 `retrieved_at` 起算，
  复用不重置。该参数只控制重检，不表示证据正确性期限。
- Research Desk 仅凭可追溯的撤稿、更正、官方更新、版本变化或来源失效信号自动写入
  cache invalidation；Representative 只能请求复查，Human 可手动强制失效。强制复查
  生成新 packet，并从新的 `retrieved_at` 重新计算完整 freshness window。
- 自动 cache reuse 仅限规范化命题和适用范围均 `EXACT_EQUIVALENT` 的 claim；较窄、较宽
  或部分重叠关系必须显式记录且不得自动复用。过期重检产生不可变的新 packet，并通过
  `supersedes_packet_ids` 建立版本链，不追溯改写冻结 evidence snapshot。
- Research Desk 试行期不设置每名 Representative 的累计请求上限或会议级 token、费用、
  查询次数上限；每个 Research Round 中每名 Representative 可在一个密封 submission
  内按阶段提交 `0–2` 或 `0–4` 条 claim，相似 claim 优先复用 freshness 合格的公共 packet。
- 同一 Research Round 在冻结并规范化后仅按完整 fingerprint 去重；完全等价请求共享一次
  检索和一个 packet。原始 request/requester 映射仅存 audit-private，公共 packet 不显示
  请求人数或请求者，非 `EXACT_EQUIVALENT` 的相似 claim 保持独立。
- Research Round 整批发布后，公共 packet 展示原始/规范化 claim、verification question
  和 scope；整轮完成前保持密封。Claim 公开不连带公开 requester、request ID 或重复请求
  次数，也不构成支持度或议程优先级信号。
- 每个实质层级首次 Proposal 前开启基础 Research Round；冻结内容若形成修正案、提案或
  动议，则在实体表决前执行条件调研；最终批准前执行 Veto 调研。同一层级后续立场迭代
  复用公共数据库，文本与关键事实未变化时复用冻结 evidence snapshot，纯程序动作不触发。
- Representative 只看 evidence packet；完整查询、候选、筛选和 cache decision 保存在
  audit-private 层，供后续检查 cherry-picking 与遗漏反证。
- Research Round 使用原子 release barrier：完整 packet 私下暂存，全部 claim 终结后才
  整批公开。单项 schema、来源对应、对抗检索或筛选完整性 QC 经规定修复仍不通过时记为
  `QC_FAILED`，不产生 packet，也不阻却会议；但仅有 `CLEAR` 共识标签过强而 packet 仍自洽
  时，发布层降为 `QUALIFIED` 并保留合格的 `SOURCE_BACKED` 状态。供应商不可用、配置缺失、
  持久化或冻结状态损坏等执行基础设施失败仍可暂停。不可研究请求和 `NO_REQUEST` 计为已
  处理，但不产生 evidence packet。
- Research QC 采用最小隔离范围：可定位的候选来源、finding、screening row 或重复记录只
  隔离具体条目并重新验证剩余 packet；仅在条目隔离后无法形成自洽 packet 时将相应 claim
  标为 `QC_FAILED`。含此类 claim 的整轮仍发布为 `PARTIAL_WITH_QC_EXCLUSIONS`。
- Composite Retriever 同样采用后端级隔离：单个后端 429、超时或网络故障时由其余后端
  继续完成对抗检索，公开 packet 只列成功后端并把 coverage 降为 `LOW`；仅当所有配置的
  检索后端都不可用时才升级为可暂停会议的基础设施失败。
- Research Retriever 对独立后端并行发起请求，并并行执行 Tavily 的四类对抗性查询；
  OpenAlex 单个 claim 内四类查询保持串行，不同去重 claim group 形成受限流水线。所有并行
  结果仍按固定 backend/查询顺序合并和落盘，Research Desk 模型综合服从会议冻结的模型
  并发上限，公开门闩与恢复语义不变。
- 模型调用调度新增每个 `provider:model` 的同时在途请求上限：供应商目录明确报告时自动
  使用，否则读取 provider 默认值或 model 精确覆盖，未知时取 1。独立密封提交可在同一
  模型内按此上限并发，但冻结顺序保持确定；RPM/TPM/RPD 不被误作并发度，也不发送付费
  探测请求猜测额度。
- Research Round 已接入可执行会议状态机：覆盖总则立场前、修正案冻结后、总则批准前、
  C0 起草前、细则审阅前和细则提案冻结后的实体边界。提交按基础模型分 lane；同一模型
  人格按固定顺序入队并受每模型并发上限约束，不同模型并行。治理私有 staging 保存恢复检查点，公开 snapshot 是
  Representative 可见性门闩，发布后的 snapshot 自动加入后续阶段上下文。
- 正式 Research Round 的原始 PDF、其他文档、manifest 和 ZIP 与 packet 共用 release
  barrier，整轮完成前不得通过公共文件泄露部分结果；独立 `ensemble-v07 research-claim`
  不属于密封批次，完成后可以即时发布，但存在未完成正式轮次时不得绕过门闩。
- 会议结果新增已发布 Research Round 数量和公开快照所引用的不重复 evidence packet 数量；
  两项均为持久化运行状态计数，不表示来源质量、文献共识或事实正确率。
- 公共证据数据库保持 meeting-local，不建立跨会议共享层。事实性发言使用 packet/source
  结构化引用；每场会议生成 Human 可下载的 `literature_bundle.zip`，集中保存依法公开取得
  的原始 PDF/文档及含 URL、访问时间、license、文件路径和 SHA-256 的 provenance manifest。
- 仅有摘要、搜索片段或元数据而没有可读原文的来源确定性降为 `PROVISIONAL`，不能单独
  产生 `SOURCE_BACKED`；模型请求的状态在发布前至多降为 `QUALIFIED`，并公开记录全文
  归档覆盖情况，但不因合法全文不可得而阻塞整场会议。

## v0.6-dev

- 密封表决与表决理由窗口接入 bounded model lanes：不同 `provider:model` 可并行，同一模型
  遵守冻结并发上限及 persona 入队顺序；中断时保留已校验的个人提交，最终按 roster 顺序
  冻结且继续禁止 partial disclosure。会改变后续输入的 amendment 及有依赖关系的 ballot
  rounds 仍按程序顺序执行。
- Human 授权 Atomic Drafting Item 机械粒度规则进入试行并解除 Drafting Alignment
  边界暂停；试行政策明确标记为待 Think Tank 审阅，审阅不得自动追溯改写冻结结果。
- Human 授权细则 docket 与 motion 门槛试行：C0 按二级编号形成 docket，9 名 ACTIVE
  Representative 各作一次隔离审阅；Clause Split 与 Motion to Suspend 当前均至少需
  3/9 支持，永久规则待 Think Tank 审阅。
- 实现已通过 Clause Split 的可恢复应用：保留 C0，产生带 provenance 的重基 standing
  text/docket，拆分项使用稳定字母后缀且不重跑已冻结审阅；歧义重定向不得静默完成。
- Chair 的细则 option-set 构造改为按 target clause 独立冻结：单提案目标机械形成 Type I，
  仅多提案目标调用 Chair；中断恢复时只补尚未完成的目标条款。
- Human 确认 amendment conflict set 优先于随机逐项处理；部分互斥内容由 Chair 拆为
  compatible fragments 和 exclusive choice sets，前者二元表决，后者执行
  multi-option → top two → binary，并始终保留 `STATUS_QUO`。
- Human 暂定同一 submission window 的 amendment 在冻结后使用可复现随机顺序；保存随机 seed、算法和最终顺序。
- （已被本版后续 Human 决定取代）Atomic Drafting Item 曾暂时悬置，并在
  Drafting Alignment 边界关闭式暂停；现由上述试行规则解除该暂停。
- Human 确认制度条款冲突或不足以唯一执行当前动作时，Chair 可发起结构化咨询；不得自行补默认值。咨询与答复须按事项冻结，并记录 roster、门槛公式、比较符号及最低票数。
- Human consultation 允许 Human 每轮提出一条自然语言程序问题，并允许正式要求 Chair 重新审查 conflict/scope/dependency 分类；解释不构成决定，复核不得覆盖原裁定。
- Constitutionalized conservative quality-first design priority: safety/correctness and epistemic/procedural integrity take priority over token/latency efficiency.
- Clarified Pragmatic Minimalist: minimize complexity only within the set of solutions that satisfy the quality floor.

## v0.5

### Confirmed
- Added Strategic-Behavior Audit as a mandatory Audit Conference dimension.
- Audit evaluates observable rule exploitation and institutional effects, not subjective cheating intent.
- Audit is authorized to inspect governance-private scoring, selection, atomic-item, co-sponsorship, Chair ruling, and influence-allocation records.
- Potential gaming patterns are normally logged during deliberation without warning, punishment, or interruption unless they independently invalidate procedure.
- Added explicit checks for blanket/conflicting co-sponsorship, atomic-item fragmentation, semantic duplicate amendments, timing/backtracking exploitation, amendment-category gaming, and low-information drafting-credit accumulation.
- Audit must examine whether behavior creates future institutional advantage disproportionate to substantive contribution.
- Audit may examine cross-meeting adaptive behavior and whether hidden anti-gaming mechanisms themselves create harmful selection effects.
- No anti-gaming rule is exempt from audit.
- Audit Member sessions must be isolated from Representative sessions; governance-private audit knowledge must not flow back into later Representative contexts.
- Added machine-readable `strategic_behavior_audit.example.yaml`.

## v0.4

### Confirmed
- Added four constitutional anti-gaming firewalls: Identity, Evaluation, Selection-Rationale, and Procedural-Horizon.
- Representatives know current rights/actions but not internal scoring, ranking, selection logic, future roles, or future procedural privileges.
- Full deliberation protocol is governance-private; Representatives receive only stage-local runtime protocols.
- Added Minimum Sufficient Procedural Information principle.
- Added Context Reconstitution recommendation between stages.
- Chair is presented to Representatives primarily through impersonal procedural state, not as a social/political actor.
- Co-sponsorship is frozen before ballot and hidden from other Representatives until the applicable ballot is closed.
- Ballots are sealed; no rolling tally or partial trend disclosure is permitted.
- Co-sponsoring mutually exclusive amendments does not interrupt the meeting; Chair logs the pattern for later audit.
- Drafting Alignment formula, contribution weights, status-selection criteria, scores, and rankings are governance-private.
- Ordinary Librarian runtime no longer discloses future Think Tank privilege. Think Tank instructions are loaded only after that stage begins.
- Resolved the previous open issue concerning strategic co-sponsorship through information hiding rather than an explicit sponsorship cap.


## v0.3

### Confirmed
- Initial-draft retained items and adopted amendment items receive equal direct drafting credit.
- Co-sponsorship receives 50% credit.
- Partial adoption is scored by adopted atomic items.
- A two-way tie for primary drafter is resolved by an all-representative head-to-head vote; a further tie is decided by Chair.
- Consultative conversion target is `floor(0.30 × N)` and may never exceed 30%; a tie crossing the cutoff is retained as ACTIVE.
- Representatives declare amendment impact scope; Chair constructs the operative conflict/dependency map.
- Chair resolves amendment-category disputes as procedural rulings and logs them for later audit.
- No abstentions in deliberative ballots.
- API unavailability: initial failure + three retries; continued failure pauses the meeting.
- Procedural review precedes epistemic review.
- Think Tank never automatically reopens or returns a meeting.
- Chair is selected by the human.
- Clerk merged into Chair.
- Audit PASS consumes one of the five speaking slots.
- Execution handoff is flexible and human-facing rather than code-specific or fully autonomous.

### Still open
See `10_open_questions/open_questions.md`.

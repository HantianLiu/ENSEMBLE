# API 与配置入口

Project_ENSEMBLE v0.7.0 使用配置文件和 provider 的 live model discovery，不把模型名称硬编码为
固定 roster。当前示例配置支持 DeepSeek、Kimi、GLM 和 Gemini 的文本接口；具体可用模型
以 `ensemble-v07 discover-models` 的实时结果为准。

Provider 定义集中放在 `model_config.toml`；主配置只用
`[project].model_config_file` 引用它。API 密钥通过环境变量或本地 secret assignment
file 读取，不写入配置、Git 仓库或会议文件。ENSEMBLE 把 assignment file 当作数据解析，
不会 `source` 或执行其中的 shell。常见
变量包括 `DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`GLM_API_KEY`、`GEMINI_API_KEY`、
`OPENALEX_API_KEY` 和启用 Tavily 时的 `TAVILY_API_KEY`。

推荐从任意工作目录使用已安装入口：

```bash
cp examples/ensemble.example.toml ensemble.toml
cp examples/model_config.example.toml model_config.toml
export ENSEMBLE_CONFIG=/absolute/path/to/ensemble.toml
ensemble-v07 doctor
ensemble-v07 discover-models
ensemble-v07              # 新会议初始化
ensemble-v07 run-general --meeting /absolute/path/to/M-XXXXXXXX
# 派生或从零开始的文献报告会议使用：
ensemble-v07 run-report --meeting /absolute/path/to/M-XXXXXXXX
```

会议会在启动命令的当前工作目录创建。会议目录中的
`human_private/session_configuration.json` 保存配置路径，因此恢复会议时通常不需要再次
提供 `--config`。

旧版 v0.6 会议需要先复制迁移，不能直接用 `ensemble-v07 open` 在原目录续跑。
迁移前可先运行只读预检：

```bash
ensemble-v07 migrate-v06 --meeting /path/to/M-OLDID \
  --output /path/to/M-OLDID-v07 --config /path/to/v07/ensemble.toml --dry-run
ensemble-v07 migrate-v06 --meeting /path/to/M-OLDID \
  --output /path/to/M-OLDID-v07 --config /path/to/v07/ensemble.toml
ensemble-v07 open /path/to/M-OLDID-v07
```

迁移必须使用独立的 v0.7.0 配置文件，不能复用原会议记录的 v0.6 配置路径。
迁移副本沿用原会议 ID；请用完整路径打开。历史密封票和冻结文书保留原样，后续未完成步骤按
v0.7.0 制度运行。审计会议暂不支持迁移。源会议和其 v0.6 恢复脚本均不改动。

初始化时会分别选择全体 Representative 的 `reasoning effort`、Chair 的
`reasoning effort`，以及是否启用共享的 Research Desk。启用 Research Desk 后才会另外选择
其模型和 reasoning effort；它不是代表、没有票权，使用 OpenAlex（可选 Tavily）完成对抗性
检索，并把 evidence packet 与可下载的会议文献包写入会议目录。还可以选择 research-only
会议：

```bash
ensemble-v07 run-research --meeting /absolute/path/to/M-XXXXXXXX
```

初始化菜单的“接续既有会议”会新建一个 `literature_review` 交付物会议。源会议保持只读；
其公开 evidence packets、调研轮次、文献原文和文献包复制到新会议，并在
`public/continuation/lineage.json` 记录逐文件来源、字节数和 SHA-256。研究总纲阶段由
四名覆盖四种职位的规划代表提交最多八个模块的密封拆分方案，Chair 聚类后由全体代表
限量结构审阅。冻结后，`run-report` 按总纲顺序逐模块执行证据覆盖检查、顺序追问、
证据 dossier、起草、专业审阅、全体限量审阅和 3/4 确认；最后形成全文综合，登记但不以
接受票设置发表门槛，并生成 Markdown、PDF、参考文献、未解决附录、审计清单和文献包。
各模块完整结束后才启动下一模块；技术故障暂停当前步骤，已经冻结的分片可安全恢复。

初始化菜单的“从零开始”使用相同的 `literature_review` 流程，但不要求源会议。系统在
`public/literature_report/origin.json` 冻结 `FROM_SCRATCH` 来源状态，Research Desk
会议级证据库从 0 个继承 evidence packet 开始。

默认不设置 ENSEMBLE 自己的单次输出 token 上限；需要限制时可在配置中设置
`governance.provider_output_token_limit`，或对单次运行使用 `--max-output-tokens`。完成的
模型调用会记录 prompt、cached、cache-miss、completion、reasoning 和 total token telemetry；
缺失的 provider cache 字段显示为“未报告”，不会被当作零。

输入上下文另有默认开启的安全预算。`governance.provider_input_context_fraction`
默认取供应商公布 input context capacity 的 `80%`；供应商未公布容量时，使用
`governance.provider_input_token_limit_fallback = 262144` 个估算 input tokens。这里的
token 数是调用前接受标准，不是供应商返回的实测量；估算式为
`ceil(UTF-8 bytes / 3) + 512`。预算按会议冻结在
`governance_private/model_input_context_budgets.json`。如果供应商 `/models` 不返回容量，
可用 `providers.<id>.model_input_token_limits` 登记经供应商文档核实的逐模型容量；旧会议
不改写原冻结文件，而是在
`governance_private/model_input_context_budget_upgrades.json` 留下不可变升级记录。超过预算的请求会在发送前以
`MODEL_INPUT_CONTEXT_BUDGET_EXCEEDED` 暂停。

公开 Research evidence 数据库仍完整保存在会议目录和文献包中；单次 Representative
调用只注入与当前公开议题相关的有界视图，最多 16 个有效 packet、64,000 个 Unicode
字符。未进入该次视图不表示证据不存在，也不会删除 packet 或原始论文。完整 snapshot
的派生读取和实际释放片段的 packet ID、字节数、哈希分别进入文件访问审计链。

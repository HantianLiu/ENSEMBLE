# 议事结束、终检与人类移交顺序

状态：CONFIRMED

## 1. 顺序原则

程序性问题前置。

最终顺序为：

1. Assembly 完成实体表决，形成 provisional resolution；
2. Chair 执行程序完整性检查，并先处理程序性错误/裁定；
3. 得到 procedurally certified text；
4. Librarian Think Tank 对该文本进行知识完整性检查；
5. Think Tank 只形成意见，不自动退回；
6. Chair 形成主要 execution/handoff brief；
7. Think Tank 独立形成对该 brief/最终知识状态的 review opinion；
8. 两份材料一起提交 Human；
9. Orchestrator 将程序认证后的成果和 Chair 交接说明置于正文，将 Think Tank
   独立意见置于明确标注为咨询性的附录；
10. Chair 只对出版稿的正文/附录分隔、导航、来源标签和文本可读性作最终确认；
11. 确认通过后冻结 Markdown 来源、PDF 和文件哈希；
12. 全体 Representative 在身份盲态下完成一次会后密封提交；Consultative Representative
    可同时提交少数报告，全体 Representative 均须明确是否请求审计介入；
13. Orchestrator 先冻结少数材料，再冻结 audit petitions；
14. Chair 基于冻结记录向 Human 作最终述职；
15. Human 决定 ACCEPT / REJECT / EXPERIMENTAL / DEFER、是否召开 Audit Conference，
    或是否重新召开议事会议。

## 2. Chair 程序检查的优先性

如果 Chair 的程序纠正改变了有效文本、vote outcome 或 provenance，则 Think Tank 必须基于纠正后的版本工作。

## 3. Think Tank 无自动退回权

任何 `MATERIAL_OMISSION`、`NEEDS_CLARIFICATION` 或其他智库意见都只是 human-facing finding。

Think Tank 不可：
- 自动 reopening；
- 自动触发 revote；
- 自动修改 final resolution。

## 4. 两份人类材料

### Chair 主文书
说明：
- Assembly 实际通过了什么；
- 当前任务应如何被理解和移交；
- 必要 provenance；
- 已知 contested / protective / human-modified 部分；
- 任务特定的后续行动建议。

格式按任务需要，不强制代码式 schema。

### Think Tank Review
独立说明：
- 知识链是否完整；
- 是否存在遗漏、假设、证据不足或执行前应注意事项；
- 哪些问题值得 Human 决定是否重开议事。

Chair 不得把 Think Tank 的独立意见“融合掉”。二者同时呈递给 Human。

## 5. 最终出版与可读性确认

最终 PDF 是展示层产物，不是新的实体决议。其正文必须完整保留程序认证后的成果，
并可同时包含 Chair 的执行移交说明；Think Tank 的知识完整性意见和执行移交意见必须
作为独立附录呈现，并明确标注其咨询性质。

若本场会议存在已经发布的 Research Desk evidence packet，最终出版物必须附带编号制的
“会议文献证据索引”和去重参考文献表。索引只可把 packet 中已经结构化记录的 supporting、
contradictory、scope limitation 和 canonical alternative finding 连接到其实际 `source_id`；
不得根据文本相似度猜测某篇文献支持正式决议中的某一条规范性要求。参考文献至少保留作者、
题名、载体或出版物、年份、DOI（如有）或 URL、evidence packet ID、证据类别和本地归档路径
（如有）。同一 DOI，或在没有 DOI 时同一规范化 URL，只列为一条参考文献。已被新版 packet
明确 supersede 的旧 packet 不进入当前索引，但仍保留在会议档案中。

Chair 的最终可读性确认只检查：
- 正式成果是否容易识别；
- 正文与咨询性附录是否清楚分隔；
- 导航和层级是否可读；
- 来源标签是否清晰，且编号引文是否可解析到本会议的 evidence packet、来源元数据和文献包；
- 来源文本是否能够正常阅读。

Chair 不得在这一阶段重新概括、修正、增强、削弱或融合正式成果和独立审查意见。
若任一展示层检查失败，系统必须暂停出版，不得生成或冻结最终 PDF。Markdown 来源、
Chair 可读性确认、PDF 和出版 manifest 均须带可核验哈希并进入审计记录。

## 6. Chair 最终述职

所有会后密封提交和 audit petitions 冻结后，Chair 向 Human 形成独立的最终述职，至少说明：
- 会议完成了什么以及最终状态；
- 会议中的主要实体、程序、provider 与恢复问题；
- 每个 Representative ID 的可核验贡献、局限和运行可靠性；
- 哪些 Representative 请求 Audit Conference 介入、提交了多少 petition、涉及哪些程序问题；
- petition 不自动推翻决定，是否召开 Audit Conference 仍由 Human 决定。

述职不得推断 hidden reasoning，不得把 Consultative 转换或试行 Drafting Alignment
当作一般能力评级，也不得改写正式成果。Chair 始终只按 Representative ID 评价，不能读取
ID 到模型/人格的真实映射。Orchestrator 可以在 Chair 完成述职后，为 Human 机械附加身份
映射，并必须明确标注该附录未曾提供给 Chair。

Chair 还须为交互式命令行提供一份不超过 6 行、1200 字符的自足简报，概括最终成果、代表
整体表现、主要程序/运行问题及审计申请数量。完整述职仍须独立冻结，命令行简报不得替代它。

为便于 Human 发现成果，Orchestrator 可在会议目录顶层建立指向冻结原件的稳定相对链接和
`MEETING_RESULTS.md` 索引。入口不得复制或改写冻结原件；恢复时必须校验既有入口仍指向同一
会议内目标。

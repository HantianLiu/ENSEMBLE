# Execution / Handoff Interface

状态：CONFIRMED — 轻量接口

## 1. 定位

Project_ENSEMBLE 当前只负责提高决策和知识形成质量，不试图定义一套统一的自主执行制度。

议事结果可能被用于：
- 代码；
- 研究；
- 文献分析；
- 实验计划；
- 数据处理；
- 写作；
- 人类决策；
- 其他任务。

因此 execution/handoff **不强制标准化为代码交付包**。

## 2. Chair 主文书

Chair 基于 procedurally certified resolution 和完整 provenance 形成主要 `execution_handoff` / `execution_brief`。

其格式可按任务自适应，但应尽可能说明：
- 已批准目标；
- 必要约束；
- 已知 contested/protective 部分；
- 关键 provenance；
- 需要 Human 或后续执行者注意的事项。

## 3. Think Tank 独立检查意见

Think Tank 阅读 Chair 主文书后，单独形成 `think_tank_execution_review`。

该意见关注：
- knowledge completeness；
- missing assumptions；
- evidence gaps；
- ambiguous claims；
- task-specific cautions。

Think Tank 不直接重写 Chair 主文书。

## 4. 一起呈递 Human

最终向 Human 同时提交：
1. Chair 主文书；
2. Think Tank review。

Human 决定下一步执行方式。

## 5. 人类可读出版物

两份材料冻结后，Orchestrator 形成一份人类可直接阅读和下载的最终出版物：
- 程序认证后的成果与 Chair 交接说明属于正文；
- Think Tank 的独立知识审查和执行审查属于咨询性附录；
- 附录不得被重新综合为伪共识；
- Chair 在 PDF 冻结前只确认出版稿的可读性和材料分隔，不重新进行实体审议；
- 可读性检查未通过时暂停，不发布最终 PDF。
- 已发布 Research Desk 资料以编号制 evidence 索引和去重参考文献表进入 PDF；每个编号必须
  可追踪到 packet/source 和文献包，且不得自动声称该来源支持某条规范性决议。

最终同时保留 Markdown 来源、PDF、Chair 可读性确认和包含 SHA-256 的出版 manifest。

## 6. 非目标

本接口不定义：
- 独立 Code Delivery Meeting；
- 通用 Implementation Agent；
- 强制统一 acceptance schema；
- AI 必须自主完成整个研究/工程任务。

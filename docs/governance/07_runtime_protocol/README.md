# Runtime Protocol Surface

状态：CONFIRMED

Representative **不得加载完整的 governance deliberation protocol**。

Orchestrator 应根据当前 meeting state，只加载一个 current-stage protocol 文件。

推荐组合：

`common_representative_rules + own_persona_memory + current_stage_protocol + current_public_state`

下一阶段文件在当前阶段冻结前不得加载。

本目录中的静态文件是最小模板；实际运行时可由 Orchestrator 根据 current state 进一步裁剪。

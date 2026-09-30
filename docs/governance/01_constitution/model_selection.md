# 会前模型发现、Representative Roster 与 Chair 选择

状态：CONFIRMED

## 1. 不硬编码代表模型

程序在每场会议前查询已配置 provider 的可用模型，并尽可能显示：
- context window；
- multimodality；
- tool use；
- structured output；
- reasoning capability；
- pricing；
- availability；
- provider metadata。

必要时可结合 adapter knowledge 与轻量 capability probe。

## 2. 人类选择 Representative 基础模型

人类选择本场进入 Assembly 的基础模型。选择完成后 roster 冻结。

每个被选模型完整遍历全部议事 persona。

## 3. 人类指定 Chair 模型

Chair 因具有程序裁定、文书控制和有限 tie-break 权，由人类单独指定其模型。

Chair 的选择不由 Assembly 自动决定。

Chair 是否与某一 Representative 使用同一基础模型，不改变其制度身份；Chair 实例仍与 Representative 上下文和身份隔离。

## 3A. Role-specific reasoning effort

会议初始化分别冻结全体 Representative 共用的 reasoning-effort 输入控制参数与 Chair
专用参数。两个参数互不覆盖。`default` 沿用 provider 默认行为；显式档位必须经 adapter
配置映射为该 provider 接受的字段和值。若所有受控模型不存在共同合法档位，TUI 只提供
`default`，不得用 prompt 文本伪装成 API reasoning control。

若 Human 启用 Research Desk，还应单独选择其模型和 reasoning effort。Research Desk
模型不加入 Representative roster；详见 `03_roles/research_librarian.md`。

## 4. 别名和谱系

程序应对疑似同谱系、别名或高度近似版本给出 warning，但不禁止人类同时选择。

## 5. 运行配置记录

长期审计记录完整 runtime configuration：
- model；
- provider；
- harness；
- reasoning effort；
- prompt version；
- tool configuration；
- 其他关键设置。

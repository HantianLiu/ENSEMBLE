# 学术主笔：程序化配图（第一版）

这是一份随写作阶段加载的主笔技能，不是额外的模型角色。程序负责绘制，主笔负责科学内容。

## 什么时候画图

仅在图能帮助读者理解关键比较、数据趋势、机制关系或方法流程时配图；不为装饰强行画图。
每章最多六张，无合适图时 `draft.figures=[]`。第一版不接受本地图片、图片网址、论文截图、
绘图代码或外部绘图工具调用；不执行 Python、JavaScript、shell 或模型生成的代码。

## 交稿方式

在 `draft.body_markdown` 的合适位置独占一行放置 `[[FIGURE:图的id]]`，每个 id 只用一次。
在 `draft.figures` 提交对应的 JSON 作图规格。正文必须解释图回答什么问题，不能把关键判断仅放在图中。
标题、图注、轴名、系列名、节点名和文字替代说明使用报告的写作语言。
图内标签不用 RM/RP/C 等内部编号；文献引文集中放图注和数据说明，便于统一编号。

通用必填字段：

- `id`：小写 ASCII 字母开头，后续仅字母、数字、短横线，最长 48 字符。
- `kind`：`line`（折线）、`bar`（分组柱状）、`scatter`（散点）、`heatmap`（矩阵热图）或 `flowchart`（关系/流程示意）。
- `title`、`caption`：标题和完整图注；图注必须交代对象、适用条件、口径及不确定性。
- `alt_text`：不看图片也能理解主要关系的文字替代说明。
- `evidence_basis`：`extracted`（来源明确给出的数据）、`derived`（可复现推导）或 `schematic`（非定量示意）。
- `data_note`：数据提取位置、每个系列/矩阵值的出处、单位/分母/时间、变换或推导方法；示意图说明箭头含义。
- `source_citation_ids`：本章冻结目录中的 C 文献编号；程序将统一编入报告参考文献。不得写 RP 编号。

## 数据图

`line`/`scatter` 用 `x_label`、`y_label`（包括单位）及 `series`，每个系列为
`{"label":"名称","x":[...],"y":[...],"y_error":[...]}`；`y_error` 可省略，但不能编造误差。
折线的 x 必须递增。`bar` 用 `categories`、`x_label`、`y_label`、`series`；系列无需 x，
y 与类别数量相同，柱状图保持零基线。每个系列可另给 `source_citation_ids` 对齐出处。
`heatmap` 用 `row_labels`、`column_labels`、`matrix`（逐行矩阵）、`x_label`、`y_label`、
`value_label`（数值含义和单位）。所有数据为有限数值，尺寸必须匹配。

不从仅有题名、元数据或不含数值的摘要虚构数据点；不把类别或主观等级伪装成连续实测量。
没有足够数据时改画明确标为非定量的示意图，或只写正文。`derived` 必须在 data_note
写清公式/计算步骤和假设，正文同时保留推导边界；不把推导或插值标成实测值。

## 关系/流程图

`flowchart` 必须 `evidence_basis="schematic"`。用 `nodes:[{"id":"a","label":"..."},...]`
和 `edges:[{"source":"a","target":"b","label":"..."},...]`。节点最多 12 个、边最多 24 条。
图注必须明确是示意而非定量证据；箭头是因果、先后、传递还是待验证假设必须在 label/data_note 说明。
不能因为渲染器画了箭头就声称因果成立。无外部来源的工作示意可留空来源，但必须明确其假设性质。

## 最小示例（仅示范结构，不可照抄示例数值或文献编号）

```json
{
  "id": "workflow",
  "kind": "flowchart",
  "title": "方法流程示意",
  "caption": "工作流程示意，箭头表示操作先后，不是因果证据。",
  "alt_text": "从构造输入开始，再进行几何测量。",
  "evidence_basis": "schematic",
  "data_note": "作者对正文方法步骤的非定量整理；无新增科学结论。",
  "source_citation_ids": [],
  "nodes": [{"id":"input","label":"构造输入"},{"id":"measure","label":"几何测量"}],
  "edges": [{"source":"input","target":"measure","label":"操作先后"}]
}
```

## 审阅和修订

作图规格、数据、出处、图注及替代说明与正文一起交科学审阅。审阅者可以用正文里的
`[[FIGURE:id]]` 精确定位问题；按科学意见修正数据或图注，不能仅改正文而留下矛盾配图。
格式错误只影响对应配图，原始请求和诊断留档；图被省略不等于科学异议已解决。
程序输出 PNG/SVG 并保存数据规格和哈希；HTML 内嵌图片，PDF 使用同一 PNG，不要求用户上传图片。

"""Unambiguous Markdown/JSON math instructions, shared by prose-producing calls."""
from __future__ import annotations

import json

DISPLAY_EXAMPLE = "公式前说明。\n\n$$\nx = \\beta y\n$$\n\n公式后说明。"
INLINE_EXAMPLE = r"短变量使用 \(x\)，参数使用 \(\beta\)。"
ALIGNED_EXAMPLE = "\n".join([
    "$$", r"\begin{aligned}", r"x &= y+z \\", r"u &= v",
    r"\end{aligned}", "$$",
])

# Generate examples with the serializer rather than hand-escaping prompt text.
# These are field fragments, not replacements for the current response schema.
MATH_JSON_EXAMPLES = {
    "display": json.dumps({"body_markdown": DISPLAY_EXAMPLE}, ensure_ascii=False),
    "inline": json.dumps({"short_summary": INLINE_EXAMPLE}, ensure_ascii=False),
    "aligned": json.dumps({"formula": ALIGNED_EXAMPLE}, ensure_ascii=False),
    "replacement": json.dumps({"new_text": DISPLAY_EXAMPLE}, ensure_ascii=False),
}
DOUBLE_ESCAPED_NEWLINE_EXAMPLE = json.dumps(
    {"body_markdown": DISPLAY_EXAMPLE.replace("\n", r"\n")}, ensure_ascii=False,
)

MATH_OUTPUT_FORMAT_RULES = (
    "\n\n公式输出格式（适用于本次获准写作的正文、摘要、术语释义/formula 和局部修订 new_text；"
    "只约束排版与编码，不要求新增公式，不改变来源、科学内容或修订权限）：\n"
    "1. JSON 解码后的字段值必须是普通 Markdown，不是又一层 JSON、Python repr 或字符串转义展示。"
    "行内只用 \\( ... \\) 写短变量、参数和短条件；完整等式、不等式、积分或推导单独成块。"
    "块公式的开头 $$ 和结尾 $$ 各独占一行，与相邻正文用真实换行分开；不要在定界符中套另一组数学定界符。\n"
    "2. 数学表达式用 LaTeX 命令；上下标和希腊字母在数学定界符内。不要把公式放进代码围栏、"
    "行内反引号或 Markdown 标题，不用 HTML 实体代替数学符号；文献引文、中文说明和公式后的标点放在数学块之外。"
    "术语解释放 explanation，formula 放公式；若当前字段必须兼容公式与说明，说明也必须在数学定界符之外。\n"
    "3. 明确区分 Markdown 内容与外层 JSON 编码：Markdown 中的换行是真实换行，"
    "不是字面的反斜杠加字母 n。输出 JSON 时，真实换行编码为一个反斜杠的 \\n；"
    "LaTeX 命令的单个反斜杠编码为两个反斜杠，例如解码后 \\beta 对应 JSON 中的 \\\\beta。"
    "不要把换行过度编码为 \\\\n，否则 json.loads 一次之后正文仍会留下字面的 \\n。"
    "也不要把 \\beta、\\rho、\\frac、\\text 中的反斜杠漏转义，生成退格、回车、换页或制表控制字符。\n"
    "4. 多行推导用一组块定界符内的 aligned 等数学环境。Markdown 换行与 TeX 换行命令不是一回事："
    "aligned 中的 TeX 换行是两个反斜杠，在外层 JSON 中编码为四个反斜杠；不要拿它代替正文的真实换行。\n"
    "5. 先组织正常 Markdown，再只对外层 JSON 序列化一次。输入中的 JSON 展示形式只用于传输，"
    "不要把它的转义层抄入输出字段；旧稿若含错误转义，只在当前获准修改的片段中输出正确格式，"
    "不得因此重写未授权片段或改变任何公式符号、定义及条件。\n"
    "以下示例只演示已有字段的编码，实际输出仍严格遵守当前 JSON schema；"
    "不要增加示例字段、复制示例内容或给最终 JSON 包代码围栏：\n"
    "正确：块公式与单次换行编码\n" + MATH_JSON_EXAMPLES["display"] + "\n"
    "正确：行内 LaTeX 的 JSON 编码\n" + MATH_JSON_EXAMPLES["inline"] + "\n"
    "正确：多行推导的 JSON 编码\n" + MATH_JSON_EXAMPLES["aligned"] + "\n"
    "正确：局部替换文本同样只编码一次\n" + MATH_JSON_EXAMPLES["replacement"] + "\n"
    "错误：下面的换行被多编码了一次，解码后留下字面的反斜杠 n；禁止仿照\n"
    + DOUBLE_ESCAPED_NEWLINE_EXAMPLE + "\n"
    "发送前自查：按 JSON 解码一次后，$$ 独占行、普通换行真实存在、"
    "LaTeX 命令仍是正确的单反斜杠、aligned 行分隔仍是两个反斜杠，且公式定界符成对闭合。"
)

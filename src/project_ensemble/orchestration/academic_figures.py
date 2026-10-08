"""Declarative, bounded scientific figures; never execute Writer-supplied code."""
from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

FIGURE_MARKER = re.compile(r"\[\[FIGURE:([a-z][a-z0-9-]{0,47})\]\]")
GENERATED_IMAGE = re.compile(r"^!\[(.*?)\]\((figures/generated-[a-f0-9]{64}\.png)\)\s*$", re.M)
_DRAW_LOCK = threading.RLock()
FIGURE_REVIEW_RULES = (
    "draft.figures 是程序化配图的科学内容，须核对数据、单位/分母/时间口径、出处、"
    "推导与实测的区分、误差及示意箭头含义，并与正文/图注一致。"
    "仅能看到结构化作图规格，不得声称已目视检查图像；可用正文 [[FIGURE:id]] 定位实质异议。"
    "figure_diagnostics 是被省略配图的技术记录，不等于对应科学异议已解决。"
)


def writer_figure_skill() -> str:
    return (Path(__file__).parent.parent / "assets/skills/academic_figures/SKILL.md").read_text(encoding="utf-8")


class _FigureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FigureSeries(_FigureModel):
    label: str = Field(min_length=1, max_length=120)
    x: list[FiniteFloat] = Field(default_factory=list, max_length=1000)
    y: list[FiniteFloat] = Field(min_length=1, max_length=1000)
    y_error: list[FiniteFloat] = Field(default_factory=list, max_length=1000)
    source_citation_ids: list[str] = Field(default_factory=list, max_length=30)


class FigureNode(_FigureModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    label: str = Field(min_length=1, max_length=120)


class FigureEdge(_FigureModel):
    source: str
    target: str
    label: str = Field(min_length=1, max_length=100)


class FigureSpec(_FigureModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    kind: Literal["line", "bar", "scatter", "heatmap", "flowchart"]
    title: str = Field(min_length=1, max_length=200)
    caption: str = Field(min_length=1, max_length=2000)
    alt_text: str = Field(min_length=1, max_length=1200)
    evidence_basis: Literal["extracted", "derived", "schematic"]
    data_note: str = Field(min_length=1, max_length=2000)
    source_citation_ids: list[str] = Field(default_factory=list, max_length=60)
    x_label: str = Field(default="", max_length=160)
    y_label: str = Field(default="", max_length=160)
    value_label: str = Field(default="", max_length=160)
    series: list[FigureSeries] = Field(default_factory=list, max_length=8)
    categories: list[str] = Field(default_factory=list, max_length=40)
    row_labels: list[str] = Field(default_factory=list, max_length=30)
    column_labels: list[str] = Field(default_factory=list, max_length=30)
    matrix: list[list[FiniteFloat]] = Field(default_factory=list, max_length=30)
    nodes: list[FigureNode] = Field(default_factory=list, max_length=12)
    edges: list[FigureEdge] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def coherent_data(self):
        graphic_labels = [self.title, self.x_label, self.y_label, self.value_label,
                          *self.categories, *self.row_labels, *self.column_labels,
                          *(s.label for s in self.series), *(n.label for n in self.nodes),
                          *(e.label for e in self.edges)]
        if any(re.search(r"\b(?:C\d+-\d+|RP-[A-Z0-9]+|RM-\d+)\b", label) for label in graphic_labels):
            raise ValueError("图内标签不用内部编号，文献引文请放在图注或数据说明")
        if any(len(label) > 200 for label in graphic_labels):
            raise ValueError("图内标签过长，请将详细说明移到图注")
        if self.kind == "flowchart":
            ids = {node.id for node in self.nodes}
            if not ids or len(ids) != len(self.nodes) or self.evidence_basis != "schematic":
                raise ValueError("关系图须为非定量示意且节点编号唯一")
            if any(edge.source not in ids or edge.target not in ids for edge in self.edges):
                raise ValueError("关系图的边指向不存在的节点")
            if self.series or self.matrix or self.categories:
                raise ValueError("关系图不能混入数值图字段")
        else:
            if self.evidence_basis == "schematic" or not self.x_label or not self.y_label:
                raise ValueError("数据图须说明数据依据和含单位的轴名")
            if self.evidence_basis == "extracted" and not figure_sources(self):
                raise ValueError("实测数据图须给出本章文献出处")
            if self.nodes or self.edges:
                raise ValueError("数据图不能混入关系图字段")
            if self.kind == "heatmap":
                if (not self.row_labels or not self.column_labels or not self.value_label
                        or len(self.matrix) != len(self.row_labels)
                        or any(len(row) != len(self.column_labels) for row in self.matrix)
                        or self.series or self.categories):
                    raise ValueError("热图矩阵尺寸、标签和数值单位须匹配")
            else:
                if not self.series or self.matrix or self.row_labels or self.column_labels:
                    raise ValueError("折线/柱状/散点图须提供系列而非矩阵")
                if self.kind == "bar" and not self.categories:
                    raise ValueError("柱状图须提供类别")
                if self.kind != "bar" and self.categories:
                    raise ValueError("连续坐标图不能混入柱状类别")
                if sum(len(series.y) for series in self.series) > 3000:
                    raise ValueError("单图数据点过多")
                for series in self.series:
                    if series.y_error and (len(series.y_error) != len(series.y) or any(e < 0 for e in series.y_error)):
                        raise ValueError("误差须非负且与数据尺寸匹配")
                    if self.kind == "bar":
                        if len(series.y) != len(self.categories) or series.x:
                            raise ValueError("柱状数据须与类别匹配，不使用 x 数值")
                    elif len(series.x) != len(series.y):
                        raise ValueError("x/y 数据尺寸须匹配")
                    if self.kind == "line" and any(b <= a for a, b in zip(series.x, series.x[1:])):
                        raise ValueError("折线 x 须严格递增")
        return self


def figure_sources(spec: FigureSpec) -> list[str]:
    return list(dict.fromkeys([*spec.source_citation_ids,
                              *(c for series in spec.series for c in series.source_citation_ids)]))


def figure_citation_prose(figures: list) -> str:
    """Use the same citation pass for figure captions and ordinary chapter prose."""
    values = []
    for item in figures:
        try:
            spec = FigureSpec.model_validate(item)
        except ValueError:
            continue  # Optional malformed requests are diagnosed by prepare_figures.
        values.extend([spec.caption, spec.alt_text, spec.data_note,
                       "".join(f"[{c}]" for c in figure_sources(spec))])
    return "\n".join(values)


def prepare_figures(body: str, requests: list, catalog: dict) -> tuple[str, list[dict], list[dict]]:
    """Omit only unusable optional figures; do not reject a scientific revision."""
    accepted, diagnostics, seen = [], [], set()
    known = {entry["citation_id"] for entry in catalog.get("sources", [])}
    aliases = {}
    for citation in known:
        match = re.fullmatch(r"C(\d+)-(\d+)", citation)
        if match:
            aliases.setdefault((int(match[1]), int(match[2])), set()).add(citation)

    def normalize(citation: str) -> str:
        value = citation.strip(" []【】［］")
        match = re.fullmatch(r"C(\d+)-(\d+)", value)
        candidates = aliases.get((int(match[1]), int(match[2])), set()) if match else set()
        return next(iter(candidates)) if value not in known and len(candidates) == 1 else value

    for index, item in enumerate(requests):
        identifier = item.get("id") if isinstance(item, dict) else None
        try:
            if index >= 6:
                raise ValueError("每章最多六张配图")
            spec = FigureSpec.model_validate(item)
            marker = f"[[FIGURE:{spec.id}]]"
            if spec.id in seen or body.count(marker) != 1 or [line.strip() for line in body.splitlines()].count(marker) != 1:
                raise ValueError("图编号须唯一且在正文独占一行出现一次")
            seen.add(spec.id)
            spec.source_citation_ids = [normalize(c) for c in spec.source_citation_ids]
            for series in spec.series:
                series.source_citation_ids = [normalize(c) for c in series.source_citation_ids]
            # Caption prose can also contain citations; never silently delete one.
            for field in ("caption", "alt_text", "data_note"):
                text = getattr(spec, field)
                text = re.sub(r"[\[【［](C\d+-\d+)[\]】］]", lambda m: f"[{normalize(m[1])}]", text)
                setattr(spec, field, text)
            used = set(figure_sources(spec)) | set(re.findall(r"\bC\d+-\d+\b", figure_citation_prose([spec.model_dump()])))
            if used - known or re.search(r"\bRP-[A-Z0-9]+\b", spec.model_dump_json()):
                raise ValueError("配图出处不在本章冻结文献目录或含内部证据编号")
            accepted.append(spec.model_dump(mode="json"))
        except ValueError as exc:
            diagnostics.append({"index": index, "figure_id": identifier,
                                "reason": str(exc)[:1200], "status": "OMITTED",
                                "science_review_required": True})
    valid_ids = {item["id"] for item in accepted}
    for identifier in FIGURE_MARKER.findall(body):
        if identifier not in valid_ids:
            body = body.replace(f"[[FIGURE:{identifier}]]", "")
            if not any(d["figure_id"] == identifier for d in diagnostics):
                diagnostics.append({"figure_id": identifier, "reason": "未提供对应作图规格",
                                    "status": "OMITTED", "science_review_required": True})
    return body, accepted, diagnostics


def figure_image_path(spec: FigureSpec) -> str:
    digest = hashlib.sha256(json.dumps(spec.model_dump(mode="json"), ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"figures/generated-{digest}.png"


def expand_figure_markers(body: str, figures: list, language: str = "zh", *, first_number: int = 1) -> str:
    labels = {"zh": ("图", "示意图", "数据依据"), "en": ("Figure", "Schematic", "Data basis"),
              "fr": ("Figure", "Schéma", "Base des données")}.get(language, ("Figure", "Schematic", "Data basis"))
    for number, item in enumerate(figures, first_number):
        spec = FigureSpec.model_validate(item)
        alt = spec.alt_text.replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        refs = "".join(f"[{c}]" for c in figure_sources(spec))
        schematic = f" ({labels[1]})" if spec.kind == "flowchart" else ""
        caption = (f"**{labels[0]} {number}. {spec.title}{schematic}** — {spec.caption} {refs}"
                   f"\n\n{labels[2]}: {spec.data_note}")
        body = body.replace(f"[[FIGURE:{spec.id}]]", f"![{alt}]({figure_image_path(spec)})\n\n{caption}")
    return body


def _flowchart(ax, spec, font):
    # Directed acyclic graphs get layers; cycles use a neutral grid, not a fake sequence.
    remaining, levels = {node.id for node in spec.nodes}, {}
    while remaining:
        ready = [node.id for node in spec.nodes if node.id in remaining and not any(
            edge.target == node.id and edge.source in remaining for edge in spec.edges)]
        if not ready:
            levels = {node.id: index // 3 for index, node in enumerate(spec.nodes)}
            break
        for identifier in ready:
            levels[identifier] = max((levels[e.source] + 1 for e in spec.edges if e.target == identifier), default=0)
            remaining.remove(identifier)
    positions = {}
    max_level = max(levels.values())
    layers = [[n for n in spec.nodes if levels[n.id] == level] for level in range(max_level + 1)]
    total_rows = sum((len(nodes) + 2) // 3 for nodes in layers)
    row_offset = 0
    for nodes in layers:
        for index, node in enumerate(nodes):
            row, column = divmod(index, 3)
            columns = min(3, len(nodes) - row * 3)
            positions[node.id] = ((column + 1) / (columns + 1),
                                  1 - (row_offset + row + 1) / (total_rows + 1))
        row_offset += (len(nodes) + 2) // 3
    for edge in spec.edges:
        a, b = positions[edge.source], positions[edge.target]
        ax.annotate("", xy=b, xytext=a, arrowprops={"arrowstyle": "->", "color": "#536a82", "shrinkA": 22, "shrinkB": 22})
        ax.text((a[0]+b[0])/2, (a[1]+b[1])/2, textwrap.fill(edge.label, 12),
                ha="center", va="center", fontproperties=font, fontsize=8,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": .9})
    for node in spec.nodes:
        ax.text(*positions[node.id], textwrap.fill(node.label, 14), ha="center", va="center",
                fontproperties=font, fontsize=10,
                bbox={"boxstyle": "round,pad=0.5", "facecolor": "#edf3fa", "edgecolor": "#536a82"})
    ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off")


def render_figure(spec: FigureSpec) -> tuple[bytes, bytes]:
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    from project_ensemble.orchestration.final_publication import resolve_cjk_font

    with _DRAW_LOCK, rc_context({"text.usetex": False, "svg.hashsalt": "ensemble-figures-v1"}):
        font = FontProperties(fname=str(resolve_cjk_font()))
        fig = Figure(figsize=(7.4, 5.0), dpi=170, layout="constrained")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        if spec.kind == "flowchart":
            _flowchart(ax, spec, font)
        elif spec.kind == "heatmap":
            image = ax.imshow(spec.matrix, aspect="auto", cmap="viridis", interpolation="nearest")
            ax.set_xticks(range(len(spec.column_labels)), spec.column_labels, rotation=35, ha="right")
            ax.set_yticks(range(len(spec.row_labels)), spec.row_labels)
            colorbar = fig.colorbar(image, ax=ax)
            colorbar.set_label(spec.value_label, fontproperties=font)
            for label in colorbar.ax.get_yticklabels():
                label.set_fontproperties(font)
        else:
            for index, series in enumerate(spec.series):
                error = series.y_error or None
                if spec.kind == "bar":
                    width = .8 / len(spec.series)
                    x = [i - .4 + width/2 + index*width for i in range(len(spec.categories))]
                    ax.bar(x, series.y, width=width, label=series.label, yerr=error)
                else:
                    ax.errorbar(series.x, series.y, yerr=error, label=series.label,
                                marker="o", linestyle="-" if spec.kind == "line" else "none", capsize=3)
            if spec.kind == "bar":
                ax.set_xticks(range(len(spec.categories)), spec.categories, rotation=25, ha="right")
                low, high = ax.get_ylim(); ax.set_ylim(min(0, low), max(0, high))
            ax.legend(prop=font)
            ax.grid(axis="y", alpha=.2)
        ax.set_title(spec.title, fontproperties=font)
        ax.set_xlabel(spec.x_label, fontproperties=font); ax.set_ylabel(spec.y_label, fontproperties=font)
        for label in [*ax.get_xticklabels(), *ax.get_yticklabels()]:
            label.set_fontproperties(font)
        png, svg = io.BytesIO(), io.BytesIO()
        fig.savefig(png, format="png", metadata={"Software": "ENSEMBLE figures v1"})
        fig.savefig(svg, format="svg", metadata={"Date": None, "Creator": "ENSEMBLE figures v1"})
        fig.clear()
        return png.getvalue(), svg.getvalue()


def freeze_figure_assets(repo, figures: list) -> tuple[dict[str, bytes], list[dict]]:
    """Persist specs and generated images once; rendering failures affect only images."""
    assets, records = {}, []
    for item in figures:
        spec = FigureSpec.model_validate(item)
        image_path = figure_image_path(spec)
        if image_path in assets:
            continue
        png_relative = Path("public/final") / image_path
        svg_relative, record_relative = png_relative.with_suffix(".svg"), png_relative.with_suffix(".json")
        failure_relative = png_relative.with_suffix(".failure.json")
        if (repo.root / failure_relative).is_file():
            records.append(json.loads((repo.root / failure_relative).read_text(encoding="utf-8")))
            continue  # A restart never replays an already failed optional rendering.
        try:
            record_path = repo.root / record_relative
            if record_path.exists():
                record = json.loads(record_path.read_text(encoding="utf-8"))
                png = (repo.root / png_relative).read_bytes()
                svg = (repo.root / svg_relative).read_bytes()
                if (record["spec"] != spec.model_dump(mode="json")
                        or hashlib.sha256(png).hexdigest() != record["png_sha256"]
                        or hashlib.sha256(svg).hexdigest() != record["svg_sha256"]):
                    raise ValueError("冻结配图的来源规格或文件哈希不一致")
            else:
                png, svg = render_figure(spec)
                record = {"renderer": "ENSEMBLE_MATPLOTLIB_V1", "spec": spec.model_dump(mode="json"),
                          "png_sha256": hashlib.sha256(png).hexdigest(),
                          "svg_sha256": hashlib.sha256(svg).hexdigest()}
                for relative, content in ((png_relative, png), (svg_relative, svg)):
                    path = repo.root / relative
                    if path.exists():
                        if path.read_bytes() != content:
                            raise ValueError("已落盘配图与本次绘制不一致，未覆盖")
                    else:
                        repo.docs.write_once(relative, content)
                repo.docs.write_once(record_relative, json.dumps(record, ensure_ascii=False, indent=2))
            assets[image_path] = png
            records.append({"path": image_path, "status": "RENDERED", "png_sha256": record["png_sha256"]})
        except Exception as exc:
            failure = {"path": image_path, "status": "TEXT_FALLBACK", "reason": str(exc)[:1200],
                       "spec": spec.model_dump(mode="json"), "science_review_required": True}
            if not (repo.root / failure_relative).exists():
                repo.docs.write_once(failure_relative, json.dumps(failure, ensure_ascii=False, indent=2))
            records.append(failure)
    return assets, records


def figure_text_fallback(spec: FigureSpec) -> str:
    """Keep the requested relationships/values readable if graphics fail."""
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    rows = []
    if spec.kind == "flowchart":
        labels = {node.id: node.label for node in spec.nodes}
        rows = [[node.label] for node in spec.nodes] + [
            [f"{labels[e.source]} → {labels[e.target]} ({e.label})"] for e in spec.edges]
        header = [spec.title]
    elif spec.kind == "heatmap":
        header = [spec.y_label + " / " + spec.value_label, *spec.column_labels]
        rows = [[label, *values] for label, values in zip(spec.row_labels, spec.matrix)]
    else:
        header = [spec.x_label, spec.y_label]
        for series in spec.series:
            header = [spec.x_label, spec.y_label, "series", "±"]
            x = spec.categories if spec.kind == "bar" else series.x
            rows.extend([[value, y, series.label, series.y_error[i] if series.y_error else "—"]
                         for i, (value, y) in enumerate(zip(x, series.y))])
    return "\n".join(["| " + " | ".join(map(cell, header)) + " |",
                      "| " + " | ".join("---" for _ in header) + " |",
                      *("| " + " | ".join(map(cell, row)) + " |" for row in rows)])

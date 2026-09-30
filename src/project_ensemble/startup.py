from __future__ import annotations

import locale
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from project_ensemble.config import EnsembleConfig
from project_ensemble.interface_language import ui_label, ui_text
from project_ensemble.user_settings import (
    interface_language, report_language_default, save_interface_language,
)
from project_ensemble.domain import (
    DeliverableType,
    DecisionRigor,
    GenerationRequest,
    InheritanceMode,
    MeetingType,
    ModelDescriptor,
    Persona,
    ReasoningEffort,
)
from project_ensemble.errors import PermanentProviderError
from project_ensemble.notifications.email import validate_email_address
from project_ensemble.orchestration.report_palette import (
    DEFAULT_PALETTE, PALETTES, PALETTE_DOCUMENT, palette_options,
)
from project_ensemble.providers.registry import build_adapters
from project_ensemble.research.human_references import (
    MAX_REFERENCE_COUNT,
    install_human_references,
    validate_reference_paths,
)
from project_ensemble.storage.meeting import MeetingRepository, provisional_rendering_text
from project_ensemble.storage.meeting_index import (
    discover_local_meetings,
    indexed_meetings,
    inspect_meeting,
    meeting_is_complete,
    resolve_indexed_meeting,
)
from project_ensemble.runtime.terminal_style import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    YELLOW,
    rule_width,
    styled,
    supports_color,
)


ProviderModel = tuple[str, str]


_LITERATURE_PROMPT_DIALOGUE_INSTRUCTIONS = (
    "会前任务：通过自然对话协助人类设计一份文献调研会议可直接使用的完整任务提示。"
    "不要立即美化人类的第一段描述；先弄清目标模型实际要完成的工作。"
    "像研究合作者一样讨论，不使用固定问卷，也不要求人类一次填完大量字段。"
    "每轮利用已有信息推进设计，最多询问一个会实质改变提示词的关键问题；"
    "只有两个问题紧密相连时才合并询问。"
    "优先澄清任务目标、可用输入、硬约束、可接受的退路、成功和失败的判据、"
    "输出形式，以及目标模型可自主判断的范围。"
    "不要反复询问已有答案；容易从上下文推断的小事直接采用合理默认。"
    "人类表示某项不重要时，采用合理默认并告知。"
    "发现目标冲突或术语歧义时，用具体例子简短解释，让人类选择；"
    "明确区分必须遵守的约束与尽量满足的偏好，不能悄悄改变任务。"
    "持续整理已确认的需求，发现新旧要求冲突时指出；"
    "可以偶尔用两三句话复述理解，但不要每轮冗长总结。"
    "当前只设计文献调研任务，不开展调研或声称已经核实事实；"
    "无需且不得自行调用外部资料。"
    "使用人类当前对话所用的语言回应；不要因为界面语言设置自行改变成文语言。"
    "允许未来会议中的模型提出反例、指出不可行性和修正人类前提，"
    "不要把预期结论写成指令。"
    "当进一步追问已难以改变关键设计时，不再提出问题；"
    "回复首行写 [READY]，然后简短说明可以定稿。"
    "其他时候只给简短的当前回应和至多一个问题。"
)

_LITERATURE_PROMPT_DRAFT_INSTRUCTIONS = (
    "根据会前讨论，交付一份可直接用于文献调研会议的完整任务提示词。"
    "说明目标、适用范围、可用输入、禁止事项、工作步骤、证据要求、交付内容和质量检查；"
    "只纳入与实际任务相关的规则，不堆积无关条款。"
    "区分不可违反的约束与可调整的偏好；保留模型提出反例、不可行性及前提修正的空间。"
    "不得把人类尚未确认的猜测或预期结论写成事实。"
    "沿用人类在对话中指定的任务语言；未指定时沿用人类当前对话语言。"
    "若仍有未明确但不妨碍开工的事项，在末尾简短列出采用的默认假设。"
    "交付前自行检查内部矛盾、通过改名绕过禁令的风险和交付标准是否可判定。"
    "只输出完整提示词，不输出检查过程；人类修改要求时重写全文，不给孤立补丁。"
)

_DELIBERATION_PROMPT_DIALOGUE_INSTRUCTIONS = (
    "会前任务：通过自然对话协助人类设计一份可直接交给多模型议事会议的委托提示。"
    "这一步只设计问题与交付标准，不预先审议、投票或代替人类作出结论。"
    "不使用角色扮演口吻或固定问卷；每轮最多询问一个会实质改变任务设计的关键问题。"
    "澄清议事要解决的问题、可用材料、拟交付的文书、不可违反的约束、可调整的偏好、"
    "何种分歧应交由代表表决，以及哪些事项必须保留给人类裁定。"
    "已知答案不再追问；模糊或冲突之处用具体例子说明差别，不暗中改变目标。"
    "允许代表提出反例、发现不可行性和反对预设结论；不能把人类尚未确认的判断写成既定事实。"
    "不要调用外部资料，也不要开始正式议事。"
    "使用人类当前对话所用的语言回应；不要因为界面语言设置自行改变成文语言。"
    "当继续追问不太可能改变关键设计时，回复首行写 [READY]，再简短说明可以定稿；"
    "其余轮次只给简短回应和至多一个问题。"
)

_DELIBERATION_PROMPT_DRAFT_INSTRUCTIONS = (
    "依据会前对话，写一份可直接用于多模型议事会议的完整任务委托。"
    "说明议题、范围与排除项、可用输入、硬约束与偏好、预期文书、决策边界及完成判据。"
    "保留代表提出新方案、反例和异议的空间，不预设表决结果；人类保留最终解释权。"
    "不要把未经核实的主张写成事实，不要堆积无关程序规则。"
    "沿用人类在对话中指定的任务语言；未指定时沿用人类当前对话语言。"
    "如仍有不妨碍开工的假设，末尾简短标出。只输出完整委托文本，不输出检查过程；"
    "若人类修改要求则重写全文，不给孤立补丁。"
)


class TerminalInputEncodingError(ValueError):
    """Interactive terminal bytes could not be decoded without data loss."""


def enable_utf8_terminal_erase(stream: TextIO | None = None) -> bool:
    """Make the terminal erase one UTF-8 character, rather than one byte.

    Linux's canonical terminal line editor needs the IUTF8 input flag to know
    how many bytes belong to the character immediately before the cursor.
    Without it, backspacing over Chinese text can leave an invalid UTF-8
    prefix in the input buffer.
    """
    if not sys.platform.startswith("linux"):
        return False
    stream = stream or sys.stdin
    try:
        if not stream.isatty():
            return False
        import termios

        file_descriptor = stream.fileno()
        attributes = termios.tcgetattr(file_descriptor)
        # Python builds do not consistently expose Linux's IUTF8 constant.
        iutf8 = getattr(termios, "IUTF8", 0x4000)
        if not attributes[0] & iutf8:
            attributes[0] |= iutf8
            termios.tcsetattr(file_descriptor, termios.TCSANOW, attributes)
        return True
    except (AttributeError, ImportError, OSError, ValueError):
        # Non-terminal stdin and unsupported platforms retain their original
        # behavior; decoding below still fails closed on malformed input.
        return False


def decode_terminal_input(raw: bytes) -> str:
    """Decode one terminal line without silently replacing invalid characters."""
    encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
    attempted: list[str] = []
    for encoding in encodings:
        normalized_encoding = encoding.lower().replace("_", "-")
        if normalized_encoding in attempted:
            continue
        attempted.append(normalized_encoding)
        try:
            return unicodedata.normalize("NFC", raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise TerminalInputEncodingError(
        "terminal input is not valid UTF-8 or GB18030; configure the terminal to send UTF-8 text"
    )


def terminal_input(prompt: str) -> str:
    """Read one Unicode line with cursor-aware TTY editing when available."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        while True:
            try:
                return _interactive_terminal_input(prompt)
            except TerminalInputEncodingError:
                # A malformed clipboard payload is an input error, not a reason
                # to terminate meeting initialization or consultation.  The
                # rejected bytes are never silently substituted into the task.
                sys.stdout.write(
                    "\r\n输入包含无法识别的字节，尚未保存；请确认终端使用 UTF-8 后重新粘贴。\r\n"
                )
                sys.stdout.flush()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return unicodedata.normalize("NFC", input())
    raw = stream.readline()
    if raw == b"":
        raise EOFError
    return decode_terminal_input(raw).rstrip("\r\n")


def _interactive_terminal_input(prompt: str) -> str:
    """Read a TTY line with the terminal's mature Unicode line editor.

    GNU readline already handles cursor navigation, insertion in the middle of
    a line, wide CJK characters, and wrapped-line redraws.  The previous
    append-only editor emulated those operations by clearing and repainting
    the whole prompt; on a wrapped line that repaint could leave the terminal
    in a pending-wrap state and create an apparent blank line after Backspace.
    Use readline whenever it is available and keep the raw editor below as a
    small dependency-free fallback for platforms without it.
    """
    # ``input()`` only delegates to readline when the process still owns the
    # original standard streams.  Embedders and PTY-based callers often wrap
    # those streams, so use the compatible editor below in that case.
    if sys.stdin is not sys.__stdin__ or sys.stdout is not sys.__stdout__:
        return _interactive_terminal_input_raw(prompt)
    try:
        # Importing readline enables editing for Python's input() on POSIX.
        import readline  # noqa: F401
    except ImportError:
        return _interactive_terminal_input_raw(prompt)
    stream = sys.stdin
    reconfigure = getattr(stream, "reconfigure", None)
    original_encoding = getattr(stream, "encoding", None)
    original_errors = getattr(stream, "errors", None)
    reconfigured = False
    if callable(reconfigure):
        try:
            # surrogateescape preserves any non-UTF-8 clipboard byte so the
            # complete line can still be retried as GB18030 below.  It does not
            # replace or discard bytes.
            reconfigure(encoding="utf-8", errors="surrogateescape")
            reconfigured = True
        except (AttributeError, OSError, ValueError):
            reconfigured = False
    try:
        try:
            value = input(prompt)
        except UnicodeDecodeError as exc:
            raise TerminalInputEncodingError(
                "terminal input could not be decoded by the active terminal encoding"
            ) from exc
    finally:
        if reconfigured:
            restore: dict[str, str] = {}
            if original_encoding:
                restore["encoding"] = original_encoding
            if original_errors:
                restore["errors"] = original_errors
            try:
                reconfigure(**restore)
            except (AttributeError, OSError, ValueError):
                pass
    raw = value.encode("utf-8", errors="surrogateescape")
    return decode_terminal_input(raw)


def _interactive_terminal_input_raw(prompt: str) -> str:
    """Fallback line editor for platforms without GNU readline.

    Kernel canonical editing can remove the correct UTF-8 bytes while leaving one
    display cell behind for a double-width CJK glyph.  Disabling terminal echo and
    redrawing the whole line after every deletion makes the input buffer and the
    visible terminal agree.  Committed IME text and ordinary pasted UTF-8 remain
    supported.  This fallback is intentionally limited; POSIX deployments use
    readline above for full cursor navigation.
    """

    import termios

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    edited = termios.tcgetattr(fd)
    edited[3] &= ~(termios.ICANON | termios.ECHO)
    edited[6][termios.VMIN] = 1
    edited[6][termios.VTIME] = 0
    edited[0] |= getattr(termios, "IUTF8", 0x4000)
    # Contemporary terminals send UTF-8 regardless of the process locale.
    # A GB18030 fallback is attempted only after a complete line is available,
    # avoiding partial-byte corruption during a large paste.
    encoding = "utf-8"
    characters: list[str] = []
    cursor = 0
    pending = bytearray()
    escape_sequence = bytearray()
    try:
        columns = max(1, os.get_terminal_size(fd).columns)
    except OSError:
        columns = 80
    rendered_cells = _terminal_display_width(prompt)

    def redraw() -> None:
        nonlocal rendered_cells
        occupied_rows = max(0, rendered_cells - 1) // columns
        # Clear from the current visual row upward.  Moving down while the
        # cursor is on the terminal's bottom row can scroll the viewport and
        # manufacture a blank line above the prompt on some SSH terminals.
        sequence = "\r\x1b[2K"
        for _ in range(occupied_rows):
            sequence += "\x1b[1A\r\x1b[2K"
        visible = prompt + "".join(characters)
        suffix = "".join(characters[cursor:])
        # The fallback is used primarily for short prompts.  For a wrapped
        # line, clear and repaint the content correctly; placing the cursor
        # within a wrapped suffix is handled conservatively at its nearest
        # horizontal position rather than emitting a literal escape sequence.
        sys.stdout.write(sequence + visible)
        suffix_width = _terminal_display_width(suffix)
        if suffix_width and suffix_width < columns:
            sys.stdout.write(f"\x1b[{suffix_width}D")
        sys.stdout.flush()
        rendered_cells = _terminal_display_width(visible)

    def flush_pending(*, final: bool = False) -> str:
        if not pending:
            return ""
        try:
            decoded = bytes(pending).decode(encoding)
        except UnicodeDecodeError as exc:
            if not final and exc.reason == "unexpected end of data":
                return ""
            if not final:
                return ""
            decoded = decode_terminal_input(bytes(pending))
        pending.clear()
        characters[cursor:cursor] = decoded
        return decoded

    def echo_pending(*, final: bool = False) -> None:
        nonlocal rendered_cells, cursor
        decoded = flush_pending(final=final)
        if decoded:
            cursor += len(decoded)
            redraw()

    def move_vertical(direction: int) -> None:
        """Move by one visual row for the dependency-free fallback editor."""
        nonlocal cursor
        prefix_width = _terminal_display_width(prompt)
        current_width = _terminal_display_width(prompt + "".join(characters[:cursor]))
        target_width = current_width + direction * columns
        total_width = _terminal_display_width(prompt + "".join(characters))
        if target_width < prefix_width or target_width > total_width:
            return
        width = prefix_width
        for index, character in enumerate(characters):
            character_width = _terminal_display_width(character)
            if target_width <= width + character_width:
                cursor = index if target_width - width < character_width / 2 else index + 1
                redraw()
                return
            width += character_width
        cursor = len(characters)
        redraw()

    sys.stdout.write(prompt)
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSANOW, edited)
    try:
        while True:
            chunk = os.read(fd, 64)
            if not chunk:
                echo_pending(final=True)
                if not characters:
                    raise EOFError
                break
            for byte in chunk:
                if escape_sequence:
                    escape_sequence.append(byte)
                    # CSI/SS3 cursor-key sequences end in an ASCII letter or ~.
                    if len(escape_sequence) > 2 and 0x40 <= byte <= 0x7E:
                        sequence = bytes(escape_sequence)
                        escape_sequence.clear()
                        echo_pending(final=True)
                        if sequence in {b"\x1b[D", b"\x1bOD"}:
                            cursor = max(0, cursor - 1)
                            redraw()
                        elif sequence in {b"\x1b[C", b"\x1bOC"}:
                            cursor = min(len(characters), cursor + 1)
                            redraw()
                        elif sequence in {b"\x1b[A", b"\x1bOA"}:
                            move_vertical(-1)
                        elif sequence in {b"\x1b[B", b"\x1bOB"}:
                            move_vertical(1)
                        elif sequence in {b"\x1b[H", b"\x1b[1~", b"\x1bOH"}:
                            cursor = 0
                            redraw()
                        elif sequence in {b"\x1b[F", b"\x1b[4~", b"\x1bOF"}:
                            cursor = len(characters)
                            redraw()
                    continue
                if byte == 0x1B:
                    echo_pending(final=True)
                    escape_sequence.append(byte)
                    continue
                if byte in (0x7F, 0x08):
                    echo_pending(final=True)
                    if cursor:
                        removed = characters[cursor - 1]
                        del characters[cursor - 1]
                        cursor -= 1
                        # Combining marks and variation selectors belong to the
                        # preceding base character, so delete the whole user-
                        # perceived cluster before repainting.
                        if _is_grapheme_modifier(removed):
                            while cursor:
                                previous = characters[cursor - 1]
                                del characters[cursor - 1]
                                cursor -= 1
                                if not _is_grapheme_modifier(previous):
                                    break
                    redraw()
                    continue
                if byte in (0x04,):  # Ctrl-D/Delete at the cursor
                    echo_pending(final=True)
                    if not characters:
                        raise EOFError
                    if cursor < len(characters):
                        del characters[cursor]
                        while cursor < len(characters) and _is_grapheme_modifier(characters[cursor]):
                            del characters[cursor]
                        redraw()
                    continue
                if byte in (0x01,):  # Ctrl-A/Home
                    echo_pending(final=True)
                    cursor = 0
                    redraw()
                    continue
                if byte in (0x05,):  # Ctrl-E/End
                    echo_pending(final=True)
                    cursor = len(characters)
                    redraw()
                    continue
                if byte in (0x0A, 0x0D):
                    echo_pending(final=True)
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return unicodedata.normalize("NFC", "".join(characters))
                if byte == 0x03:
                    sys.stdout.write("^C\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                if byte < 0x20:
                    continue
                pending.append(byte)
                echo_pending()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
    return unicodedata.normalize("NFC", "".join(characters))


def _terminal_display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character) or character in {"\u200c", "\u200d"}:
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _delete_last_grapheme(characters: list[str]) -> None:
    """Delete the last user-perceived cluster sufficiently for CJK/combining input."""
    if not characters:
        return
    removed = characters.pop()
    removed_modifier = _is_grapheme_modifier(removed)
    while removed_modifier and characters:
        removed = characters.pop()
        removed_modifier = _is_grapheme_modifier(removed)


def _is_grapheme_modifier(character: str) -> bool:
    return bool(
        unicodedata.combining(character)
        or character == "\u200d"
        or "\ufe00" <= character <= "\ufe0f"
    )


@dataclass(frozen=True)
class StartupSelection:
    meeting_type: MeetingType
    providers: tuple[str, ...]
    models: tuple[ProviderModel, ...]
    chair_model: ProviderModel | None
    task_description: str
    escalation_email: str | None
    representative_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    chair_reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    research_enabled: bool = False
    research_model: ProviderModel | None = None
    research_reasoning_effort: ReasoningEffort | None = None
    openalex_max_results_per_query: int | None = None
    openalex_quota_policy: Literal["wait", "tavily"] = "wait"
    research_max_concurrent_claim_groups: int | None = None
    maximum_parallelism: bool = False
    decision_rigor: DecisionRigor = DecisionRigor.STRICT
    deliverable_type: DeliverableType = DeliverableType.NORMATIVE_INSTRUMENT
    meeting_title: str | None = None
    report_palette: str = "ocean"
    parent_meeting_path: str | None = None
    inheritance_mode: InheritanceMode | None = None
    rendering_provisional_source: bool = False
    rendering_science_models: tuple[ProviderModel, ...] = ()
    rendering_citation_models: tuple[ProviderModel, ...] = ()
    rendering_language: str | None = None
    rendering_academic_skeleton: bool | None = None
    rendering_full_abstract: bool | None = None
    rendering_section_abstracts: bool | None = None
    rendering_segmentation: int | None = None
    rendering_liveliness: int | None = None
    rendering_output_formats: tuple[str, ...] = ()
    rendering_science_order: tuple[str, ...] = ()
    rendering_science_consultation_authority: str = "human"
    rendering_target_body_characters: int | None = None
    rendering_scope_description: str | None = None
    literature_language: str | None = None
    deliberation_language: str | None = None
    literature_full_abstract: bool | None = None
    literature_section_abstracts: bool | None = None
    literature_segmentation: int | None = None
    literature_liveliness: int | None = None
    literature_signposting: int | None = None
    literature_target_body_characters: int | None = None
    writer_model: ProviderModel | None = None
    writer_reasoning_effort: ReasoningEffort | None = None
    fast_planner_models: tuple[ProviderModel, ...] = ()
    technician_model: ProviderModel | None = None
    technician_reasoning_effort: ReasoningEffort | None = None
    literature_writing_policy: str | None = None
    human_reference_paths: tuple[str, ...] = ()
    fact_first_writing: bool = True
    prompt_development: dict | None = None


def natural_meeting_title(task_description: str, *, max_characters: int = 56) -> str:
    """Derive a stable human-readable title without another provider call."""

    normalized = " ".join(unicodedata.normalize("NFC", task_description).split())
    for prefix in ("请你们", "请你", "请", "我现在想要研究", "我想要研究"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].lstrip("：:，,。 ")
            break
    candidate = normalized.split("\n", 1)[0]
    for delimiter in ("。", "；", ";"):
        head = candidate.split(delimiter, 1)[0].strip()
        if len(head) >= 8:
            candidate = head
            break
    if len(candidate) > max_characters:
        candidate = candidate[: max_characters - 1].rstrip("，,：: ") + "…"
    return candidate or "未命名会议"


def discover_models(config: EnsembleConfig, provider_ids: list[str]) -> list[ModelDescriptor]:
    unknown = sorted(set(provider_ids) - set(config.providers))
    if unknown:
        raise PermanentProviderError(f"selected providers are unavailable or unconfigured: {', '.join(unknown)}")
    selected = {
        provider_id: config.providers[provider_id]
        for provider_id in provider_ids
        if config.providers[provider_id].enabled
    }
    adapters = build_adapters(config.model_copy(update={"providers": selected}), require_keys=True)
    unavailable = sorted(set(provider_ids) - set(adapters))
    if unavailable:
        raise PermanentProviderError(
            f"selected providers are unavailable or disabled: {', '.join(unavailable)}"
        )
    models: list[ModelDescriptor] = []
    for provider_id in provider_ids:
        discovered = adapters[provider_id].list_models()
        provider = config.providers[provider_id]
        required_method = (
            "generateContent" if provider.kind == "gemini"
            else "codex/app-server" if provider.kind == "codex_subscription"
            else "claude/print" if provider.kind == "claude_code"
            else "chat/completions"
        )
        # Provider catalogs may also include embedding, realtime, music, video,
        # or other models that the configured adapter cannot invoke.
        usable = [
            model
            for model in discovered
            if not model.supported_methods or required_method in model.supported_methods
        ]
        configured = provider.selectable_models
        if configured is not None:
            by_id = {model.model_id: model for model in usable}
            usable = [by_id[model_id] for model_id in configured if model_id in by_id]
            if not usable:
                raise PermanentProviderError(
                    f"none of the configured selectable_models for {provider_id} "
                    "were returned by live model discovery"
                )
        models.extend(usable)
    if not models:
        raise PermanentProviderError("the selected providers returned no available models")
    return models


class StartupWizardCancelled(Exception):
    """The Human went back from the first setup step to the home menu."""


class _StartupWizardBack(Exception):
    """Restart the in-memory setup form at the preceding answered prompt."""


class TerminalWizard:
    """Dependency-free terminal UI so startup remains usable over SSH."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
        output: TextIO | None = None,
        color: bool | None = None,
    ):
        self.language = interface_language() or "zh"
        raw_input = input_fn or terminal_input
        self.input = lambda prompt: raw_input(ui_text(prompt, self.language))
        self.output = output or sys.stdout
        self.color = supports_color(self.output, color)
        self.width = rule_width(self.output)
        self.discovered_catalog: list[ModelDescriptor] = []

    def _print(self, text: str = "") -> None:
        print(ui_text(text, self.language), file=self.output)

    def _dialogue_reply(self, chinese_role: str, english_role: str, text: str) -> None:
        """Give each model reply a visible boundary from the Human's next turn."""
        role = english_role if self.language == "en" else chinese_role
        self._print()
        self._print(styled(f"  ── {role} ──", CYAN, enabled=self.color))
        self._print(text)
        self._print()

    def _dialogue_human_turn(self, text: str) -> None:
        """Show the complete committed input, even when terminal paste echo is clipped."""
        role = "You · sent" if self.language == "en" else "你 · 已发送"
        self._print()
        self._print(styled(f"  ── {role} ──", GREEN, enabled=self.color))
        print(text, file=self.output)
        self._print()

    def _section(self, title: str) -> None:
        title = ui_text(title, self.language)
        heading = f"┌─ {title} "
        line = heading + "─" * max(1, self.width - len(heading))
        self._print("\n" + styled(line, BOLD, CYAN, enabled=self.color))

    def show_home(self) -> None:
        """Introduce the project before asking the Human to choose a workflow."""
        self._print(styled("━" * self.width, CYAN, enabled=self.color))
        title = "  Project ENSEMBLE · Multi-model research and deliberation" if self.language == "en" else "  Project ENSEMBLE · 多模型研究与审议工作台"
        self._print(styled(title, BOLD, CYAN, enabled=self.color))
        self._print(styled("━" * self.width, CYAN, enabled=self.color))
        if self.language == "en":
            self._print("  Independent models plan, review evidence, and vote; the Chair coordinates while you retain final control.")
            self._print("  Create a deliberative document, a full literature review, or a scholarly rendering of an existing report.")
            self._print("  Claim verification checks one question; it is not a full literature review.")
            self._print("  Meetings and evidence are saved, so you can resume, ask the Chair, or start a successor meeting.")
            self._print("  Interactive HTML is the default rich-text reading format: one browser-openable file with navigation, reference cards, annotations, and optional Q&A.")
            self._print(styled("  New meetings are created in the current directory; frozen work is not repeated on resume.", DIM, enabled=self.color))
            self._print(styled("  PDFs use bundled HarmonyOS Sans SC, a versatile font available for royalty-free commercial use under its accompanying license.", DIM, enabled=self.color))
            return
        self._print("  多个模型分别提出方案、审阅证据和表决；主席组织流程，人类保留介入权。")
        self._print("  可形成规范性文书、完整文献综述，或把已有报告重绘为更易阅读的学术文本。")
        self._print("  单项命题核实只查证一个可检验的问题，不等同于撰写文献综述。")
        self._print("  会议进度与资料会保存下来；可以稍后继续，也可以接续会议向主席提问。")
        self._print("  默认富文本阅读版是 HTML：单文件、浏览器可打开，提供目录、引文释义、高亮批注与可选问答。")
        self._print(styled("  新会议在当前目录创建；接续会议不会重做已经冻结的工作。", DIM, enabled=self.color))
        self._print(styled("  PDF 内置 HarmonyOS Sans SC，一款可免费商用的全场景字体；具体使用条件见随附许可。", DIM, enabled=self.color))

    def ensure_language_selected(self) -> None:
        """Ask only once per user profile, before loading any model configuration."""
        saved = interface_language()
        if saved is not None:
            self.language = saved
            return
        self._print("Project ENSEMBLE · 首次使用 / First launch")
        self._print("此处仅选择界面语言。每次会议的成文语言在该会议初始化时独立选择；两者互不联动。")
        self._print("This choice affects only the interface. Choose the writing language separately when starting each meeting; the two settings do not change each other.")
        language = self._choose_one("界面语言 / Interface language", [
            ("zh", "中文"), ("en", "English"),
        ])
        save_interface_language(language)
        self.language = language

    def show_meeting_guide(self) -> None:
        """Explain workflow and model assignments before the first setup choice."""
        if self.language == "en":
            self._section("How meetings work and how to choose models")
            self._print("  Deliberation is for drafting rules, requirements, evaluation criteria, or a tightly specified prompt brief.")
            self._print("  Independent representatives propose, challenge, amend, and vote on clauses; the Chair applies accepted decisions.")
            self._print("  You retain reserved decisions. The output is an auditable normative document, not an executed task or literature review.")
            self._print("  Literature review: plan modules, retrieve evidence, draft, review science and citations, then publish.")
            self._print("  Fast review uses one writer and a smaller review loop; rendering improves an existing report without replacing its source.")
            self._print("  Claim verification checks one externally answerable proposition through Research Desk.")
            self._print("  Choose diverse base models for independent viewpoints; use a strong writing model for the writer and a reliable organizer for Chair.")
            self._print("  Research Desk needs dependable retrieval and source handling; Technician, if enabled, should be your best coding model.")
            self._print("  More models and stronger reasoning can increase time and cost. Interface language does not set report language.")
            return
        self._section("会议如何进行，以及如何选择模型")
        self._print("  议事会议适合制定规则、任务要求、评价标准或高度约束的提示词委托。")
        self._print("  不同模型的代表逐条提案、质疑、修订并表决；主席落实通过的条款，保留事项交你裁定。")
        self._print("  交付可审计的规范性文书，不代你执行任务，也不默认生成文献综述。")
        self._print("  文献调研先规划模块，再检索证据、写作、核校科学事实与引文，最后出版。")
        self._print("  快速调研由一位主笔和精简审阅组完成；学术重绘改善已有报告，保留原稿。")
        self._print("  命题核实只让 Research Desk 查证一个可由外部资料回答的主张。")
        self._print("  基础模型尽量选不同模型以保持独立判断；主笔重写作能力，主席重组织与程序能力。")
        self._print("  Research Desk 重检索与来源处理；如启用 Technician，建议选择最擅长编程排障的模型。")
        self._print("  更多模型和更高推理强度可能增加耗时与费用；界面语言不决定报告语言。")

    def _show_selected_workflow(self, choice: str) -> None:
        """Explain what happens to the Human's request before model setup."""
        workflows = {
            "fast_literature_review": (
                "你的需求先交给两至三个独立模型，各自做一轮探索性检索并提出模块拆分方案；"
                "学术主笔只看匿名方案，自行整理成可审阅的任务书与模块范围。你确认后，主笔逐模块提出问题，"
                "Research Desk 检索并核查来源。主笔按知识卡和文献写作，独立模型复核科学问题，"
                "最后组装综述、术语表和引文。范围与未解决的关键争议会交还给你决定。",
                "Two or three independent models each perform one exploratory search and propose a module split. "
                "The writer sees only anonymous proposals and independently drafts a reviewable taskbook. After your approval, "
                "the writer asks module questions, Research Desk checks sources, and the writer drafts "
                "from evidence cards. Independent models review science before the report, glossary, "
                "and references are assembled. You decide unresolved scope or material disputes.",
            ),
            "new_literature_review": (
                "你的需求先由主席和不同模型规划并拆分为研究模块，模块总纲由你批准；"
                "随后逐模块检索、制定提纲、写作及审阅科学事实与引文，最终组装综述。"
                "关键范围变更和无法解决的争议会暂停请你裁定。",
                "The Chair and independent models plan modules from your request; you approve the outline. "
                "Each module then goes through retrieval, writing outline, drafting, science review, "
                "and citation review before report assembly. Material scope changes or unresolved disputes return to you.",
            ),
            "derived_literature_review": (
                "系统先只读继承你选定会议的资料，再按完整文献调研流程规划、核查、写作和审阅；"
                "源会议的冻结文书不会改动，最终交付一份新的综述。",
                "The selected meeting's materials are inherited read-only. The full review workflow then "
                "plans, checks, drafts, and reviews a new report without changing frozen source records.",
            ),
            "scholarly_rendering": (
                "系统保留原报告，由主席分段重绘以改善学术表达；科学组和引文组依次核校，"
                "争议按规则表决或交你处理。最终输出可读版本，同时保留原文供对照。",
                "The original report is preserved while the Chair redraws it in sections. Science and "
                "citation reviewers check the changes; unresolved disputes are voted on or returned to you. "
                "The readable output remains traceable to the source.",
            ),
            MeetingType.RESEARCH.value: (
                "你提交一项可由外部资料回答的具体主张；Research Desk 检索支持、反证和适用范围，"
                "交付证据包及未能核实的边界，不撰写完整综述。",
                "Submit one externally answerable claim. Research Desk searches support, counterevidence, "
                "and scope, then returns an evidence packet and unresolved limits rather than a full review.",
            ),
            MeetingType.DELIBERATION.value: (
                "适用于把规则、研究协作要求、评价准则或复杂提示词委托写成可表决的条文。"
                "独立代表提案、质疑、修订和逐项表决；主席只整合通过的决定，人类裁定保留的范围与程序争议。"
                "交付的是带有决策轨迹的规范性文书，不是替你执行提示词、搜索文献或证明经验事实。",
                "Use this when rules, research-work requirements, evaluation criteria, or a complex prompt brief "
                "need clause-by-clause decisions. Independent representatives propose, challenge, amend, and vote. "
                "The Chair assembles accepted decisions; you decide reserved scope and procedural disputes. "
                "The deliverable is an auditable normative document, not execution of the prompt, a literature search, "
                "or proof of empirical claims.",
            ),
        }
        zh, en = workflows.get(choice, workflows["new_literature_review"])
        self._section("本次需求将如何处理" if self.language == "zh" else "How this meeting will handle your request")
        self._print("  " + (en if self.language == "en" else zh))

    def _option(self, index: int, value: str, description: str) -> None:
        description = ui_text(description, self.language)
        number = styled(f"{index:>2}.", BOLD, GREEN, enabled=self.color)
        # Hide internal enum-like option codes when a Human-readable Chinese
        # label is already present; keep model IDs and paths visible.
        if (
            (re.fullmatch(r"[a-z][a-z_]*", value) or value.startswith("/"))
            and (
                value in {"zh", "en", "fr"}
                or self.language == "en"
                or re.search(r"[\u3400-\u9fff]", description)
            )
        ):
            self._print(f"  {number} {description}")
        else:
            self._print(f"  {number} {value} {styled('—', DIM, enabled=self.color)} {description}")

    def _choose_one(self, title: str, options: list[tuple[str, str]]) -> str:
        self._section(title)
        for index, (value, description) in enumerate(options, start=1):
            self._option(index, value, description)
        while True:
            raw = self.input("选择编号: ").strip()
            if raw.lower() == "b" and any(value == "back" for value, _ in options):
                return "back"
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            hint = "（或输入 b 返回）" if any(value == "back" for value, _ in options) else ""
            if self.language == "en":
                self._print(styled("Enter one listed number" + (" (or b to go back)" if hint else "") + ".", YELLOW, enabled=self.color))
            else:
                self._print(styled(f"请输入列表中的一个编号{hint}。", YELLOW, enabled=self.color))

    def _choose_source_meeting(self, config: EnsembleConfig) -> str:
        """Offer registered and nearby meetings, retaining an ID/path fallback."""
        config_path = config.source_path or Path.cwd() / "ensemble.toml"
        paths = [Path(entry.path) for entry in indexed_meetings(config_path)]
        paths.extend(discover_local_meetings(Path.cwd()))
        entries = []
        seen: set[Path] = set()
        for path in paths:
            try:
                entry = inspect_meeting(path)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            resolved = Path(entry.path)
            if resolved not in seen:
                seen.add(resolved)
                entries.append(entry)
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        if entries:
            options = [
                (
                    entry.path,
                    f"{entry.meeting_id} · {entry.title} · "
                    f"{'已完成' if meeting_is_complete(entry.path) else '进行中'} · {entry.path}",
                )
                for entry in entries
            ]
            options.append(("manual", "手动输入源会议 ID、标题或目录"))
            choice = self._choose_one("选择需要接续的源会议", options)
            if choice != "manual":
                return choice
        while True:
            candidate = self.input("输入源会议 ID、标题或目录: ").strip()
            if candidate:
                resolved = resolve_indexed_meeting(candidate, config_path)
                if resolved is not None:
                    return str(resolved)
            self._print(styled("未找到有效的 ENSEMBLE 会议；请核对会议 ID 或目录。", RED, enabled=self.color))

    def _chair_title(self, config: EnsembleConfig, chair_model: ProviderModel | None,
                     chair_effort: ReasoningEffort, task: str, *,
                     role_label: str = "Chair") -> str | None:
        """Ask a setup model for a short title; never block initialization on failure."""
        if chair_model is None:
            return None
        provider_id, model_id = chair_model
        provider = config.providers[provider_id]
        if provider.kind not in {"codex_subscription", "claude_code"} and not provider.api_key():
            return None
        self._print(
            f"\nAsking {role_label} to propose a meeting title from the final brief..."
            if self.language == "en" else
            f"\n正在请 {role_label} 根据最终任务委托拟定会议标题……"
        )
        try:
            adapter = build_adapters(
                config.model_copy(update={"providers": {provider_id: provider}}),
                require_keys=True,
            )[provider_id]
            response = adapter.generate(GenerationRequest(
                model_id=model_id,
                system_text=("请为人类发起的 ENSEMBLE 会议拟定一个准确、中性的短标题，"
                             "语言与任务描述一致。只输出一行标题，不要编号、引号、解释或承诺研究结论；"
                             "标题最多 56 个字符。"),
                user_text=task[:12000],
                reasoning_effort=chair_effort,
                max_output_tokens=1024,
            ))
            title = " ".join(response.text.strip().strip('"“”「」').split())
            if not title or "\n" in response.text.strip() or len(title) > 72:
                return None
            return title
        except Exception as exc:
            message = (
                f"{role_label} could not propose a title; using a title derived from the brief "
                f"({type(exc).__name__})." if self.language == "en" else
                f"{role_label} 拟题未成功，改用任务描述生成默认标题（{type(exc).__name__}）。"
            )
            self._print(styled(message, YELLOW, enabled=self.color))
            return None

    def _develop_research_claim(
        self,
        config: EnsembleConfig,
        catalog_options: list[tuple[str, str]],
        research_model: ProviderModel,
        research_effort: ReasoningEffort,
    ) -> tuple[str | None, dict | None]:
        """Let the Human refine a one-shot claim before any meeting is created."""
        self._section("会前整理命题 · 与筹备主席对话")
        self._print("  这只是会前写作协助，不是事实核验；筹备主席不参与正式命题核实。")
        self._print("  只有你确认的最终提问会交给 Research Desk。")
        model_choice = self._choose_one(
            "选择筹备主席模型",
            [
                ("research", "使用刚选定的 Research Desk 模型"),
                ("other", "从已发现模型中另选一个"),
            ],
        )
        if model_choice == "research":
            advisor_model = research_model
            advisor_effort = research_effort
        else:
            advisor_model = self._parse_provider_model(
                self._choose_one("选择筹备主席使用的模型", catalog_options)
            )
            advisor_effort = ReasoningEffort(
                self._choose_one(
                    "设置筹备主席的 reasoning effort",
                    self._reasoning_options(config, (advisor_model,)),
                )
            )
        provider_id, model_id = advisor_model
        try:
            provider = config.providers[provider_id]
            adapter = build_adapters(
                config.model_copy(update={"providers": {provider_id: provider}}),
                require_keys=True,
            )[provider_id]
        except Exception as exc:
            self._print(styled(
                f"筹备主席模型暂不可用（{type(exc).__name__}）；请直接输入最终命题。",
                YELLOW, enabled=self.color,
            ))
            return None, None

        turns: list[dict[str, str]] = []
        self._print("  写下研究想法即可，不必一次成稿。输入 /draft 查看候选命题；/back 改为直接输入。")
        self._print("  发送后会完整回显你的发言，方便核对长段或多行粘贴。")
        self._print()
        while True:
            message = unicodedata.normalize("NFC", self.input("你: ")).strip()
            if message.lower() in {"/back", "/cancel"}:
                return None, None
            if not message:
                continue
            if message.lower() == "/draft":
                if not turns:
                    self._print(styled("先描述你希望核查的问题。", YELLOW, enabled=self.color))
                    continue
                instruction = (
                    "基于会前对话，拟写一项可由外部资料核查的具体命题或问句。"
                    "保留人类给出的对象、时间、范围和限制；没有说清的条件不要编造。"
                    "只输出最终提问，不输出解释、前言、编号或多项任务。"
                )
            else:
                turns.append({"role": "human", "text": message})
                self._dialogue_human_turn(message)
                instruction = (
                    "协助人类把研究想法收敛为可由外部资料核查的一项具体命题。"
                    "简短回应，必要时只问一个最关键的澄清问题。"
                    "可以讨论措辞和核查范围，但不要假装已完成检索或断定事实真假。"
                    "不要把开放性立场委托直接改写为预设结论。"
                )
            transcript = "\n".join(
                f"{'人类' if turn['role'] == 'human' else '筹备主席'}：{turn['text']}"
                for turn in turns
            )
            # The initial intention and most recent turns matter more than an
            # unbounded chat history; never send an endlessly growing prompt.
            if len(transcript) > 24000:
                transcript = transcript[:4000] + "\n[中间对话已省略]\n" + transcript[-20000:]
            try:
                response = adapter.generate(GenerationRequest(
                    model_id=model_id,
                    system_text=instruction,
                    user_text=transcript,
                    reasoning_effort=collapse_reasoning_effort(config, advisor_model, advisor_effort),
                ))
                reply = unicodedata.normalize("NFC", response.text).strip()
                if not reply:
                    raise ValueError("模型未返回可读文本")
            except Exception as exc:
                if message.lower() != "/draft":
                    turns.pop()
                self._print(styled(
                    f"筹备主席本次未能回应（{type(exc).__name__}）；可以重试、输入 /draft，或 /back 直接填写。",
                    YELLOW, enabled=self.color,
                ))
                continue
            if message.lower() != "/draft":
                turns.append({"role": "advisor", "text": reply})
                self._dialogue_reply("筹备主席", "Preparatory Chair", reply)
                continue
            candidate = " ".join(reply.split())
            if len(candidate) > 3000:
                self._print(styled("候选命题过长；请继续讨论并要求缩小范围。", YELLOW, enabled=self.color))
                continue
            turns.append({"role": "advisor_draft", "text": candidate})
            self._section("待确认的最终核查命题")
            print(f"  {candidate}", file=self.output)
            decision = self._choose_one(
                "如何处理这份候选命题",
                [
                    ("confirm", "确认采用；创建会议后才启动核实"),
                    ("continue", "继续与筹备主席讨论"),
                    ("edit", "自行改写最终提问并确认"),
                    ("direct", "放弃这次对话，直接输入最终提问"),
                ],
            )
            if decision == "continue":
                continue
            if decision == "direct":
                return None, None
            if decision == "edit":
                while True:
                    final = unicodedata.normalize("NFC", self.input("你确认的最终提问: ")).strip()
                    if final:
                        break
                    self._print(styled("最终提问不能为空。", YELLOW, enabled=self.color))
            else:
                final = candidate
            return final, {
                "kind": "RESEARCH_CLAIM_PREFLIGHT",
                "advisor_model": f"{provider_id}:{model_id}",
                "advisor_reasoning_effort": advisor_effort.value,
                "turns": turns,
                "confirmed_task": final,
            }

    def _develop_literature_task(
        self,
        config: EnsembleConfig,
        chair_model: ProviderModel,
        chair_effort: ReasoningEffort,
        *,
        deliberation: bool = False,
        preparatory: bool = False,
        source_context: str | None = None,
    ) -> tuple[str | None, dict | None]:
        """Discuss a meeting brief with its selected Chair before startup."""
        self._section(
            "会前任务设计 · 与筹备主席对话" if preparatory
            else ("会前议题设计 · 与本次会议主席对话" if deliberation
                  else "会前任务设计 · 与本次会议主席对话")
        )
        if preparatory:
            self._print("  筹备主席只协助拟定开题委托；正式简易会议不任命主席。")
        self._print(
            "  这一步只设计议事委托，不开始提案、审议或表决。" if deliberation
            else "  这一步只设计研究任务，不开始文献检索或撰写报告。"
        )
        self._print("  每次可只说一部分想法；/draft 可随时生成当前最佳版本，/back 改为直接输入。")
        self._print("  发送后会完整回显你的发言，方便核对长段或多行粘贴。")
        provider_id, model_id = chair_model
        try:
            provider = config.providers[provider_id]
            adapter = build_adapters(
                config.model_copy(update={"providers": {provider_id: provider}}),
                require_keys=True,
            )[provider_id]
        except Exception as exc:
            self._print(styled(
                f"主席模型暂不可用（{type(exc).__name__}）；请直接输入最终任务。",
                YELLOW, enabled=self.color,
            ))
            return None, None

        turns: list[dict[str, str]] = []

        def context() -> str:
            transcript = "\n".join(
                f"{'人类' if turn['role'] == 'human' else '主席'}：{turn['text']}"
                for turn in turns
            )
            if source_context:
                transcript = (
                    "来源会议概览（只供设计新研究题目，不代表本次证据已核实）：\n"
                    + source_context + "\n\n" + transcript
                )
            if len(transcript) <= 64000:
                return transcript
            # Preserve human decisions before retaining recent discussion. The
            # confirmed final prompt is displayed in full before creation.
            human_text = "\n".join(
                f"人类：{turn['text']}" for turn in turns if turn["role"] == "human"
            )
            if len(human_text) > 48000:
                human_text = human_text[:24000] + "\n[部分较早发言已省略]\n" + human_text[-24000:]
            recent = "\n".join(
                f"{'人类' if turn['role'] == 'human' else '主席'}：{turn['text']}"
                for turn in turns[-12:]
            )[-16000:]
            return (source_context + "\n\n" if source_context else "") + human_text + "\n[最近对话，可能与上文重复]\n" + recent

        self._print()
        while True:
            message = unicodedata.normalize("NFC", self.input("你: ")).strip()
            if message.lower() in {"/back", "/cancel"}:
                return None, None
            if not message:
                continue
            drafting = message.lower() == "/draft"
            if drafting and not any(turn["role"] == "human" for turn in turns):
                self._print(styled(
                    "先说说希望会议讨论什么、最终交付什么。" if deliberation
                    else "先说说希望调研什么，以及报告给谁使用。",
                    YELLOW, enabled=self.color,
                ))
                continue
            if not drafting:
                turns.append({"role": "human", "text": message})
                self._dialogue_human_turn(message)
            try:
                if not drafting:
                    response = adapter.generate(GenerationRequest(
                        model_id=model_id,
                        system_text=(
                            _DELIBERATION_PROMPT_DIALOGUE_INSTRUCTIONS if deliberation
                            else _LITERATURE_PROMPT_DIALOGUE_INSTRUCTIONS
                        ),
                        user_text=context(),
                        reasoning_effort=collapse_reasoning_effort(config, chair_model, chair_effort),
                    ))
                    reply = unicodedata.normalize("NFC", response.text).strip()
                    if not reply:
                        raise ValueError("模型未返回可读文本")
                    ready = reply.startswith("[READY]")
                    if ready:
                        reply = reply.removeprefix("[READY]").strip()
                    turns.append({"role": "chair", "text": reply or "关键设计已经明确，可以生成当前最佳版本。"})
                    self._dialogue_reply("主席", "Chair", turns[-1]["text"])
                    if not ready:
                        continue
                response = adapter.generate(GenerationRequest(
                    model_id=model_id,
                    system_text=(
                        _DELIBERATION_PROMPT_DRAFT_INSTRUCTIONS if deliberation
                        else _LITERATURE_PROMPT_DRAFT_INSTRUCTIONS
                    ),
                    user_text=context(),
                    reasoning_effort=collapse_reasoning_effort(config, chair_model, chair_effort),
                ))
                candidate = unicodedata.normalize("NFC", response.text).strip()
                if not candidate:
                    raise ValueError("模型未返回提示词")
            except Exception as exc:
                self._print(styled(
                    f"主席本次未能回应（{type(exc).__name__}）；可以重试、输入 /draft，或 /back 直接填写。",
                    YELLOW, enabled=self.color,
                ))
                continue
            turns.append({"role": "chair_draft", "text": candidate})
            self._section(
                "完整候选议事委托 · 尚未启动会议" if deliberation
                else "完整候选研究任务 · 尚未启动会议"
            )
            print(candidate, file=self.output)
            decision = self._choose_one(
                "如何处理主席提出的完整版本",
                [
                    ("confirm", "确认采用并继续初始化"),
                    ("continue", "继续讨论或提出修改意见；主席须重写整份任务"),
                    ("direct", "放弃对话，改为直接输入任务"),
                ],
            )
            if decision == "continue":
                continue
            if decision == "direct":
                return None, None
            return candidate, {
                "kind": "DELIBERATION_PROMPT_PREFLIGHT" if deliberation else "LITERATURE_PROMPT_PREFLIGHT",
                "chair_model": f"{provider_id}:{model_id}",
                "chair_reasoning_effort": chair_effort.value,
                "turns": turns,
                "confirmed_task": candidate,
            }

    def _choose_many(
        self,
        title: str,
        options: list[tuple[str, str]],
        *,
        blank_means_all: bool = False,
        blank_means_none: bool = False,
        default_values: list[str] | None = None,
    ) -> list[str]:
        self._section(title)
        for index, (value, description) in enumerate(options, start=1):
            self._option(index, value, description)
        while True:
            prompt = "选择一个或多个编号（逗号分隔）"
            if blank_means_all:
                prompt += "，直接回车选择全部"
            elif blank_means_none:
                prompt += "，直接回车跳过"
            elif default_values:
                prompt += f"，直接回车使用默认值 {', '.join(default_values)}"
            raw = self.input(f"{prompt}: ").strip()
            if blank_means_all and not raw:
                return [value for value, _ in options]
            if blank_means_none and not raw:
                return []
            if default_values and not raw:
                return list(default_values)
            try:
                indexes = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                indexes = []
            if indexes and len(indexes) == len(set(indexes)) and all(1 <= x <= len(options) for x in indexes):
                return [options[x - 1][0] for x in indexes]
            self._print(styled("请输入不重复的有效编号，例如 1,3。", YELLOW, enabled=self.color))

    def collect(self, *args, **kwargs) -> StartupSelection:
        """Offer `b` at every setup prompt without creating a partial meeting.

        Replaying earlier answers reconstructs dependent menus after a change;
        the answer immediately before `b` is discarded and asked again.
        """
        original_input = self.input
        history: list[str] = []
        self._setup_history = history
        self._setup_cursor = 0
        self._setup_effect_cache: dict[tuple, tuple[object, int, tuple[str, ...]]] = {}

        def navigable_input(prompt: str) -> str:
            cursor = self._setup_cursor
            if cursor < len(history):
                answer = history[cursor]
                self._setup_cursor += 1
                return answer
            answer = original_input(prompt)
            if answer.strip().lower() == "b":
                if not history:
                    raise StartupWizardCancelled()
                history.pop()
                raise _StartupWizardBack()
            history.append(answer)
            self._setup_cursor += 1
            return answer

        self.input = navigable_input
        try:
            while True:
                self._setup_cursor = 0
                try:
                    return self._collect_once(*args, **kwargs)
                except _StartupWizardBack:
                    self._print("已返回上一步；此前有效的选择仍保留。" if self.language == "zh"
                                else "Back one step; earlier choices are retained.")
        finally:
            self.input = original_input
            del self._setup_history, self._setup_cursor, self._setup_effect_cache

    def _cached_setup_effect(self, name: str, callback):
        """Avoid a second model call when navigation merely replays a dialogue."""
        history = getattr(self, "_setup_history", None)
        if history is None:
            return callback()
        start = self._setup_cursor
        key = (name, tuple(history[:start]))
        cached = self._setup_effect_cache.get(key)
        if cached is not None:
            value, end, answers = cached
            if len(history) >= end and tuple(history[:end]) == answers:
                self._setup_cursor = end
                return value
        value = callback()
        self._setup_effect_cache[key] = (value, self._setup_cursor,
                                         tuple(history[:self._setup_cursor]))
        return value

    @staticmethod
    def _source_meeting_topic_context(parent_meeting_path: str | None) -> str | None:
        """A small orientation note for a successor's pre-meeting topic dialogue."""
        if not parent_meeting_path:
            return None
        root = Path(parent_meeting_path)
        parts: list[str] = []
        task_path = root / "public/task.json"
        if task_path.is_file():
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
                description = str(task.get("description") or "").strip()
                if description:
                    parts.append("源会议原任务：" + description[:2500])
            except (OSError, ValueError, TypeError):
                pass
        for relative in (
            "public/final/literature_review_report.md",
            "public/final/scholarly_rendering/scholarly_review.md",
            "public/final/final_report.md",
        ):
            path = root / relative
            if path.is_file():
                try:
                    parts.append("已完成文稿开头（摘录，不是全文）：\n"
                                 + path.read_text(encoding="utf-8")[:3500])
                except (OSError, UnicodeError):
                    pass
                break
        return "\n\n".join(parts) or None

    def _collect_once(
        self,
        config: EnsembleConfig,
        *,
        parent_meeting_path: str | None = None,
        inheritance_mode: InheritanceMode | None = None,
        forced_meeting_type: MeetingType | None = None,
        provisional_rendering_source: bool = False,
        prompt_for_title: bool = False,
        prompt_for_references: bool = False,
        prompt_for_claim_dialogue: bool = False,
        prompt_for_literature_dialogue: bool = False,
        prompt_for_deliberation_dialogue: bool = False,
    ) -> StartupSelection:
        self._print(styled("━" * self.width, CYAN, enabled=self.color))
        heading = "  Project ENSEMBLE · Meeting setup" if self.language == "en" else "  Project ENSEMBLE · 会议初始化"
        self._print(styled(heading, BOLD, CYAN, enabled=self.color))
        self._print(styled("━" * self.width, CYAN, enabled=self.color))
        self.show_meeting_guide()
        self._print("  配置过程中输入 b 可返回上一个提示；已完成的选择会保留。" if self.language == "zh"
                    else "  Enter b at any setup prompt to return one step; earlier choices are retained.")
        meeting_options = [
            (
                "new_literature_review",
                "文献调研：从新任务出发，选择简易或完整流程撰写综述报告",
            ),
            (
                "derived_literature_review",
                "接续文献调研：继承既有会议资料，选择简易或完整流程撰写综述报告",
            ),
            (
                "scholarly_rendering",
                "学术化重绘：继承已完成的报告，由 Chair 重绘并经科学/引文核校",
            ),
            (MeetingType.RESEARCH.value, "命题核实：由 Research Desk 查证一项可检验的主张"),
            (MeetingType.DELIBERATION.value,
             "议事会议：为规则、标准或提示词委托逐条提案与表决，形成规范性文书"),
        ]
        derived_report = False
        if forced_meeting_type is not None:
            if forced_meeting_type == MeetingType.SCHOLARLY_RENDERING:
                meeting_choice = forced_meeting_type.value
                label = "学术化重绘"
            elif forced_meeting_type == MeetingType.DELIBERATION:
                meeting_choice = "derived_literature_review"
                label = "接续文献调研报告"
            else:
                raise ValueError("direct continuation supports literature review or scholarly rendering")
            if not parent_meeting_path:
                raise ValueError("direct continuation requires a source meeting")
            self._section(f"1/15 已选择{label}")
            self._print(f"  来源会议：{parent_meeting_path}；原文与证据包保持只读。")
            if provisional_rendering_source:
                self._print(styled(
                    "  注意：源会议未完成最终可读性认证。此处继承的是冻结的主席修改稿，不是正式报告。",
                    YELLOW, enabled=self.color,
                ))
        else:
            meeting_choice = self._choose_one(
                "1/10 选择会议类型",
                meeting_options,
            )
            if meeting_choice in {"new_literature_review", "derived_literature_review"}:
                derived_report = meeting_choice == "derived_literature_review"
                literature_flow = self._choose_one(
                    "选择接续文献调研流程" if derived_report else "选择文献调研流程",
                    [
                        ("fast_literature_review", "简易流程：继承源会议资料，由单一主笔写作并接受多模型科学复核"
                         if derived_report else "简易流程：单一主笔、Research Desk 与多模型科学复核"),
                        ("new_literature_review", "完整流程：继承源会议资料，进行多代表规划、检索、审议与核校"
                         if derived_report else "完整流程：多代表规划、检索、审议、写作与核校"),
                    ],
                )
                meeting_choice = literature_flow
            else:
                derived_report = False
        if forced_meeting_type is not None and meeting_choice == "derived_literature_review":
            derived_report = True
            meeting_choice = self._choose_one(
                "选择接续文献调研流程",
                [
                    ("fast_literature_review", "简易流程：继承源会议资料，由单一主笔写作并接受多模型科学复核"),
                    ("new_literature_review", "完整流程：继承源会议资料，进行多代表规划、检索、审议与核校"),
                ],
            )
        fast_report = meeting_choice == "fast_literature_review"
        self._show_selected_workflow(meeting_choice)
        if derived_report:
            self._print("  源会议资料将以只读方式继承；源会议的冻结文书不会改动。")
        literature_report = meeting_choice in {
            "new_literature_review",
            "fast_literature_review",
        }
        literature_prompt_route: str | None = None
        if literature_report and prompt_for_literature_dialogue:
            self._print("  先选择如何确定研究题目；若选择对话，将在指定主笔／主席模型后开始交流。"
                        if self.language == "zh" else
                        "  Choose how to define the research topic now; dialogue starts after model selection.")
            literature_prompt_route = self._choose_one(
                "接续文献调研 · 如何确认新的研究题目" if derived_report
                else "文献调研 · 如何确认研究题目",
                [
                    ("direct", "直接输入已拟好的研究委托"),
                    ("dialogue", "与 AI 逐步讨论，确认本次研究题目和完整委托"),
                ],
            )
        scholarly_rendering = meeting_choice == "scholarly_rendering"
        meeting_type = (
            MeetingType.DELIBERATION
            if literature_report
            else (
                MeetingType.SCHOLARLY_RENDERING
                if scholarly_rendering
                else MeetingType(meeting_choice)
            )
        )
        deliverable_type = (
            DeliverableType.LITERATURE_REVIEW
            if literature_report
            else (
                DeliverableType.SCHOLARLY_RENDERING
                if scholarly_rendering
                else DeliverableType.NORMATIVE_INSTRUMENT
            )
        )

        enabled = [(pid, self._provider_description(pid, config)) for pid, cfg in config.providers.items() if cfg.enabled]
        if not enabled:
            raise PermanentProviderError("configuration contains no enabled providers")
        providers = self._choose_many("2/10 选择模型供应商", enabled, blank_means_all=True)

        self._print("\n" + styled("正在从所选供应商实时发现模型……", CYAN, enabled=self.color))
        catalog = discover_models(config, providers)
        self.discovered_catalog = list(catalog)
        counts = {provider_id: 0 for provider_id in providers}
        for model in catalog:
            counts[model.provider_id] += 1
        self._print("可选模型数量：" + "，".join(f"{provider_id} {counts[provider_id]} 个" for provider_id in providers))
        catalog_options = [
            (f"{m.provider_id}:{m.model_id}", self._model_description(m))
            for m in catalog
        ]
        selected_models: tuple[ProviderModel, ...] = ()
        representative_effort = ReasoningEffort.DEFAULT
        chair_model: ProviderModel | None = None
        chair_effort = ReasoningEffort.DEFAULT
        research_model: ProviderModel | None = None
        research_effort: ReasoningEffort | None = None
        openalex_max_results_per_query: int | None = None
        openalex_quota_policy: Literal["wait", "tavily"] = "wait"
        research_max_concurrent_claim_groups: int | None = None
        maximum_parallelism = fast_report
        report_palette = DEFAULT_PALETTE
        decision_rigor = DecisionRigor.STRICT
        rendering_science_models: tuple[ProviderModel, ...] = ()
        rendering_citation_models: tuple[ProviderModel, ...] = ()
        rendering_language: str | None = None
        rendering_academic_skeleton: bool | None = None
        rendering_full_abstract: bool | None = None
        rendering_section_abstracts: bool | None = None
        rendering_segmentation: int | None = None
        rendering_liveliness: int | None = None
        rendering_output_formats: tuple[str, ...] = ()
        rendering_science_order: tuple[str, ...] = ()
        rendering_science_consultation_authority = "human"
        rendering_target_body_characters: int | None = None
        rendering_scope_description: str | None = None
        literature_language: str | None = None
        deliberation_language: str | None = None
        literature_full_abstract: bool | None = None
        literature_section_abstracts: bool | None = None
        literature_segmentation: int | None = None
        literature_liveliness: int | None = None
        literature_signposting: int | None = None
        literature_target_body_characters: int | None = None
        writer_model: ProviderModel | None = None
        writer_reasoning_effort: ReasoningEffort | None = None
        fast_planner_models: tuple[ProviderModel, ...] = ()
        technician_model: ProviderModel | None = None
        technician_effort: ReasoningEffort | None = None
        if meeting_type == MeetingType.RESEARCH:
            research_model = self._parse_provider_model(
                self._choose_one("3/6 指定 Research Desk 模型", catalog_options)
            )
            research_effort = ReasoningEffort(
                self._choose_one(
                    "4/6 设置 Research Desk 的 reasoning effort",
                    self._reasoning_options(config, (research_model,)),
                )
            )
            research_enabled = True
            task_prompt = "\n5/6 输入待核实的具体命题: "
            email_prompt = "\n6/6 输入人工介入通知邮箱（可留空）: "
        elif scholarly_rendering:
            while True:
                science = self._choose_many(
                    "3/15 选择科学事实核校模型（至少 2 个）", catalog_options
                )
                if len(science) >= 2:
                    break
                self._print(styled("科学事实组至少需要 2 个模型。", YELLOW, enabled=self.color))
            rendering_science_models = tuple(self._parse_provider_model(x) for x in science)
            while True:
                citations = self._choose_many(
                    "4/15 选择引文核校模型（至少 2 个）", catalog_options
                )
                if len(citations) >= 2:
                    break
                self._print(styled("引文组至少需要 2 个模型。", YELLOW, enabled=self.color))
            rendering_citation_models = tuple(self._parse_provider_model(x) for x in citations)
            reviewer_models = tuple(dict.fromkeys((*rendering_science_models, *rendering_citation_models)))
            representative_effort = ReasoningEffort(
                self._choose_one(
                    "5/15 设置核校组 reasoning effort",
                    self._reasoning_options(config, reviewer_models),
                )
            )
            chair_model = self._parse_provider_model(
                self._choose_one("6/15 指定 Chair／主渲染模型", catalog_options)
            )
            chair_effort = ReasoningEffort(
                self._choose_one(
                    "7/15 设置 Chair reasoning effort",
                    self._reasoning_options(config, (chair_model,)),
                )
            )
            research_enabled = True
            research_model = self._parse_provider_model(
                self._choose_one("8/15 指定 Research Desk 二次核验模型", catalog_options)
            )
            research_effort = ReasoningEffort(
                self._choose_one(
                    "9/15 设置 Research Desk reasoning effort",
                    self._reasoning_options(config, (research_model,)),
                )
            )
            if parent_meeting_path is None:
                while True:
                    candidate = self.input("\n输入需要学术化重绘的已完成会议目录: ").strip()
                    source = Path(candidate).expanduser()
                    if any((source / relative).is_file() for relative in (
                        "public/final/literature_review_report.md",
                        "public/final/scholarly_rendering/scholarly_review.md",
                    )):
                        parent_meeting_path = str(source.resolve())
                        inheritance_mode = InheritanceMode.BOTH
                        break
                    self._print(styled("源会议必须包含已完成的文献综述或学术重绘 Markdown。", RED, enabled=self.color))
            if self._choose_one(
                "重绘范围",
                [("full", "重绘全文"), ("partial", "局部重绘（也可再次重绘已有重绘稿）")],
            ) == "partial":
                while not rendering_scope_description:
                    rendering_scope_description = self.input(
                        "请用自然语言说明要重绘的范围；主席分割后须经你确认: "
                    ).strip() or None
            rendering_language = self._choose_one(
                "10/15 选择唯一输出语言",
                [("zh", "中文"), ("en", "English"), ("fr", "français")],
            )
            rendering_academic_skeleton = self._choose_one(
                "11/15 是否采用严格学术文章骨架",
                [("yes", "启用"), ("no", "关闭")],
            ) == "yes"
            summaries = self._choose_many(
                "12/15 选择摘要层级",
                [("full", "全文摘要"), ("section", "逐章节摘要")],
                blank_means_none=True,
            )
            rendering_full_abstract = "full" in summaries
            rendering_section_abstracts = "section" in summaries
            from project_ensemble.orchestration.readability_policy import (
                LIVELINESS_ANCHORS, SEGMENTATION_ANCHORS,
            )
            rendering_segmentation = int(
                self._choose_one(
                    "13a/15 分段积极性（1 最保守，5 最积极）",
                    [(str(i), f"{i}. {SEGMENTATION_ANCHORS[i]}") for i in range(1, 6)],
                )
            )
            rendering_liveliness = int(
                self._choose_one(
                    "13b/15 行文活泼性（1 最克制，5 最活泼）",
                    [(str(i), f"{i}. {LIVELINESS_ANCHORS[i]}") for i in range(1, 6)],
                )
            )
            if rendering_scope_description is None:
                while True:
                    raw_length = self.input(
                        "目标正文长度（字符数；不含参考文献和独立附录，允许上下浮动 20%）: "
                    ).strip().replace(",", "")
                    if raw_length.isdecimal() and int(raw_length) >= 1000:
                        rendering_target_body_characters = int(raw_length)
                        break
                    self._print(styled("请输入至少 1000 的正整数。", YELLOW, enabled=self.color))
            else:
                self._print("局部重绘不设置整篇长度目标；未选中原文必须保持不变。")
            rendering_output_formats = tuple(
                self._choose_many(
                    "14/15 选择输出格式（至少一个）",
                    [("html", "交互式 HTML（默认富文本阅读版）"), ("md", "Markdown（可编辑底稿）"), ("latex", "LaTeX"), ("pdf", "PDF")],
                    default_values=["html"],
                )
            )
            rendering_science_consultation_authority = self._choose_one(
                "科学事实异议的默认裁决方式",
                [
                    ("human", "由人类逐条裁决（默认）"),
                    ("chair", "授权主席逐条裁决；可在会议中随时撤销"),
                ],
            )
            self._section("科学事实组顺序（两轮均沿用）")
            for index, value in enumerate(science, start=1):
                self._print(f"  {index}. {value}")
            while True:
                raw_order = self.input("按顺序输入全部编号（逗号分隔）: ").strip()
                try:
                    order = [int(value.strip()) for value in raw_order.split(",")]
                except ValueError:
                    order = []
                if sorted(order) == list(range(1, len(science) + 1)):
                    rendering_science_order = tuple(science[index - 1] for index in order)
                    break
                self._print(styled("必须把每个科学核校模型恰好排列一次。", YELLOW, enabled=self.color))
            task_prompt = "\n15/15 输入本次学术化重绘的任务说明: "
            email_prompt = "\n输入人工介入通知邮箱（可留空）: "
        elif fast_report:
            self._section("简易文献调研 · 科学复核组")
            self._print("  智库长是科学知识核实与检验模型：检查主笔稿中的科学判断和证据边界，不负责主笔写作或代替人类裁定。")
            self._print("  至少选择两个不同的基础模型，以便独立指出问题；下文简称“智库长”。")
            while True:
                selected = self._choose_many(
                    "3/10 选择科学复核模型（至少两个不同基础模型）", catalog_options
                )
                if len(selected) >= 2:
                    break
                self._print(styled("快速会议至少需要两个不同模型组成智库长复核组。", YELLOW, enabled=self.color))
            selected_models = tuple(self._parse_provider_model(x) for x in selected)
            representative_effort = ReasoningEffort(self._choose_one(
                "4/10 科学复核组（智库长）推理强度", self._reasoning_options(config, selected_models)
            ))
            writer_model = self._parse_provider_model(self._choose_one(
                "5/10 指定贯穿全文的学术主笔", catalog_options
            ))
            writer_reasoning_effort = ReasoningEffort(self._choose_one(
                "6/10 学术主笔推理强度", self._reasoning_options(config, (writer_model,))
            ))
            self._print("  接下来选择 2–3 个只负责提出模块拆分方案的模型。每个模型独立做一轮探索性检索；主笔只看匿名方案，独立拟定任务书。此阶段沿用刚设定的智库长推理强度。")
            while True:
                chosen = self._choose_many(
                    "选择模块拆分提议模型（2–3 个；不参与此后的科学审阅身份）",
                    catalog_options,
                )
                if 2 <= len(chosen) <= 3:
                    fast_planner_models = tuple(self._parse_provider_model(value) for value in chosen)
                    break
                self._print(styled("请选择两个或三个不同模型。", YELLOW, enabled=self.color))
            research_model = self._parse_provider_model(self._choose_one(
                "7/10 指定 Research Desk 模型", catalog_options
            ))
            research_effort = ReasoningEffort(self._choose_one(
                "8/10 Research Desk 推理强度", self._reasoning_options(config, (research_model,))
            ))
            research_enabled = True
            self._print("  简易流程的正式会议不任命主席。你可以直接提交完整研究委托，也可以先与会前筹备主席讨论；独立模型先提拆分方案，之后学术主笔会拟定模块任务书供你批准。")
            task_prompt = "\n请输入完整研究委托（正式研究开始前仍会审阅模块任务书）: "
            email_prompt = "\n输入人工介入通知邮箱（可留空）: "
        else:
            selected = self._choose_many("3/10 选择本次会议的基础模型", catalog_options)
            selected_models = tuple(self._parse_provider_model(x) for x in selected)
            representative_effort = ReasoningEffort(
                self._choose_one(
                    "4/10 设置全体代表的 reasoning effort",
                    self._reasoning_options(config, selected_models),
                )
            )
            chair = self._choose_one("5/10 指定 Chair 模型", catalog_options)
            chair_model = self._parse_provider_model(chair)
            chair_effort = ReasoningEffort(
                self._choose_one(
                    "6/10 设置 Chair 的 reasoning effort",
                    self._reasoning_options(config, (chair_model,)),
                )
            )
            research_enabled = literature_report or self._choose_one(
                "7/10 是否启用 Research Desk 联网命题核实与证据收集",
                [
                    ("no", "关闭；会议行为与旧版本一致"),
                    ("yes", "启用共享、无表决权、可审计的外部证据服务"),
                ],
            ) == "yes"
            if research_enabled:
                research_model = self._parse_provider_model(
                    self._choose_one("8a/10 指定 Research Desk 模型", catalog_options)
                )
                research_effort = ReasoningEffort(
                    self._choose_one(
                        "8b/10 设置 Research Desk 的 reasoning effort",
                        self._reasoning_options(config, (research_model,)),
                    )
                )
            else:
                self._section("8/10 Research Desk 运行参数")
                self._print("  已跳过：Research Desk 联网证据服务未启用。")
            if literature_report:
                writer_model = self._parse_provider_model(
                    self._choose_one("指定贯穿全文的学术主笔模型", catalog_options)
                )
                writer_reasoning_effort = ReasoningEffort(self._choose_one(
                    "设置学术主笔 reasoning effort",
                    self._reasoning_options(config, (writer_model,)),
                ))
            task_prompt = (
                "\n9/10 输入文献调研报告的任务描述: "
                if literature_report
                else "\n9/10 输入任务描述: "
            )
            email_prompt = "\n10/10 输入人工介入通知邮箱（可留空）: "

        if research_enabled:
            self._section("OpenAlex 每类检索返回的文献候选数")
            self._print("  每条事实主张会分别检索支持、反证、适用范围和替代解释。")
            self._print("  建议值 12；允许 1–50。直接回车采用建议值。")
            while True:
                raw_limit = self.input("每类查询最多返回多少条 [12]: ").strip()
                if not raw_limit:
                    openalex_max_results_per_query = 12
                    break
                if raw_limit.isdecimal() and 1 <= int(raw_limit) <= 50:
                    openalex_max_results_per_query = int(raw_limit)
                    break
                self._print(styled("请输入 1–50 的整数，或直接回车采用 12。", YELLOW, enabled=self.color))

            self._section(ui_label("OpenAlex 返回 HTTP 429 时", "When OpenAlex returns HTTP 429", self.language))
            self._print(ui_label("   1. 当日额度耗尽时询问是否使用备用搜索；未知 429 短时退避重试（默认）", "   1. Ask whether to use backup search on daily exhaustion; retry unknown 429s (default)", self.language))
            if config.research.tavily.enabled:
                self._print(ui_label("   2. 预先授权限流时用 Tavily 补读；稍后尝试 OpenAlex 补检", "   2. Pre-authorize Tavily reading during limits, then retry OpenAlex", self.language))
                while True:
                    answer = self.input(ui_label("选择 1–2；回车默认等待人类裁定: ", "Choose 1–2; Enter asks for a Human decision: ", self.language)).strip()
                    if answer in {"", "1", "2"}:
                        openalex_quota_policy = "tavily" if answer == "2" else "wait"
                        break
                    self._print(ui_label("请输入 1 或 2，或直接回车采用默认值。", "Enter 1 or 2, or press Enter for the default.", self.language))
                self._print(ui_label(
                    "  OpenAlex 连接故障经重试仍未恢复时，Tavily 自动接手；网页与法规等非学术检索仍由 Tavily 正常执行。",
                    "  If OpenAlex remains unreachable after retries, Tavily takes over; Tavily still handles web and regulatory searches.",
                    self.language,
                ))
            else:
                self._print(ui_label("  Tavily 未启用，因此只能等待 OpenAlex 重置。", "  Tavily is disabled, so the meeting must wait for OpenAlex to reset.", self.language))

        if research_enabled and meeting_type == MeetingType.DELIBERATION:
            default_parallel = config.research.max_concurrent_claim_groups
            self._section("Research Desk 独立任务最大并行度")
            self._print("  仅用于独立 claim group 与现有证据覆盖检查；顺序追问仍然串行。")
            self._print("  并行度过高可能触发限流，也可能降低 prompt cache 命中率。")
            while True:
                raw_parallel = self.input(
                    f"最多同时处理多少个独立任务 [{default_parallel}]: "
                ).strip()
                if not raw_parallel:
                    research_max_concurrent_claim_groups = default_parallel
                    break
                if raw_parallel.isdecimal() and int(raw_parallel) >= 1:
                    research_max_concurrent_claim_groups = int(raw_parallel)
                    break
                self._print(styled("请输入正整数，或直接回车采用默认值。", YELLOW, enabled=self.color))

        if meeting_type == MeetingType.DELIBERATION and not fast_report:
            decision_rigor = DecisionRigor(
                self._choose_one(
                    "决策严谨度 · 重要决策需要多少赞成票",
                    [
                        ("strict", "严格：部分重要决策须获全体合格投票者至少四分之三赞成；其他规则不变"),
                        ("relaxed", "宽松：这些重要决策只须获全体合格投票者过半赞成；其他规则不变"),
                    ],
                )
            )
        if meeting_type != MeetingType.RESEARCH and not fast_report:
            maximum_parallelism = self._choose_one(
                "模型独立任务并行度（默认每个基础模型最多 4 路）",
                [
                    ("yes", "开启；每个代表模型最多同时调用 4 次，速度更快但缓存未命中可能增加"),
                    ("no", "关闭；使用当前模型配置或保守并发上限"),
                ],
            ) == "yes"

        if derived_report and parent_meeting_path is None:
            parent_meeting_path = self._choose_source_meeting(config)
            inheritance_mode = InheritanceMode.BOTH
        source_topic_context = self._source_meeting_topic_context(parent_meeting_path) if derived_report else None

        task: str | None = None
        prompt_development: dict | None = None
        title_model = writer_model if fast_report else chair_model
        title_effort = writer_reasoning_effort if fast_report else chair_effort
        title_role = ("academic writer" if self.language == "en" else "学术主笔") if fast_report else "Chair"
        if meeting_type == MeetingType.RESEARCH and prompt_for_claim_dialogue:
            route = self._choose_one(
                "如何确定本次要核实的命题",
                [
                    ("direct", "直接输入已想好的最终命题"),
                    ("dialogue", "与筹备主席对话，逐步整理成最终命题"),
                ],
            )
            if route == "dialogue":
                assert research_model is not None and research_effort is not None
                task, prompt_development = self._cached_setup_effect(
                    "research_claim_dialogue",
                    lambda: self._develop_research_claim(
                        config, catalog_options, research_model, research_effort,
                    ),
                )
        elif fast_report and prompt_for_literature_dialogue:
            assert writer_model is not None and writer_reasoning_effort is not None
            if literature_prompt_route == "dialogue":
                advisor = self._choose_one(
                    "选择会前筹备主席模型（不加入正式简易会议）",
                    [
                        ("writer", "使用已选的学术主笔模型"),
                        ("other", "从可用模型中另选一个"),
                    ],
                )
                if advisor == "writer":
                    title_model = writer_model
                    title_effort = writer_reasoning_effort
                else:
                    title_model = self._parse_provider_model(self._choose_one(
                        "指定会前筹备主席模型", catalog_options,
                    ))
                    title_effort = ReasoningEffort(self._choose_one(
                        "设置会前筹备主席的推理强度",
                        self._reasoning_options(config, (title_model,)),
                    ))
                title_role = "preparatory Chair" if self.language == "en" else "筹备主席"
                task, prompt_development = self._cached_setup_effect(
                    "fast_literature_dialogue",
                    lambda: self._develop_literature_task(
                        config, title_model, title_effort, preparatory=True,
                        source_context=source_topic_context,
                    ),
                )
        elif literature_report and prompt_for_literature_dialogue:
            if literature_prompt_route == "dialogue":
                assert chair_model is not None
                task, prompt_development = self._cached_setup_effect(
                    "literature_dialogue",
                    lambda: self._develop_literature_task(
                        config, chair_model, chair_effort,
                        source_context=source_topic_context,
                    ),
                )
        elif (meeting_type == MeetingType.DELIBERATION and not literature_report
              and prompt_for_deliberation_dialogue):
            route = self._choose_one(
                "如何确定本次议事会议的任务委托",
                [
                    ("direct", "直接输入已拟好的完整任务委托"),
                    ("dialogue", "与本次会议主席对话，逐步形成完整委托"),
                ],
            )
            if route == "dialogue":
                assert chair_model is not None
                task, prompt_development = self._cached_setup_effect(
                    "deliberation_dialogue",
                    lambda: self._develop_literature_task(
                        config, chair_model, chair_effort, deliberation=True,
                    ),
                )
        while not task:
            task = unicodedata.normalize("NFC", self.input(task_prompt)).strip()
            if not task:
                self._print(styled("任务描述不能为空。", RED, enabled=self.color))
        meeting_title = self._cached_setup_effect(
            "meeting_title",
            lambda: self._chair_title(
                config, title_model, title_effort, task, role_label=title_role,
            ),
        ) or natural_meeting_title(task)
        if prompt_for_title:
            while True:
                supplied_title = unicodedata.normalize(
                    "NFC", self.input(f"会议标题（最多 72 字符）[默认：{meeting_title}]: ")
                ).strip()
                if len(supplied_title) <= 72:
                    meeting_title = supplied_title or meeting_title
                    break
                self._print(styled("标题过长，请缩短到 72 字符以内。", YELLOW, enabled=self.color))

        human_reference_paths: tuple[str, ...] = ()
        if prompt_for_references and research_enabled:
            self._section("可选：提供人类参考资料")
            self._print("  每行输入一份本地文件路径；直接回车结束。资料会复制进会议，供 Research Desk 核验。")
            self._print("  支持 PDF、TXT、Markdown、HTML、CSV；上传后可供本会参与者查阅，请勿加入不宜公开的文件。")
            self._print("  上传不等于事实已获证实；扫描版 PDF 如无法提取文字，只保存原件。")
            collected: list[str] = []
            while len(collected) < MAX_REFERENCE_COUNT:
                raw = self.input(f"参考资料 {len(collected) + 1}/{MAX_REFERENCE_COUNT}（回车结束）: ").strip()
                if not raw:
                    break
                try:
                    validated = validate_reference_paths([raw])
                except ValueError as exc:
                    self._print(styled(str(exc), YELLOW, enabled=self.color))
                    continue
                path = str(validated[0])
                if path not in collected:
                    collected.append(path)
            human_reference_paths = tuple(collected)

        if literature_report:
            self._section("文献报告的读者写作设置")
            default_report_language = report_language_default()
            literature_language = self._choose_one(
                "报告唯一输出语言（设置中的默认值排在最前；本次仍需确认）",
                sorted(
                    [("zh", "中文"), ("en", "English"), ("fr", "français")],
                    key=lambda item: item[0] != default_report_language,
                ),
            )
            summaries = self._choose_many(
                "选择摘要层级（可同时选择；回车表示都不启用）",
                [("full", "全文摘要"), ("section", "逐章节摘要")],
                blank_means_none=True,
            )
            literature_full_abstract = "full" in summaries
            literature_section_abstracts = "section" in summaries
            from project_ensemble.orchestration.readability_policy import (
                LIVELINESS_ANCHORS, SEGMENTATION_ANCHORS, SIGNPOSTING_ANCHORS,
            )
            literature_segmentation = int(self._choose_one(
                "分段积极性", [(str(i), f"{i}. {SEGMENTATION_ANCHORS[i]}") for i in range(1, 6)]
            ))
            literature_liveliness = int(self._choose_one(
                "行文活泼性", [(str(i), f"{i}. {LIVELINESS_ANCHORS[i]}") for i in range(1, 6)]
            ))
            literature_signposting = int(self._choose_one(
                "论证路标：正文常规位置按所选级别，复杂处自动提高一级",
                [(str(i), f"{i}. {SIGNPOSTING_ANCHORS[i]}") for i in range(1, 6)],
            ))
            while True:
                raw_length = self.input(
                    "整篇报告目标正文长度（非空白字符数；不含参考文献和独立附录，允许上下浮动 20%）: "
                ).strip().replace(",", "")
                if raw_length.isdecimal() and int(raw_length) >= 1000:
                    literature_target_body_characters = int(raw_length)
                    break
                self._print(styled("请输入至少 1000 的正整数。", YELLOW, enabled=self.color))
        if literature_report or meeting_type == MeetingType.SCHOLARLY_RENDERING:
            report_palette = self._choose_one(
                "最终报告配色方案（仅改变 HTML/PDF 排版，不改变正文或证据）",
                palette_options(self.language),
            )

        if (meeting_type == MeetingType.DELIBERATION and not literature_report
                and prompt_for_deliberation_dialogue):
            deliberation_language = self._choose_one(
                "议事文书成文语言（与界面语言独立）",
                [
                    ("task", "按本次任务委托指定的语言写作（默认）"),
                    ("zh", "中文"), ("en", "English"), ("fr", "français"),
                ],
            )

        while True:
            raw_email = self.input(email_prompt).strip()
            if not raw_email:
                email = None
                break
            try:
                email = validate_email_address(raw_email)
                break
            except ValueError as exc:
                self._print(styled(f"邮箱无效：{exc}", RED, enabled=self.color))

        if self._choose_one(
            "Technician · 会议自修复（可选）",
            [
                ("no", "关闭；技术故障沿用暂停与人工处理"),
                ("yes", "启用；故障片段可能发送给所选模型供应商；建议选最擅长 coding 的模型"),
            ],
        ) == "yes":
            self._print(
                "Risk: Technician may receive the minimum task, document, or evidence excerpts needed for recovery. "
                "These are sent to the selected model provider. Credentials are never sent; Technician cannot edit ENSEMBLE code or frozen decisions."
                if self.language == "en" else
                "风险：Technician 会接收排障所需的最小会议任务、文书或证据片段；"
                "这些内容将发送给所选模型供应商。不会发送密钥，不能修改 ENSEMBLE 程序或冻结决议。"
            )
            technician_model = self._parse_provider_model(self._choose_one(
                "指定负责会议自修复的 Technician 模型", catalog_options,
            ))
            technician_effort = ReasoningEffort(self._choose_one(
                "设置 Technician 推理强度",
                self._reasoning_options(config, (technician_model,)),
            ))

        selection = StartupSelection(
            meeting_type=meeting_type,
            providers=tuple(providers),
            models=selected_models,
            chair_model=chair_model,
            task_description=task,
            escalation_email=email,
            representative_reasoning_effort=representative_effort,
            chair_reasoning_effort=chair_effort,
            research_enabled=research_enabled,
            research_model=research_model,
            research_reasoning_effort=research_effort,
            openalex_max_results_per_query=openalex_max_results_per_query,
            openalex_quota_policy=openalex_quota_policy,
            research_max_concurrent_claim_groups=research_max_concurrent_claim_groups,
            maximum_parallelism=maximum_parallelism,
            decision_rigor=decision_rigor,
            deliverable_type=deliverable_type,
            meeting_title=meeting_title,
            report_palette=report_palette,
            parent_meeting_path=parent_meeting_path,
            inheritance_mode=inheritance_mode,
            rendering_provisional_source=provisional_rendering_source,
            rendering_science_models=rendering_science_models,
            rendering_citation_models=rendering_citation_models,
            rendering_language=rendering_language,
            rendering_academic_skeleton=rendering_academic_skeleton,
            rendering_full_abstract=rendering_full_abstract,
            rendering_section_abstracts=rendering_section_abstracts,
            rendering_segmentation=rendering_segmentation,
            rendering_liveliness=rendering_liveliness,
            rendering_output_formats=rendering_output_formats,
            rendering_science_order=rendering_science_order,
            rendering_science_consultation_authority=rendering_science_consultation_authority,
            rendering_target_body_characters=rendering_target_body_characters,
            rendering_scope_description=rendering_scope_description,
            literature_language=literature_language,
            deliberation_language=deliberation_language,
            literature_full_abstract=literature_full_abstract,
            literature_section_abstracts=literature_section_abstracts,
            literature_segmentation=literature_segmentation,
            literature_liveliness=literature_liveliness,
            literature_signposting=literature_signposting,
            literature_target_body_characters=literature_target_body_characters,
            writer_model=writer_model,
            writer_reasoning_effort=writer_reasoning_effort,
            fast_planner_models=fast_planner_models,
            technician_model=technician_model,
            technician_reasoning_effort=technician_effort,
            literature_writing_policy=("fast" if fast_report else "v071" if literature_report else None),
            human_reference_paths=human_reference_paths,
            prompt_development=prompt_development,
        )
        self._section("配置摘要")
        def summary(chinese: str, english: str) -> None:
            self._print(ui_label(chinese, english, self.language))
        self._print(
            f"  Interface language (user setting): {self.language}"
            if self.language == "en" else f"  界面语言（用户级设置）: {self.language}"
        )
        if selection.deliberation_language is not None:
            self._print(
                f"  Deliberation document language (meeting setting): {selection.deliberation_language}"
                if self.language == "en" else
                f"  议事文书成文语言（本次会议设置）: {selection.deliberation_language}"
            )
        summary(f"  会议标题: {selection.meeting_title}", f"  Meeting title: {selection.meeting_title}")
        meeting_type_label = (
            ("接续文献调研" if selection.parent_meeting_path else "文献调研")
            + (" · 简易流程" if fast_report else " · 完整流程")
            if literature_report else selection.meeting_type.value
        )
        meeting_type_label_en = (
            ("Continued literature review" if selection.parent_meeting_path else "Literature review")
            + (" · streamlined workflow" if fast_report else " · full workflow")
            if literature_report else selection.meeting_type.value
        )
        summary(f"  会议类型: {meeting_type_label}", f"  Meeting type: {meeting_type_label_en}")
        summary(f"  交付物类型: {selection.deliverable_type.value}", f"  Deliverable type: {selection.deliverable_type.value}")
        if selection.parent_meeting_path:
            summary(f"  接续源会议: {selection.parent_meeting_path}", f"  Source meeting: {selection.parent_meeting_path}")
            summary(f"  继承内容: {selection.inheritance_mode.value}", f"  Inherited material: {selection.inheritance_mode.value}")
            if selection.rendering_provisional_source:
                summary("  来源状态: 未完成最终可读性认证；只转接冻结草稿，原会议仍保持未完成", "  Source status: final readability not certified; only the frozen draft is transferred and the source meeting remains unfinished")
        if selection.human_reference_paths:
            summary(f"  人类提供的候选参考资料: {len(selection.human_reference_paths)} 份；尚未经事实核验", f"  Human-provided candidate references: {len(selection.human_reference_paths)}; not yet fact-checked")
        if scholarly_rendering:
            delegated = selection.rendering_science_consultation_authority == "chair"
            summary("  科学异议裁决: " + ("授权主席" if delegated else "人类"),
                    "  Science-objection decisions: " + ("authorized Chair" if delegated else "Human"))
            summary(f"  输出语言: {selection.rendering_language}", f"  Output language: {selection.rendering_language}")
            summary(f"  核校模型: 科学事实组 {len(selection.rendering_science_models)} 个；引文组 {len(selection.rendering_citation_models)} 个",
                    f"  Review models: {len(selection.rendering_science_models)} science; {len(selection.rendering_citation_models)} citation")
            summary(f"  输出格式: {', '.join(selection.rendering_output_formats)}", f"  Output formats: {', '.join(selection.rendering_output_formats)}")
            if selection.rendering_target_body_characters is not None:
                target = selection.rendering_target_body_characters
                summary(
                    f"  正文目标长度（输入控制参数）: {target:,} 字符；"
                    f"允许范围 {target * 4 // 5:,}–{target * 6 // 5:,} 字符，"
                    "参考文献与独立附录不计",
                    f"  Target body length (input control): {target:,} characters; allowed range {target * 4 // 5:,}–{target * 6 // 5:,}; references and separate appendices excluded",
                )
        if literature_report and selection.literature_target_body_characters is not None:
            target = selection.literature_target_body_characters
            summary(
                f"  报告正文目标长度（输入控制参数）: {target:,} 个非空白字符；"
                f"参考范围 {target * 4 // 5:,}–{target * 6 // 5:,}；"
                "参考文献与独立附录不计",
                f"  Target report body length (input control): {target:,} non-whitespace characters; reference range {target * 4 // 5:,}–{target * 6 // 5:,}; references and separate appendices excluded",
            )
        summary(f"  供应商: {', '.join(selection.providers)}", f"  Providers: {', '.join(selection.providers)}")
        if selection.meeting_type != MeetingType.RESEARCH:
            if selection.meeting_type == MeetingType.SCHOLARLY_RENDERING:
                reviewer_union = set(selection.rendering_science_models) | set(
                    selection.rendering_citation_models
                )
                summary(f"  核校基础模型并集: {len(reviewer_union)} 个", f"  Distinct reviewer base models: {len(reviewer_union)}")
            else:
                summary(f"  基础模型数: {len(selection.models)}", f"  Base models: {len(selection.models)}")
            if selection.chair_model is not None:
                self._print(f"  Chair: {selection.chair_model[0]}:{selection.chair_model[1]}")
            elif fast_report:
                summary("  Chair: 无；由单一学术主笔统筹，智库长独立科学复核", "  Chair: none; one academic writer coordinates drafting and independent science reviewers check it")
            summary(f"  {'智库长' if fast_report else '代表'} reasoning effort: {selection.representative_reasoning_effort.value}",
                    f"  {'Science reviewer' if fast_report else 'Representative'} reasoning effort: {selection.representative_reasoning_effort.value}")
            if selection.chair_model is not None:
                self._print(f"  Chair reasoning effort: {selection.chair_reasoning_effort.value}")
            if fast_report and selection.writer_model is not None:
                summary(f"  学术主笔: {selection.writer_model[0]}:{selection.writer_model[1]}", f"  Academic writer: {selection.writer_model[0]}:{selection.writer_model[1]}")
                summary(f"  模块拆分提议模型: {len(selection.fast_planner_models)} 个；并行提出匿名方案后由主笔定稿",
                        f"  Module-split proposers: {len(selection.fast_planner_models)}; anonymous parallel proposals before Writer synthesis")
            if not fast_report:
                summary("  独立代表提交最大并行: " + ("开启 · 每模型最多 4 次在途调用" if selection.maximum_parallelism else "关闭"),
                        "  Maximum independent submission parallelism: " + ("on · up to 4 in-flight calls per model" if selection.maximum_parallelism else "off"))
            if selection.meeting_type == MeetingType.DELIBERATION and not fast_report:
                relaxed = selection.decision_rigor == DecisionRigor.RELAXED
                summary("  决策严谨度: " + ("宽松 · 原 3/4 高门槛改为过半" if relaxed else "严格 · 沿用原门槛"),
                        "  Decision rigor: " + ("relaxed · former 3/4 thresholds become majority" if relaxed else "strict · original thresholds"))
        if selection.research_enabled and selection.research_model is not None:
            summary(f"  Research Desk: 已启用 · {selection.research_model[0]}:{selection.research_model[1]} · reasoning effort {selection.research_reasoning_effort.value}",
                    f"  Research Desk: enabled · {selection.research_model[0]}:{selection.research_model[1]} · reasoning effort {selection.research_reasoning_effort.value}")
        if selection.technician_model is not None:
            summary(f"  Technician: 已启用 · {selection.technician_model[0]}:{selection.technician_model[1]} · 故障片段会发送给所选供应商；无权修改 ENSEMBLE 本体",
                    f"  Technician: enabled · {selection.technician_model[0]}:{selection.technician_model[1]} · failure excerpts may reach this provider; cannot edit ENSEMBLE code")
            summary(f"  OpenAlex 每类查询候选数上限: {selection.openalex_max_results_per_query} 条", f"  OpenAlex candidate limit per query type: {selection.openalex_max_results_per_query}")
            summary(
                f"  OpenAlex HTTP 429 策略: {'确认当日耗尽时请求人类裁定；未知限流重试' if selection.openalex_quota_policy == 'wait' else '已预授权 Tavily 补读，稍后尝试 OpenAlex 补检'}",
                f"  OpenAlex HTTP 429 policy: {'ask Human on confirmed daily exhaustion; retry unknown limits' if selection.openalex_quota_policy == 'wait' else 'Tavily reading pre-authorized, then OpenAlex recheck'}",
            )
            if selection.research_max_concurrent_claim_groups is not None:
                summary(f"  Research Desk 独立任务最大并行度: {selection.research_max_concurrent_claim_groups} 组", f"  Research Desk maximum concurrent independent tasks: {selection.research_max_concurrent_claim_groups}")
        else:
            summary("  Research Desk: 未启用", "  Research Desk: disabled")
        summary(f"  通知邮箱: {selection.escalation_email or '未配置'}", f"  Notification email: {selection.escalation_email or 'not configured'}")
        self._print(styled("└" + "─" * (self.width - 1), CYAN, enabled=self.color))
        confirmed = self.input("创建会议工作区？[y/N]: ").strip().lower()
        if confirmed not in {"y", "yes"}:
            raise KeyboardInterrupt("meeting initialization cancelled")
        return selection

    @staticmethod
    def _parse_provider_model(value: str) -> ProviderModel:
        provider, model = value.split(":", 1)
        return provider, model

    @staticmethod
    def _provider_description(provider_id: str, config: EnsembleConfig) -> str:
        cfg = config.providers[provider_id]
        name = cfg.display_name or {
            "deepseek": "DeepSeek", "kimi": "Kimi", "glm": "GLM", "gemini": "Gemini",
            "codex": "Codex（ChatGPT 订阅）",
        }.get(provider_id, provider_id)
        if cfg.kind == "codex_subscription":
            return f"{name} · 通过本机 Codex 登录核验；不使用 OpenAI API key"
        if cfg.kind == "claude_code" and not cfg.api_key_env:
            return f"{name} · 使用本机 Claude Code 登录；模型由用户配置"
        credential = (
            "凭据已检测"
            if cfg.api_key()
            else f"缺少 {cfg.api_key_env}（或 api_key_file）"
        )
        return f"{name} · {credential}"

    @staticmethod
    def _model_description(model: ModelDescriptor) -> str:
        details: list[str] = []
        if model.input_token_limit is not None:
            details.append(f"输入上限 {model.input_token_limit} tokens")
        if model.output_token_limit is not None:
            details.append(f"输出上限 {model.output_token_limit} tokens")
        if model.supported_methods:
            details.append(f"接口 {', '.join(model.supported_methods)}")
        return "; ".join(details) or "供应商实时返回"

    @staticmethod
    def _reasoning_options(
        config: EnsembleConfig,
        models: tuple[ProviderModel, ...],
    ) -> list[tuple[str, str]]:
        options = [
            (ReasoningEffort.DEFAULT.value, "沿用供应商与模型默认值，不发送控制字段"),
            (ReasoningEffort.LOW.value, "低推理强度；通常优先降低延迟与成本"),
            (ReasoningEffort.MEDIUM.value, "中等推理强度；由供应商映射到合法值"),
            (ReasoningEffort.HIGH.value, "高推理强度；通常增加延迟与推理 token"),
        ]
        available = [
            option
            for option in options
            if option[0] == ReasoningEffort.DEFAULT.value
            or any(
                config.providers[provider_id].supports_reasoning_effort(model_id, option[0])
                for provider_id, model_id in models
            )
        ]
        if len(models) > 1:
            available = [
                (
                    value,
                    description
                    + (
                        "；不支持该档位的模型将自动简并到最接近的合法档位"
                        if value != ReasoningEffort.DEFAULT.value
                        and not all(
                            config.providers[provider_id].supports_reasoning_effort(model_id, value)
                            for provider_id, model_id in models
                        )
                        else ""
                    ),
                )
                for value, description in available
            ]
        return available


_REASONING_ORDER = (
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
)


def collapse_reasoning_effort(
    config: EnsembleConfig,
    model: ProviderModel,
    requested: ReasoningEffort,
) -> ReasoningEffort:
    """Map a shared requested level to the nearest legal level for one model."""
    if requested == ReasoningEffort.DEFAULT:
        return requested
    provider_id, model_id = model
    provider = config.providers[provider_id]
    supported = [
        effort
        for effort in _REASONING_ORDER
        if provider.supports_reasoning_effort(model_id, effort.value)
    ]
    if not supported:
        return ReasoningEffort.DEFAULT
    if requested in supported:
        return requested
    target = _REASONING_ORDER.index(requested)
    # Prefer the lower setting when two supported settings are equally close;
    # this avoids silently increasing latency/cost beyond the Human request.
    return min(supported, key=lambda effort: (abs(_REASONING_ORDER.index(effort) - target), _REASONING_ORDER.index(effort)))


def assert_models_were_discovered(selection: StartupSelection, catalog: list[ModelDescriptor]) -> None:
    available = {(model.provider_id, model.model_id) for model in catalog}
    requested = set(selection.models)
    if selection.chair_model is not None:
        requested.add(selection.chair_model)
    if selection.research_model is not None:
        requested.add(selection.research_model)
    if selection.writer_model is not None:
        requested.add(selection.writer_model)
    if selection.technician_model is not None:
        requested.add(selection.technician_model)
    requested.update(selection.fast_planner_models)
    requested.update(selection.rendering_science_models)
    requested.update(selection.rendering_citation_models)
    unavailable = sorted(requested - available)
    if unavailable:
        rendered = ", ".join(f"{provider}:{model}" for provider, model in unavailable)
        raise ValueError(f"models were not returned by live provider discovery: {rendered}")


def validate_noninteractive_selection(selection: StartupSelection, config: EnsembleConfig) -> None:
    if not selection.providers:
        raise ValueError("at least one --provider is required")
    if (
        selection.meeting_type not in {MeetingType.RESEARCH, MeetingType.SCHOLARLY_RENDERING}
        and not selection.models
    ):
        raise ValueError("at least one --model is required")
    if not selection.task_description.strip():
        raise ValueError("--task cannot be empty")
    if selection.human_reference_paths:
        if not selection.research_enabled:
            raise ValueError("human reference files require the Research Desk")
        validate_reference_paths(selection.human_reference_paths)
    if selection.escalation_email is not None:
        validate_email_address(selection.escalation_email)
    configured = {pid for pid, provider in config.providers.items() if provider.enabled}
    unknown = set(selection.providers) - configured
    if unknown:
        raise ValueError(f"unknown or disabled providers: {', '.join(sorted(unknown))}")
    selected_providers = set(selection.providers)
    model_providers = {
        provider
        for provider, _ in (
            *selection.models,
            *selection.rendering_science_models,
            *selection.rendering_citation_models,
            *selection.fast_planner_models,
        )
    }
    if not model_providers <= selected_providers or (
        selection.chair_model is not None
        and selection.chair_model[0] not in selected_providers
    ):
        raise ValueError("every model and the Chair must belong to a selected provider")
    if (selection.technician_model is None) != (selection.technician_reasoning_effort is None):
        raise ValueError("Technician model and reasoning effort must be configured together")
    if (selection.technician_model is not None
            and selection.technician_model[0] not in selected_providers):
        raise ValueError("Technician model must belong to a selected provider")
    if selection.literature_writing_policy in {"v071", "fast"}:
        if selection.deliverable_type != DeliverableType.LITERATURE_REVIEW:
            raise ValueError("v0.7.1 writing policy is limited to literature reports")
        if selection.writer_model is None or selection.writer_reasoning_effort is None:
            raise ValueError("v0.7.1 literature reports require an independently selected Writer")
        if selection.writer_model[0] not in selected_providers:
            raise ValueError("Writer model must belong to a selected provider")
    if selection.meeting_type != MeetingType.DELIBERATION and selection.decision_rigor != DecisionRigor.STRICT:
        raise ValueError("relaxed decision rigor is only available for deliberation meetings")
    if selection.rendering_science_consultation_authority not in {"human", "chair"}:
        raise ValueError("science consultation authority must be human or chair")
    if selection.rendering_target_body_characters is not None:
        if (selection.meeting_type != MeetingType.SCHOLARLY_RENDERING
                or selection.rendering_target_body_characters < 1000):
            raise ValueError("rendering body target requires scholarly rendering and at least 1000 characters")
    if selection.literature_target_body_characters is not None:
        if (selection.deliverable_type != DeliverableType.LITERATURE_REVIEW
                or selection.literature_target_body_characters < 1000):
            raise ValueError("literature body target requires a literature review and at least 1000 characters")
    if (selection.meeting_type != MeetingType.SCHOLARLY_RENDERING
            and selection.rendering_science_consultation_authority != "human"):
        raise ValueError("Chair science consultation delegation requires scholarly rendering")
    if selection.meeting_type == MeetingType.RESEARCH:
        if selection.models or selection.chair_model is not None:
            raise ValueError("research-only meetings cannot configure Representatives or a Chair")
        if not selection.research_enabled:
            raise ValueError("research-only meetings require the Research Desk")
        if selection.maximum_parallelism:
            raise ValueError("research-only meetings have no Representative calls to parallelize")
    elif selection.meeting_type == MeetingType.SCHOLARLY_RENDERING:
        if selection.chair_model is None:
            raise ValueError("scholarly rendering requires a Chair/renderer")
        if selection.deliverable_type != DeliverableType.SCHOLARLY_RENDERING:
            raise ValueError("scholarly rendering requires the scholarly_rendering deliverable")
        if len(selection.rendering_science_models) < 2:
            raise ValueError("select at least two science-bookkeeping models")
        if len(selection.rendering_citation_models) < 2:
            raise ValueError("select at least two citation-bookkeeping models")
        if len(set(selection.rendering_science_models)) != len(
            selection.rendering_science_models
        ):
            raise ValueError("science-bookkeeping models must be unique")
        if len(set(selection.rendering_citation_models)) != len(
            selection.rendering_citation_models
        ):
            raise ValueError("citation-bookkeeping models must be unique")
        if selection.parent_meeting_path is None:
            raise ValueError("scholarly rendering requires a parent literature report")
        if selection.inheritance_mode != InheritanceMode.BOTH:
            raise ValueError("scholarly rendering must inherit both the report and evidence database")
        parent_root = Path(selection.parent_meeting_path).expanduser()
        source_report = parent_root / "public/final/literature_review_report.md"
        rendered_report = parent_root / "public/final/scholarly_rendering/scholarly_review.md"
        if selection.rendering_provisional_source:
            provisional_rendering_text(parent_root)
        elif not (source_report.is_file() or rendered_report.is_file()):
            raise ValueError("scholarly rendering requires a completed parent Markdown report")
        parent_manifest = json.loads(
            (parent_root / "public/meeting_manifest.json").read_text(encoding="utf-8")
        )
        parent_deliverable = parent_manifest.get("deliverable_type")
        if parent_deliverable not in {None, DeliverableType.LITERATURE_REVIEW.value,
                                      DeliverableType.SCHOLARLY_RENDERING.value}:
            raise ValueError("scholarly rendering parent must be a literature_review or scholarly_rendering meeting")
        if selection.rendering_language not in {"zh", "en", "fr"}:
            raise ValueError("rendering language must be zh, en, or fr")
        if not selection.rendering_output_formats:
            raise ValueError("select at least one rendering output format")
        if len(set(selection.rendering_output_formats)) != len(
            selection.rendering_output_formats
        ):
            raise ValueError("rendering output formats must be unique")
        expected_order = {
            f"{provider}:{model}" for provider, model in selection.rendering_science_models
        }
        if set(selection.rendering_science_order) != expected_order or len(
            selection.rendering_science_order
        ) != len(expected_order):
            raise ValueError("science-review order must list every science model exactly once")
    elif selection.chair_model is None and selection.literature_writing_policy != "fast":
        raise ValueError("deliberation and audit meetings require a Chair")
    if selection.literature_writing_policy == "fast":
        if selection.chair_model is not None:
            raise ValueError("fast literature meetings do not appoint a Chair")
        if len(selection.models) < 2 or len(set(selection.models)) != len(selection.models):
            raise ValueError("fast literature meetings need at least two distinct reviewer models")
        if all(model == selection.writer_model for model in selection.models):
            raise ValueError("at least one reviewer model must differ from the Writer")
        if not 2 <= len(selection.fast_planner_models) <= 3:
            raise ValueError("fast literature meetings need two or three split-proposal models")
        if len(set(selection.fast_planner_models)) != len(selection.fast_planner_models):
            raise ValueError("split-proposal models must be distinct")
    elif selection.fast_planner_models:
        raise ValueError("split-proposal models are available only in fast literature meetings")
    if selection.deliverable_type == DeliverableType.LITERATURE_REVIEW:
        if selection.meeting_type != MeetingType.DELIBERATION:
            raise ValueError("literature-review deliverables require deliberation procedure")
    if selection.deliberation_language not in {None, "task", "zh", "en", "fr"}:
        raise ValueError("deliberation document language must be task, zh, en, or fr")
    if (selection.deliberation_language is not None
            and selection.deliverable_type != DeliverableType.NORMATIVE_INSTRUMENT):
        raise ValueError("deliberation document language requires a normative deliverable")
    if selection.deliverable_type == DeliverableType.SCHOLARLY_RENDERING:
        if selection.meeting_type != MeetingType.SCHOLARLY_RENDERING:
            raise ValueError("scholarly rendering requires scholarly_rendering procedure")
    if selection.parent_meeting_path:
        parent_manifest = (
            Path(selection.parent_meeting_path).expanduser()
            / "public/meeting_manifest.json"
        )
        if not parent_manifest.is_file():
            raise ValueError("parent meeting directory has no public meeting manifest")
        if selection.inheritance_mode is None:
            raise ValueError("a parent meeting requires an inheritance mode")
    elif selection.inheritance_mode is not None:
        raise ValueError("inheritance mode requires a parent meeting")
    if selection.research_enabled:
        if selection.research_model is None or selection.research_reasoning_effort is None:
            raise ValueError("enabled Research Desk requires a model and reasoning effort")
        if selection.research_model[0] not in selected_providers:
            raise ValueError("the Research Desk model must belong to a selected provider")
        if selection.openalex_max_results_per_query is not None and not (
            1 <= selection.openalex_max_results_per_query <= 50
        ):
            raise ValueError("OpenAlex results per query must be between 1 and 50")
        if selection.openalex_quota_policy == "tavily" and not config.research.tavily.enabled:
            raise ValueError("Tavily quota fallback requires the Tavily research backend")
        if (selection.research_max_concurrent_claim_groups is not None
                and selection.research_max_concurrent_claim_groups < 1):
            raise ValueError("Research Desk independent-task parallelism must be positive")
        if (selection.research_max_concurrent_claim_groups is not None
                and selection.meeting_type != MeetingType.DELIBERATION):
            raise ValueError("Research Desk independent-task parallelism requires deliberation")
    elif selection.research_model is not None or selection.research_reasoning_effort is not None:
        raise ValueError("disabled Research Desk cannot have a model or reasoning effort")
    elif selection.openalex_max_results_per_query is not None:
        raise ValueError("disabled Research Desk cannot have an OpenAlex result limit")
    if not selection.research_enabled and selection.research_max_concurrent_claim_groups is not None:
        raise ValueError("disabled Research Desk cannot have an independent-task parallelism limit")
    reasoning_models = tuple(
        dict.fromkeys(
            (
                *selection.models,
                *selection.rendering_science_models,
                *selection.rendering_citation_models,
            )
        )
    )
    if reasoning_models:
        _validate_reasoning_effort(
            config,
            reasoning_models,
            selection.representative_reasoning_effort,
            role="representative",
        )
    if selection.chair_model is not None:
        _validate_reasoning_effort(
            config,
            (selection.chair_model,),
            selection.chair_reasoning_effort,
            role="Chair",
        )
    if selection.research_model is not None and selection.research_reasoning_effort is not None:
        _validate_reasoning_effort(
            config,
            (selection.research_model,),
            selection.research_reasoning_effort,
            role="Research Desk",
        )
    if selection.writer_model is not None and selection.writer_reasoning_effort is not None:
        _validate_reasoning_effort(
            config, (selection.writer_model,), selection.writer_reasoning_effort,
            role="学术主笔",
        )
    if selection.technician_model is not None and selection.technician_reasoning_effort is not None:
        _validate_reasoning_effort(
            config, (selection.technician_model,), selection.technician_reasoning_effort,
            role="Technician",
        )


def _validate_reasoning_effort(
    config: EnsembleConfig,
    models: tuple[ProviderModel, ...],
    effort: ReasoningEffort,
    *,
    role: str,
) -> None:
    if effort == ReasoningEffort.DEFAULT:
        return
    supported = [
        (provider_id, model_id)
        for provider_id, model_id in models
        if config.providers[provider_id].supports_reasoning_effort(model_id, effort.value)
    ]
    if not supported:
        raise ValueError(
            f"{role} reasoning effort {effort.value!r} is unsupported by every selected model; "
            "choose default or configure reasoning_effort_map"
        )


def start_meeting(
    config: EnsembleConfig,
    selection: StartupSelection,
    *,
    governance_docs: str | Path | None = None,
    output_directory: str | Path | None = None,
    model_catalog: list[ModelDescriptor] | None = None,
) -> MeetingRepository:
    validate_noninteractive_selection(selection, config)
    governance_docs = governance_docs or config.project.governance_docs
    output_directory = Path.cwd() if output_directory is None else Path(output_directory)
    reviewer_models = tuple(
        dict.fromkeys(
            (
                *selection.models,
                *selection.rendering_science_models,
                *selection.rendering_citation_models,
            )
        )
    )
    representative_effective = {
        model: collapse_reasoning_effort(
            config, model, selection.representative_reasoning_effort
        )
        for model in reviewer_models
    }
    chair_effective = (
        collapse_reasoning_effort(
            config, selection.chair_model, selection.chair_reasoning_effort
        )
        if selection.chair_model is not None
        else ReasoningEffort.DEFAULT
    )
    research_effective = (
        collapse_reasoning_effort(
            config, selection.research_model, selection.research_reasoning_effort
        )
        if selection.research_model is not None
        and selection.research_reasoning_effort is not None
        else None
    )
    selected_runtime_models = set(selection.models)
    selected_runtime_models.update(selection.rendering_science_models)
    selected_runtime_models.update(selection.rendering_citation_models)
    selected_runtime_models.update(selection.fast_planner_models)
    if selection.chair_model is not None:
        selected_runtime_models.add(selection.chair_model)
    if selection.research_model is not None:
        selected_runtime_models.add(selection.research_model)
    if selection.writer_model is not None:
        selected_runtime_models.add(selection.writer_model)
    if selection.technician_model is not None:
        selected_runtime_models.add(selection.technician_model)
    catalog_by_model = {
        (model.provider_id, model.model_id): model
        for model in (model_catalog or [])
    }
    concurrency_limits: dict[tuple[str, str], int] = {}
    concurrency_sources: dict[tuple[str, str], str] = {}
    for provider_id, model_id in selected_runtime_models:
        if selection.maximum_parallelism and (
            selection.deliverable_type == DeliverableType.LITERATURE_REVIEW
            or (provider_id, model_id) in reviewer_models
        ):
            concurrency_limits[(provider_id, model_id)] = 4
            concurrency_sources[(provider_id, model_id)] = "HUMAN_MAX_PARALLEL_4"
            continue
        configured = config.providers[provider_id].configured_concurrency_limit(model_id)
        reported = (
            catalog_by_model[(provider_id, model_id)].max_concurrent_requests
            if (provider_id, model_id) in catalog_by_model
            else None
        )
        if configured is not None:
            concurrency_limits[(provider_id, model_id)] = configured
            concurrency_sources[(provider_id, model_id)] = "CONFIGURED"
        elif reported is not None:
            concurrency_limits[(provider_id, model_id)] = reported
            concurrency_sources[(provider_id, model_id)] = "PROVIDER_CATALOG"
        else:
            concurrency_limits[(provider_id, model_id)] = 1
            concurrency_sources[(provider_id, model_id)] = "SAFE_FALLBACK"
    repo = MeetingRepository.create(
        output_directory,
        selected_models=list(reviewer_models),
        chair_model=selection.chair_model,
        governance_docs=governance_docs,
        meeting_type=selection.meeting_type,
        task_description=selection.task_description,
        escalation_email=selection.escalation_email,
        config_path=config.source_path,
        representative_reasoning_effort=selection.representative_reasoning_effort,
        representative_reasoning_effective=representative_effective,
        chair_reasoning_effort=chair_effective,
        research_enabled=selection.research_enabled,
        research_model=selection.research_model,
        research_reasoning_effort=research_effective,
        openalex_max_results_per_query=(
            selection.openalex_max_results_per_query
            if selection.research_enabled and selection.openalex_max_results_per_query is not None
            else (config.research.max_results_per_query if selection.research_enabled else None)
        ),
        openalex_quota_policy=(selection.openalex_quota_policy if selection.research_enabled else None),
        research_max_concurrent_claim_groups=(
            (selection.research_max_concurrent_claim_groups
             or config.research.max_concurrent_claim_groups)
            if selection.research_enabled and selection.meeting_type == MeetingType.DELIBERATION
            else None
        ),
        model_concurrency_limits=concurrency_limits,
        model_concurrency_sources=concurrency_sources,
        maximum_parallelism=selection.maximum_parallelism,
        decision_rigor=selection.decision_rigor,
        deliverable_type=selection.deliverable_type,
        meeting_title=selection.meeting_title or natural_meeting_title(selection.task_description),
        parent_meeting_id=(
            str(json.loads((Path(selection.parent_meeting_path).expanduser() / "public/meeting_manifest.json").read_text(encoding="utf-8"))["meeting_id"])
            if selection.parent_meeting_path is not None
            else None
        ),
        parent_meeting_path=selection.parent_meeting_path,
        inheritance_mode=selection.inheritance_mode,
        rendering_provisional_source=selection.rendering_provisional_source,
        rendering_science_models=list(selection.rendering_science_models),
        rendering_citation_models=list(selection.rendering_citation_models),
        rendering_language=selection.rendering_language,
        deliberation_language=selection.deliberation_language,
        rendering_academic_skeleton=selection.rendering_academic_skeleton,
        rendering_full_abstract=selection.rendering_full_abstract,
        rendering_section_abstracts=selection.rendering_section_abstracts,
        rendering_segmentation=selection.rendering_segmentation,
        rendering_liveliness=selection.rendering_liveliness,
        rendering_output_formats=list(selection.rendering_output_formats),
        rendering_science_order=list(selection.rendering_science_order),
        rendering_science_consultation_authority=selection.rendering_science_consultation_authority,
        rendering_target_body_characters=selection.rendering_target_body_characters,
        rendering_scope_description=selection.rendering_scope_description,
        literature_writing_policy=selection.literature_writing_policy,
        personas=([Persona.LIBRARIAN] if selection.literature_writing_policy == "fast" else None),
        writer_model=selection.writer_model,
        fast_planner_models=list(selection.fast_planner_models),
        fast_planner_reasoning_effective={
            f"FAST_PLANNER_{index}": collapse_reasoning_effort(
                config, model, selection.representative_reasoning_effort,
            )
            for index, model in enumerate(selection.fast_planner_models, start=1)
        },
        writer_reasoning_effort=(
            collapse_reasoning_effort(config, selection.writer_model, selection.writer_reasoning_effort)
            if selection.writer_model is not None and selection.writer_reasoning_effort is not None
            else None
        ),
        technician_model=selection.technician_model,
        technician_reasoning_effort=(
            collapse_reasoning_effort(config, selection.technician_model, selection.technician_reasoning_effort)
            if selection.technician_model is not None and selection.technician_reasoning_effort is not None
            else None
        ),
    )
    install_human_references(repo, selection.human_reference_paths)
    if selection.report_palette not in PALETTES:
        raise ValueError("unknown report palette")
    if selection.deliverable_type in {
        DeliverableType.LITERATURE_REVIEW, DeliverableType.SCHOLARLY_RENDERING,
    }:
        repo.docs.write_once(
            PALETTE_DOCUMENT,
            json.dumps({
                "palette": selection.report_palette,
                "scope": "PRESENTATION_ONLY_HTML_PDF",
                "colors": {
                    key: value for key, value in PALETTES[selection.report_palette].items()
                    if key not in {"zh", "en"}
                },
            }, ensure_ascii=False, indent=2),
        )
    if selection.prompt_development is not None:
        repo.docs.write_once(
            "human_private/prompt_development.json",
            json.dumps(selection.prompt_development, indent=2, ensure_ascii=False),
        )
    if (selection.deliberation_language is not None
            and selection.deliverable_type == DeliverableType.NORMATIVE_INSTRUMENT):
        repo.docs.write_once(
            "public/deliberation_writing_preferences.json",
            json.dumps({
                "meeting_id": repo.meeting_id,
                "language": selection.deliberation_language,
                "scope": "reader_facing_deliberation_text_only",
            }, ensure_ascii=False, indent=2),
        )
    if (selection.deliverable_type == DeliverableType.LITERATURE_REVIEW
            and (selection.literature_language or selection.literature_target_body_characters is not None)):
        repo.docs.write_once(
            "public/literature_report/writing_preferences.json",
            json.dumps({
                "language": selection.literature_language or "zh",
                "full_abstract": bool(selection.literature_full_abstract),
                "section_abstracts": bool(selection.literature_section_abstracts),
                "segmentation_1_to_5": selection.literature_segmentation,
                "liveliness_1_to_5": selection.literature_liveliness,
                "signposting_1_to_5": selection.literature_signposting or 4,
                "target_body_characters": selection.literature_target_body_characters,
                "fact_first_writing": selection.fact_first_writing,
                "length_tolerance_fraction": 0.2 if selection.literature_target_body_characters is not None else None,
            }, indent=2, ensure_ascii=False),
        )
    return repo

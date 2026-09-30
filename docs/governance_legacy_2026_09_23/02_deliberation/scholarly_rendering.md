# Scholarly Rendering Meeting

## Purpose

A `scholarly_rendering` meeting transforms a frozen literature-review report
into reader-facing scholarly prose. It is a derivative publication procedure,
not a new evidence-gathering deliberation. The inherited report and evidence
packets remain immutable and downloadable.

Chair and lead renderer are the same runtime. Chair may remove internal
procedural language, improve structure and readability, and apply the Human's
chosen language, abstract, article-skeleton, segmentation and liveliness
settings. Chair must not negate, add, strengthen or weaken a frozen scientific
conclusion merely for readability.

## Panels

Initialization separately selects at least two science-bookkeeping models and
at least two citation-bookkeeping models. Each selected model creates one
role-scoped reviewer. The Human freezes the science-review order. Citation
review is parallel. Public appendices list the union of models and reasoning
controls without exposing reviewer-to-model/persona mappings; full authorship
remains audit-private.

## Science bookkeeping

Each bounded section receives two sequential review rounds. A reviewer reads
the frozen source, current redraw and all earlier comments in that round. YES
carries no comment; NO is constituted by one or more concrete objections. A
proposed scientific correction beyond the source must be sent to Research Desk.
It may inform Chair only when the returned packet contains supporting evidence;
otherwise its proposer loses the final ballot for that section. Research/QC
failure does not establish support. A Research Desk availability failure is
recorded as an unsupported correction and does not block the rest of the
meeting; configuration, authorization and governance-policy errors remain
fatal. Completed verification outcomes are frozen per section and round, and
their immutable request or evidence records are reused after recovery.

Chair may revise after either review round. A third, parallel YES/NO ballot
contains no further editing. Absolute majority of eligible science reviewers is
required. Failure pauses the section for Human–Chair dialogue and a Human choice
between source text, current redraw, or Human-supplied wording.

## Citation bookkeeping

Citation review begins only after science review and any Human resolution.
There are two citation rounds. Every reviewer submits a review; the issue list
may be empty. Every issue is atomized as INSERT or REMOVE. MOVE is forbidden and
must be represented by one REMOVE plus one INSERT. Reviewers vote YES/NO on all
amendments in parallel; absolute-majority results are advisory to Chair, who
retains final application authority but records exactly one disposition for
every amendment. Chair cannot return a rewritten body at this stage: the
orchestrator mechanically applies only the accepted INSERT/REMOVE units to the
current text. Numeric inline citations must resolve to unique entries in the
reference section, and non-peer-reviewed sources are marked in the reference
entry.

If an accepted amendment cannot identify exactly one text anchor, Chair gets
one bounded anchor-only repair. A remaining ambiguity pauses for Human choice:
skip only the ambiguous amendments, keep the pre-citation text, or supply final
wording. An incomplete citation vote receives one contract-only repair; if it
still fails, Human may authorize one new attempt or record that reviewer's
ballot as missing. No frozen response is deleted or overwritten.

Chair's citation docket identifiers are normalized mechanically into numeric
order before freezing. A decision set that omits or duplicates amendments gets
one contract-only repair; a remaining defect pauses for Human choice instead
of creating a permanently replayed exception.

## Publication

The meeting emits at least one Human-selected format among Markdown, LaTeX and
PDF. Only the selected output language is produced; no translation appendix is
added. The main report excludes internal ENSEMBLE terminology. A separately
visible appendix preserves rendering configuration, source hash, Chair's plan,
model unions and reasoning controls. The original report remains a prominent
root-level artifact. The execution-result completion marker is written only
after all selected publications, root links, provenance records and the
publication event have been committed.

Bracketed numbers are checked as citations only when the report contains an
explicit References/Bibliography section. Within such a section, identifiers
must be unique, continuous from 1, and cover every numeric inline citation. A
failure pauses publication for a Human decision to publish with a visible
warning or supply final wording.

Parallel reviewer lanes are keyed by the effective runtime after audited
Ctrl+R replacements, not by the frozen registry runtime. Reviewers replaced
from different original models into one target model therefore share that
target model's concurrency limit.

Citation reviewers receive a bounded evidence index: at most 300 packets and
60,000 serialized characters, with an explicit truncation record when either
limit is reached. Only packets actually injected into a reviewer's context are
recorded as that reviewer's file reads.

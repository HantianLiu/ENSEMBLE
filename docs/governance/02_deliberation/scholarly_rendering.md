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

The reader-facing body, headings and tables use natural-language research
topics, not ENSEMBLE's internal module numbers or storage fields such as
`RM-xx`, `SR-xx`, `record_id` or evidence-packet IDs. Removing these internal
labels is not a loss of scientific content and must not be reversed by science
or citation reviewers in the name of completeness. The original-to-rendered
mapping stays auditable outside the body. Actual bibliographic citations and
scientifically meaningful external identifiers remain available to readers.

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
carries no substantive objection, but either vote may carry an optional
advisory `style_note`. It does not change the tally and need not be adopted or
explained by the next writer. NO is constituted by one or more concrete objections. A
change in structure or wording is not itself an objection, but may support NO
when the reviewer identifies its location and explains a substantive change
in scientific meaning, argument dependencies, scope, uncertainty or evidence
mapping. An omission may be located using an adjacent surviving passage;
reviewers must not invent a quotation from the redraw. A proposed scientific
correction beyond the source must be sent to Research Desk.
It may inform Chair only when the returned packet contains supporting evidence;
otherwise its proposer loses the final ballot for that section. Research/QC
failure does not establish support. A Research Desk availability failure is
recorded as an unsupported correction and does not block the rest of the
meeting; configuration, authorization and governance-policy errors remain
fatal. Completed verification outcomes are frozen per section and round, and
their immutable request or evidence records are reused after recovery.

Chair normally preserves the frozen source's conclusions and may correct a
redraw that drifted from them without treating that restoration as a new
scientific claim. Changing a conclusion in the frozen source itself is allowed
only for an explicit reviewer-proposed correction that appears in the verified
correction set and has at least partial Research Desk support. Chair must keep
the changed conclusion within the supported scope, preserve uncertainty and
record the objection and evidence packet in the revision disposition.

Chair may revise after either review round. A third, parallel YES/NO ballot
contains no further editing or style note. A NO ballot must identify concrete objections to
the **current, post-revision** redraw and quote the current passage; already
repaired objections from either review round do not enter the Human docket.
In newly initialized rendering meetings, before each final science ballot a
science reviewer may request at most four Research Desk checks **per section,
across all revision cycles**. Requests are sequential: the reviewer reads the
previous result before deciding whether to ask again. Exact duplicate claims
reuse their frozen result without spending another check; a request rejected as
not externally verifiable also does not spend one. Research Desk quality-control
or availability failures spend a check but never count as supporting evidence.
Only a newly proposed correction whose returned packet contains at least some
supporting evidence may enter the final NO ballot. Persisted decisions, requests,
and results survive interruption; a reviewer who still submits an unsupported
new correction after three ballot attempts loses eligibility for that section's
final ballot. Meetings initialized before this allowance was introduced retain
their original final-ballot rule and cannot gain a query window by resuming.
Old-format frozen ballots remain immutable. Their NO voters make one separately
archived clarification on the current draft before tallying; a corrected
YES/NO supersedes the old vote only for this tally while the original remains
available for audit. An unanswered consultation based on the old ballot is
withdrawn with an append-only public record after clarification; if the
clarified vote still objects, a fresh issue uses only its current-draft
objections. Already recorded Human rulings retain their original effect and
are never withdrawn or silently reinterpreted. Historical review objections
are never a substitute for missing objections on an unanswered final ballot.
Absolute majority of eligible science reviewers is required. If the ballot
fails, all distinct objections on the final ballots are docketed before
the Human consultation begins. Chair presents them one at a time, with a
source/redraw comparison and an actionable suggestion. For each
objection the Human may accept it, reject it, or give one natural-language
revision direction; the Human may ask Chair one question at a time and inspect
the complete source and redraw. The Human is not required to write replacement
Markdown. Chair must not revise between objections: only after all rulings
are recorded does Chair revise the existing redraw, preserving the
frozen source's conclusions and using only verified external corrections. The
revision undergoes two new science-review rounds and a new final ballot, with
separate immutable records for each cycle. Citation review begins only after a
science ballot passes. Already resolved legacy Human consultations retain their
original effect; an open legacy consultation is superseded by this itemized
process without deleting its record.

For this itemized science-objection consultation only, the Human may authorize
Chair to make future decisions. The default is Human decision; initialization
may opt into Chair delegation, and the Human may grant or revoke it while the
meeting is running. Each change is an immutable, Human-attributed record and
affects only unresolved objections after that change. Each delegated decision
records Chair, not Human, as its author and names the authorization record.
Chair may choose only the same accept, reject or local-revision options already
offered to the Human; no other Human consultation, citation decision or source
conclusion is delegated. If Chair cannot produce a valid decision, the issue
remains open for Human intervention. Earlier rulings are never rewritten.

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
one bounded anchor-only repair. A proposed INSERT or REMOVE whose target still
has zero or multiple matches after that repair is invalid and is not applied.
The orchestrator records each invalid amendment and its match count in a
reader-accessible audit artifact, then applies the remaining valid amendments
in docket order. If skipping one invalid amendment exposes another anchor
conflict, that amendment is invalidated in the same way. An unanswered Human
consultation opened under the earlier ambiguity policy is withdrawn with an
append-only record; an already recorded Human ruling retains its original
effect. An incomplete citation vote receives one contract-only repair; if it
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

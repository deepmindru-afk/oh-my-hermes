# Capability Impact

OMH should improve what Hermes does after setup, not merely add more files or
commands. The impact contract therefore answers six separate questions:

This report is an agent and maintainer inspection surface. Normal users can ask
Hermes about capability or setup health without running these commands.

1. Did OMH select the right workflow?
2. Does the selected workflow carry useful quality and safety guidance?
3. Did the Hermes host actually load the native OMH tools and hooks?
4. Are optional external providers available for the requested work?
5. Was the produced artifact checked at the surface where users consume it?
6. Did OMH produce a better result than a comparison setup?

Agents and maintainers can run the local report with:

```sh
omh capabilities impact
omh capabilities impact --json
```

## Current Evidence

The report deliberately has no aggregate score. A single percentage would make
strong local routing evidence look interchangeable with an unavailable video
provider or an unrun comparative benchmark.

| Dimension | Current status | What the status means |
| --- | --- | --- |
| Route selection | `passing_local_contract` | Fixed precision, common-request coverage, and representative cases all pass as in-repo contracts; representative cases remain implementation smoke checks, not independent outcome evidence. |
| Guidance depth | `partially_proven` | Skill quality bars and evidence boundaries are inspectable; better task outcomes still need paired evaluation. |
| Native execution availability | `requires_host_observation` | Tool and hook registration exists, but only host load and invocation records prove availability in a real Hermes session. |
| Provider execution availability | `requires_provider_observation` | Browser, connector, image, video, and structural-search providers vary by installation. |
| Artifact verification | `partially_proven` | OMH can request the right served-surface check; the check result must still be observed. |
| Comparative outcome quality | `requires_external_evaluator` | “Better than another setup” remains unproven until blinded paired tasks are scored externally. |

Structural search is one such optional provider class: `omh doctor` and
`omh probe` resolve the `ast-grep` binary on PATH without executing it, so
presence is locally detectable. Whether Hermes or a delegated executor
actually uses it — and any resulting change in exploration cost — is not
observable from OMH and stays prepared guidance, not observed benefit.

The deterministic report currently evaluates three route sets:

- `fixed_precision`: the fixed routing boundaries, negative controls plus
  expected interventions, including over-route and missed-intervention checks
- `common_requests`: common Hermes requests spanning the popular plugin-style
  families
- `representative`: representative English and Korean requests, including
  focused intents and collision-prone near neighbors

The focused cases lock distinctions that broad keyword matching commonly gets
wrong: TDD versus an article about testing, literature review versus project
memory, screenshot layout QA versus OCR, deployment versus production audit,
session recovery versus handoff status, image editing versus image summary, and
video generation versus video summarization.

## Focused Skill Selection

OMH does not treat the installed skill count as a quality score. The router
should select the smallest useful workflow set and keep unrelated guidance out
of context. This direction matches the SkillsBench finding that curated skills
can improve results while small two- or three-skill bundles outperform large
comprehensive bundles in the reported aggregate analysis.

References:

- [SkillsBench paper](https://arxiv.org/html/2602.12670v4)
- [Hermes Agent hook documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md)
- [Hermes Agent releases](https://github.com/NousResearch/hermes-agent/releases)

## Served-Surface Verification

The OMH plugin registers a narrow `pre_verify` hook only when the Hermes host
advertises that lifecycle. Older hosts that do not expose `pre_verify` skip the
hook without warnings while keeping the rest of the OMH plugin available.
After edited code reaches Hermes verification, the hook requests one additional
check only for surfaces where ordinary code tests are insufficient:

- plugin manifest changes require a real plugin load and registration smoke
- frontend changes require rendered desktop and mobile checks
- dependency or build metadata changes require installation or import smoke
- CI workflow changes require a syntax check or the workflow's local command

A turn in the coding posture that edited a file with a source suffix also gets
a round-end consistency check, once, at its first stop (`attempt == 0`). The
coding posture means Hermes's `cli`, `tui`, `acp` or `desktop` surface in a
code workspace, or `agent.coding_context: on`; the suffixes are listed in
`_CODE_SUFFIXES` in `hooks/verify_hooks.py`.

The check asks the model to look for other copies or call sites of the logic
it changed that have to change with it, and to fix any it finds. It also tells
the model not to re-run tests for this check, unless something beside it asks
for a run or a re-check: the served-surface request above, or the opt-in impact
check's note about importers the edit broke. The message keeps a fixed order:
served-surface gate, impact note, consistency check, plan directive, and one
closing sentence for the final answer.

Hermes keeps the answer the model stopped with only as an interim message, and
returns whatever follows the nudge as the turn's final response. So when no
plan is open, the message ends with one closing sentence: if nothing more
changed, the model replies with the single line `OMH: no further changes.`.

- `pre_verify` stashes the answer the turn stopped with, in process and per
  session.
- `transform_llm_output` swaps that line back to the stashed answer before
  Hermes persists it.
- The stash is consumed only by that swap. A leftover is cleared at the
  session's next turn start (`pre_llm_call`, which Hermes's detached review
  forks do not reach), overwritten by the next stash, or dropped after an hour.
- The stored transcript, `--oneshot`, API callers and parents of delegated runs
  get the original answer byte for byte.
- Each surface shows it differently:
  - The OMH TUI keeps the streamed line, so the answer is not shown twice.
  - Hermes's own CLI prints the restored answer a second time under
    "[Response transformed after streaming]".
  - Desktop and Hermes's Ink TUI replace the streamed line with the restored
    answer.
- The replay history holds the answer twice, the interim row and the final
  row. Later turns pay for that in tokens.

With an open plan the turn goes on, so there is no closing and nothing is
stashed.

Delegated children (`platform == "subagent"`) never get the check, and when
only the impact note fires for one, its closing asks for the full answer and
nothing is stashed. The child's reply is its parent's tool result. The skip
only matters under `agent.coding_context: on`, because otherwise Hermes gives
non-interactive platforms no coding posture.

The cost is one extra round of the turn. Measured costs and gains are in the
2026-10-30 section of `benchmarks/live-model-tools/v1/README.md`.
`agent.max_verify_nudges: 0` turns this off together with every other
`pre_verify` nudge.

The same hook carries one further directive: when the session's own OMH plan
still has open work, or a finished dispatch is unacknowledged, the message also
names the plan position and the next item. This is the only moment a host lets
a plugin start the next turn, so it is where a run that would otherwise end its
turn on a status report is told to advance instead. It stops on the plan's own
stop criterion — every item done, the next item recorded blocked with its
reason, or the plan recording that the person steered the session elsewhere.
That last one is a plan-level `deferred_reason`, and it is stored with a digest
of the item list it was written against: a reader honours it only while that
digest still matches, so marking an item done or re-scoping the list lapses the
deferral and the plan resumes with nobody having cleared anything. A writer
that deliberately re-sends the reason with a changed list has declared a new
deferral for that list, the way an item's `blocked_reason` is re-declared on
every write. Nothing infers a redirection from the conversation; the record
declares it.
Its reach is bounded by when the host fires the hook at all: Hermes
gates `pre_verify` on the turn having made at least one landed `write_file` or
`patch` call, so a turn that only read, searched, ran tests, or reported never
reaches it, and neither does a turn whose file edits went through the shell.

How much of the host's turn-end budget the plan directive may spend is decided
on progress, not on the attempt number. Hermes allows `agent.max_verify_nudges`
nudges per turn (3 by default) and enforces that bound itself. The first
attempt nudges; a later attempt in the same turn nudges only when the plan
record was written again since the previous nudge, so a run that keeps
advancing is carried to the host's bound and a run that produced no movement
spends one nudge and lets the turn finish. The served-surface check stays
one-shot: it asks for the check this turn's edits earned, and those edits do
not change between attempts.

The hook is fail-open and metadata-only. It does not store raw changed paths or
final responses, and its message is guidance rather than proof that
verification, review, CI, or merge readiness occurred. The plan directive
reports what the plan record says and is never evidence that an item ran.

## Growth Gate

The next product-quality claim should come from paired outcome evaluation, not
from adding more catalog entries. A representative benchmark should run the
same Hermes tasks with and without OMH, blind the outputs, and score correctness,
completeness, evidence use, artifact quality, recovery behavior, and wasted
tool/context cost. Until that evaluator exists, OMH can prove routing and local
contracts, but it should not claim overall superiority.

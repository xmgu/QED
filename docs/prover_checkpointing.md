# Per-step proof checkpointing

Design record for writing the proof in per-step files instead of one final
`proof.md`, and for rebuilding the document from those files when a prover
session is cut off.

## The problem

A prover turn is one long agent session. In `run_20261023_103103` the prover
call recorded 1,398,282 input tokens over 5,721 s; the session that died
recorded **0 in / 0 out over 5,610 s**, meaning no `turn.completed` event ever
fired. Nothing was written.

Before commit `39cab90` the pipeline then copied the agent's last chat message
into `proof.md`, so an interrupted session left one line of narration standing
in as its proof. That is now rejected. But rejecting it means the attempt
yields *nothing*: the plan, the reading, and every finished step are lost, and
the next attempt starts from scratch on a plan that has already burned 93
minutes of budget.

The structure of a real run makes the waste visible. `revision_4` has

```
proof_order: [STEP1, STEP2, STEP3, STEP4, STEP5, STEP6, STEP7a, STEP7b, GOAL]
```

and `proof_1/proof.md` is 1,580 lines / 85 KB with exactly those nine headings.
A proof that naturally decomposes into nine units was being persisted as one
all-or-nothing write.

## What changed

The prover is now told to write each step to its own file as soon as the step is
finished, and the pipeline assembles `proof.md` from those files.

```
proof_N/
├── proof_steps/
│   ├── 00_front.md      # "# Proof" + "## Problem Statement" + "## Proof"
│   ├── STEP1.md         # begins with "### STEP1: <title>"
│   ├── …
│   ├── STEP7b.md
│   ├── GOAL.md
│   └── 99_notes.md      # Unresolved Steps / Key Ideas / Deviations
├── proof.md             # model's own write, or assembled here
├── proof_partial.md     # only when the checkpoint is incomplete
├── prover_rejected.md   # only when there is nothing at all
├── scratchpad.md
└── prover_response.md
```

### Why the model does not assemble

Asking the model to concatenate its own step files would re-emit the whole
proof, roughly doubling output tokens (~21 k for an 85 KB document). Assembly is
deterministic and therefore belongs in code. The model writes each step exactly
once; total output is unchanged.

### Why `proof_order`, not filenames

Filenames sort wrongly in two ways: `GOAL` precedes `STEP*` alphabetically, and
`STEP10` precedes `STEP2`. Real plans also use suffixed ids (`STEP7a`,
`STEP7b`). `step_order()` therefore reads `proof_order` from the decomposition
and only falls back to natural sort (digits parsed as integers, `GOAL` last)
when no decomposition is available.

## Decision table

| Step checkpoints | `proof.md` | Handling |
|---|---|---|
| all nine, all non-trivial | valid | unchanged: use the model's file |
| all nine | absent | assemble → treat as complete → normal verification |
| some | absent | assemble → `proof_partial.md` → `ProofIncomplete` → skip verification |
| none | absent | unchanged: `ProofNotProduced` |
| — | valid | unchanged: model wrote it in one pass |

`looks_like_step()` requires 200 non-whitespace characters. Real steps run to
thousands; the threshold only separates "wrote something" from "wrote a
placeholder", so a stub counts as missing rather than as progress.

### Why a partial skips verification

Structural verification Phase 4 requires a `### STEPn:` heading and a proof for
every id in `proof_order`. An incomplete checkpoint fails that by construction,
so a verification call would spend a round-trip confirming what the file listing
already states. The regulator instead receives a deterministic gap list, plus the
statement that this was a truncation and not a mathematical failure — otherwise
it tends to read "no report" as "the proof was wrong".

The partial is written to `proof_partial.md` and never promoted to
`proof_dir/proof.md` or the run's top-level `proof.md`, so a run that dies here
cannot leave a partial document standing in as its result.

## An unresolved step is not a missing step

The two look alike on disk and mean opposite things:

| Signal | Meaning | Handling |
|---|---|---|
| no `STEP6.md` | session ended before the step was written | truncation, skip verification |
| `STEP6.md` containing `**Status:** not proved` | the step is genuinely open | verification runs, Phase 2a.4 fails it as an acknowledged gap |

So the prompt requires a file for **every** id in `proof_order`; a step that
cannot be proved gets a file that says so, with `Tried:` and `Breaks at:` filled
in. This preserves the declared-versus-concealed distinction from `single_prover`
in the artifact layout itself.

When the checkpoint is complete, the pipeline appends no `## Unresolved Steps`
section — the model's own `99_notes.md` carries it, and a generated
"none — all steps proved" would contradict a step file that declares a gap. When
the checkpoint is incomplete, the pipeline generates the section, because in that
case the model never got a chance to.

## Code

| Location | Change |
|---|---|
| `decomposition_prover.py` | `ProofIncomplete(ProofNotProduced)` carrying `present` / `missing` / `partial_file` |
| | `looks_like_step()`, `steps_dir()`, `_natural_step_key()`, `step_order()` |
| | `collect_step_files()`, `_front_matter()`, `_unresolved_section()`, `assemble_proof_from_steps()` |
| | `run_single_prover()`: on a missing `proof.md`, try the checkpoint before the chat-response fallback |
| | proof loop: catch `ProofIncomplete`, synthesise the feedback, point `proof_file` at the partial |
| `single_prover.md` | `## Output Format` rewritten as a per-file protocol; `{proof_steps_dir}` placeholder; checklist item |
| `smoke_test.py` | render kwargs, Test 3d (assembler) |

## Costs

| | Before | After |
|---|---|---|
| output tokens, completed run | ~21 k | ~21 k (same document, written in nine pieces) |
| tool round-trips | 1 write | 9 writes |
| output after truncation | 0 | up to 8/9 of the proof |
| next attempt after truncation | restarts | can reuse the checkpoint (P1) |

Worst case, if the model ignores the protocol and also writes `proof.md` at the
end, output doubles. The prompt forbids rewriting finished steps and the
assembler prefers an existing `proof.md`, so the failure mode is a cost
regression, not a correctness one.

## Not done (P1)

Reusing the partial on the next attempt — passing `{previous_partial_file}` so
the prover continues from the first missing step instead of restarting — is a
separate change. This commit only makes the interruption *visible and bounded*.

Also out of scope: whether an interrupted attempt should consume one of
`max_proof_attempts`. It does, by design, unchanged.

## Verification

1. Two real narration `proof.md` files (`revision_3/proof_4`,
   `revision_4/proof_2`) → still `ProofNotProduced`, no regression.
2. A real 85 KB proof split into nine checkpoints and reassembled → same nine
   headings, `proof_order` sequence.
3. A synthetic six-of-nine checkpoint → `present`/`missing` correct,
   `proof_partial.md` written, top-level `proof.md` untouched, verification
   skipped.
4. Existing regressions: 13 real verification reports parse identically; resume
   detection on all five runs unchanged.

---
name: clone-hunt
description: Find duplicated code across a codebase and decide what is actually worth extracting into shared helpers. Runs a cheap deterministic clone scan first, then reasons only about the ranked results. Use this skill whenever the user mentions duplicate code, copy-paste, repeated logic, boilerplate, DRY violations, "extract a helper", "this is repeated everywhere", or asks to find refactoring opportunities across more than one file — even if they do not use the word "duplicate". Prefer this over ad-hoc grepping, which only finds literal repetition and floods context with file reads.
---

# Clone hunt

Finding duplicated code by reading files does not scale: the codebase does not
fit in context, and grep only matches literal repetition. Instead, delegate
detection to a scanner and spend the context budget on judgment.

**Never start by reading source files. Run the scan first.**

## Workflow

### 1. Scan

Run the bundled script. It sits next to this file, so invoke it by absolute
path — the working directory is the user's repo, not this skill's directory:

```bash
# installed as a plugin:
python3 "$CLAUDE_PLUGIN_ROOT/skills/clone-hunt/scripts/clonerank.py" --run . --top 10

# committed to a repo under .claude/skills/:
python3 .claude/skills/clone-hunt/scripts/clonerank.py --run . --top 10
```

On Windows use `python` — `python3` is often absent or a Store stub that
exits without running anything. If neither path to the script exists, find it:
`find . ~/.claude -name clonerank.py 2>/dev/null`.

Requires `jscpd` (`npm install -g jscpd`). If it is missing, install it — do
not fall back to grep. The scan writes its report to a temp directory and
cleans up, so it leaves nothing behind in the user's repo.

Useful flags:

| Flag | When |
| --- | --- |
| `--min-tokens 80` | Repo is noisy; raise the floor |
| `--min-tokens 30 --min-lines 3` | Small repo, nothing found |
| `--include 'tests/helpers/'` | User explicitly wants test code included |
| `--exclude '(^\|/)legacy/'` | Areas known to be frozen |
| `--json` | Feeding another script |
| `--no-git` | Not a git repo, or history is a fresh import |

The output is a ranked menu, not source code. It costs a few hundred tokens
regardless of repo size.

### 2. Triage before reading

Each cluster reports three things worth reading carefully:

- **holes** — token positions where copies genuinely disagree. These become
  the helper's parameters.
- **renames** — positions that differ only in a bound name (the function's own
  name, a local variable). These are *not* parameters; they disappear on
  extraction. A cluster with `holes: 0, renames: 3` extracts cleanly.
- **co-change** — commits that touched two or more copies. Evidence the
  duplication already costs real edits.

Decide from the menu alone:

- **0–2 holes and co-change > 0** — extract. The copies drift together and the
  helper needs few parameters. This is the case worth doing.
- **0 holes, co-change 0** — probably extract, but check whether the copies are
  coincidentally identical rather than conceptually the same thing.
- **3+ holes** — usually leave alone. A helper with five parameters and three
  boolean flags is worse than the duplication.
- **Copies in unrelated bounded contexts** — leave alone even when identical.
  Coupling two modules through a shared helper to save twelve lines is a bad
  trade. Two `Address` validators in `billing/` and `shipping/` may be the same
  today and diverge next quarter.

Clusters that score below the bar appear under **Scored below the bar** rather
than being dropped, precisely so they can be named. Report the triage to the
user before editing anything — including that section. Naming what you are
*not* extracting, and why, is as valuable as the extraction.

If a cluster reports `holes: unknown`, the scanner could not read every copy
from disk (a stale report, or a path that moved). Re-run the scan rather than
guessing at it.

### 3. Read only the chosen clusters

Now read the files, at the exact line ranges from the menu. Read every copy,
not just the representative — the differences are the whole design problem.

### 4. Extract

- Put the helper where both callers already depend, not in a new `utils/`
  dump. If no such place exists, that is a signal the extraction may be wrong.
- Turn holes into parameters in the order they appear.
- Prefer a value parameter over a boolean flag. Two call sites passing
  `true`/`false` into one branchy function usually means two functions.
- Keep the helper's name about *what it does*, not where it came from
  (`validatePagination`, not `sharedApiLogic`).
- Match the surrounding code's conventions — check an existing helper in the
  same directory first.

### 5. Verify

Run the test suite. If the affected code has no tests, say so before editing
and offer to add characterization tests first — extraction across four call
sites with no coverage is a silent-breakage risk, not a cleanup.

Then re-run the scan to confirm the cluster is gone and nothing new appeared.

## Limits worth stating out loud

jscpd is token-based. It finds identical and lightly-renamed code, but not
logic that was restructured while being copied — reordered statements, a `for`
rewritten as `map`, inverted conditionals. A clean scan means "no textual
clones", not "no duplication". When the user suspects deeper structural
duplication, reach for `ast-grep` or `semgrep` with a hand-written pattern and
say why.

Hole classification is a token heuristic, not a parser: it treats an
identifier following a binding keyword (`function`, `const`, `def`, `class`, …)
as a rename. It will miscount for languages that bind names differently, so
treat the hole count as a strong hint, not a measurement.

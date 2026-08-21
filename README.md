# clone-hunt

A Claude Code plugin. Scans for duplicated code with jscpd, ranks each cluster
by whether it is actually worth extracting (parameter count + git co-change),
and guides the refactor.

Then in any repo, ask Claude: *"find duplicate code worth extracting"*.

Requires `jscpd`: `npm install -g jscpd`

## Try it before it's published

```
claude --plugin-dir /path/to/clone-hunt
```

The plugin loads for that session only, no install needed. This is also how
anyone else can use it straight from a `git clone`.

## Install (once accepted into the community marketplace)

```
/plugin marketplace add anthropics/claude-plugins-community
/plugin install clone-hunt@claude-community
```

## Layout

The repo *is* the plugin — its root is the plugin root:

```
.claude-plugin/plugin.json      <- manifest, the only file that goes in here
skills/clone-hunt/
  SKILL.md                      <- skill name must match this directory
  scripts/clonerank.py
```

Nothing but `plugin.json` belongs inside `.claude-plugin/`; `skills/` sits at
the root beside it. There is deliberately no `marketplace.json` — the
community catalog serves that role.

## Publishing

1. Push this repo to GitHub, public.
2. `claude plugin validate .` — the review pipeline runs the same check.
3. Submit at [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit).
   (There's also a claude.ai form, but it needs a Team/Enterprise org.)

Once approved, the plugin is pinned into `anthropics/claude-plugins-community`
at a commit SHA, and their CI bumps the pin as you push. The public catalog
syncs nightly, so expect a lag between approval and installability.

## Releasing

Bump `version` in `.claude-plugin/plugin.json` and push. Users only receive
updates when that field changes.

## Developing clonerank

The ranker is stdlib-only Python with no test suite yet. To exercise it
without running jscpd, hand it a report directly:

```
python skills/clone-hunt/scripts/clonerank.py \
  --report some-jscpd-report.json --root /path/to/repo --no-git --json
```

Use `--report-dir DIR` with `--run` to keep the generated jscpd report instead
of discarding it with the temp directory.

# clone-hunt

A Claude Code plugin. Scans for duplicated code with jscpd, ranks each cluster
by whether it is actually worth extracting (parameter count + git co-change),
and guides the refactor.

Current release: **v1.0.1**

Requires `jscpd`: `npm install -g jscpd`

## Use it now

```
claude --plugin-dir /path/to/clone-hunt
```

The plugin loads for that session only, no install needed. This is how anyone
can use it straight from a `git clone` while the marketplace submission is
pending.

## Install (once accepted into the community marketplace)

```
/plugin marketplace add anthropics/claude-plugins-community
/plugin install clone-hunt@claude-community
```

Either way, then ask Claude in any repo: *"find duplicate code worth
extracting"*.

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

1. ~~Push this repo to GitHub, public.~~ done
2. ~~`claude plugin validate .` — the review pipeline runs the same check.~~ passing
3. Submit at [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit).
   (There's also a claude.ai form, but it needs a Team/Enterprise org.)
   **Not yet submitted.**

Once approved, the plugin is pinned into `anthropics/claude-plugins-community`
at a commit SHA, and their CI bumps the pin as you push. The public catalog
syncs nightly, so expect a lag between approval and installability.

## Releasing

Users only receive updates when `version` in `.claude-plugin/plugin.json`
changes, so bump it *before* pushing — a git tag on its own triggers nothing.

```
# 1. bump "version" in .claude-plugin/plugin.json
claude plugin validate .
git commit -am "vX.Y.Z"
git push
git tag -a vX.Y.Z -m "clone-hunt vX.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."
```

The tag and GitHub release are for humans reading the repo; the manifest
`version` is what Claude Code actually watches.

## License

MIT. See [LICENSE](LICENSE).

## Developing clonerank

The ranker is stdlib-only Python with no test suite yet. To exercise it
without running jscpd, hand it a report directly:

```
python skills/clone-hunt/scripts/clonerank.py \
  --report some-jscpd-report.json --root /path/to/repo --no-git --json
```

Use `--report-dir DIR` with `--run` to keep the generated jscpd report instead
of discarding it with the temp directory.

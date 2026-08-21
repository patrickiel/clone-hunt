#!/usr/bin/env python3
"""
clonerank - rank clone-detector output by "worth extracting", not by size.

Detection is delegated to jscpd. This tool does the part jscpd doesn't:
merges pairs into clusters, drops noise, measures how cleanly each cluster
would extract, checks whether the copies actually drift together in git
history, and emits a compact menu an LLM can act on without reading files.

  clonerank.py --run .                    # run jscpd, then rank
  clonerank.py --report .jscpd/jscpd-report.json
  clonerank.py --run . --json             # machine-readable

Stdlib only. Requires jscpd on PATH for --run (npm i -g jscpd).
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

# Paths that produce duplication nobody should refactor. Applied after the
# scan, so --include can override any of them.
DEFAULT_EXCLUDES = [
    r"(^|/)node_modules/", r"(^|/)vendor/", r"(^|/)dist/", r"(^|/)build/",
    r"(^|/)target/", r"(^|/)\.jscpd", r"(^|/)coverage/", r"(^|/)__pycache__/",
    r"(^|/)tests?/", r"(^|/)spec/", r"(^|/)fixtures?/", r"(^|/)__tests__/",
    r"(^|/)__mocks__/", r"(^|/)testdata/", r"(^|/)migrations?/",
    r"(^|/)generated/", r"(^|/)__generated__/", r"\.generated\.",
    r"\.min\.(js|css)$", r"\.pb\.go$", r"_pb2\.py$", r"\.g\.dart$",
    r"\.designer\.cs$", r"\.snap$", r"\.lock$", r"-lock\.json$",
]

# Handed to jscpd so it never reads these at all. Deliberately narrower than
# DEFAULT_EXCLUDES: anything --include might resurrect (tests, generated code)
# has to reach the report, or the override would have nothing to override.
SCAN_IGNORE_GLOBS = [
    "**/.git/**", "**/.jscpd/**", "**/node_modules/**", "**/vendor/**",
    "**/dist/**", "**/build/**", "**/target/**", "**/coverage/**",
    "**/__pycache__/**", "**/*.min.js", "**/*.min.css", "**/*-lock.json",
    "**/*.lock",
]

TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|\d+\.?\d*|\"[^\"]*\"|'[^']*'|\S")

# Keywords that introduce a name. An identifier immediately after one is the
# block's own name or a local binding -- both vanish when the block is
# extracted, so they are renames, not parameters.
BINDERS = {
    "function", "func", "fn", "def", "defn", "sub", "proc", "method",
    "class", "struct", "interface", "enum", "type", "trait", "impl",
    "const", "let", "var", "val",
}
IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


# ---------------------------------------------------------------- detection

def run_jscpd(path, min_lines, min_tokens, outdir):
    # Keep the resolved path: on Windows npm installs jscpd as a .cmd shim,
    # which which() finds via PATHEXT but CreateProcess will not resolve from
    # a bare name -- subprocess would die with FileNotFoundError.
    exe = shutil.which("jscpd")
    if not exe:
        sys.exit("jscpd not found on PATH. Install with: npm install -g jscpd")
    cmd = [
        exe, path,
        "--min-lines", str(min_lines),
        "--min-tokens", str(min_tokens),
        "--reporters", "json",
        "--output", outdir,
        "--ignore", ",".join(SCAN_IGNORE_GLOBS),
        "--silent",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    report = os.path.join(outdir, "jscpd-report.json")
    if not os.path.exists(report):
        sys.exit(f"jscpd produced no report.\n{proc.stdout}\n{proc.stderr}")
    return report


# ------------------------------------------------------------------ parsing

class Loc:
    """One instance of a duplicated block."""

    def __init__(self, name, start, end):
        # NB: not lstrip("./") -- that strips characters, eating the leading
        # dot of paths like .jscpd/ and .github/.
        name = name.replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        self.path = name
        self.start = int(start)
        self.end = int(end)

    def key(self):
        return (self.path, self.start, self.end)

    def overlaps(self, other):
        return (self.path == other.path
                and self.start <= other.end and other.start <= self.end)

    def __str__(self):
        return f"{self.path}:{self.start}-{self.end}"


def make_path_filter(excludes, includes):
    """A path is noise if it matches an exclude and no --include overrides it."""
    ex = [re.compile(p) for p in excludes]
    inc = [re.compile(p) for p in includes]

    def is_noise(path):
        if any(p.search(path) for p in inc):
            return False
        return any(p.search(path) for p in ex)

    return is_noise


def load_pairs(report_path, is_noise):
    try:
        # jscpd writes UTF-8; the platform default (cp1252 on Windows) either
        # raises or silently mojibakes non-ASCII paths into files we then
        # cannot open, which surfaces much later as "holes: unknown".
        with open(report_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        sys.exit(f"No such report: {report_path}\n"
                 f"Generate one with: clonerank.py --run <path>")
    except UnicodeDecodeError as exc:
        sys.exit(f"{report_path} is not valid UTF-8 ({exc}).")
    except json.JSONDecodeError as exc:
        sys.exit(f"{report_path} is not valid JSON ({exc}).\n"
                 f"Expected a jscpd report from --reporters json.")
    if "duplicates" not in data:
        sys.exit(f"{report_path} has no 'duplicates' key -- is this a jscpd JSON report?")
    pairs, skipped = [], 0
    for dup in data.get("duplicates", []):
        a = Loc(dup["firstFile"]["name"], dup["firstFile"]["start"], dup["firstFile"]["end"])
        b = Loc(dup["secondFile"]["name"], dup["secondFile"]["start"], dup["secondFile"]["end"])
        if is_noise(a.path) or is_noise(b.path):
            skipped += 1
            continue
        pairs.append({
            "a": a, "b": b,
            "lines": dup.get("lines", 0),
            "tokens": dup.get("tokens", 0),
            "format": dup.get("format", ""),
        })
    return pairs, skipped


def cluster(pairs):
    """Union-find over locations: A~B and A~C means {A,B,C} is one cluster."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    locs = {}
    for p in pairs:
        for loc in (p["a"], p["b"]):
            locs[loc.key()] = loc
        union(p["a"].key(), p["b"].key())

    # Aggregate only once every union has settled. Roots move as clusters
    # merge, so anything keyed by an intermediate root gets orphaned -- which
    # silently understated a cluster's tokens, and with it its score.
    meta = {}
    for p in pairs:
        m = meta.setdefault(find(p["a"].key()),
                            {"lines": 0, "tokens": 0, "format": ""})
        m["lines"] = max(m["lines"], p["lines"])
        m["tokens"] = max(m["tokens"], p["tokens"])
        m["format"] = m["format"] or p["format"]

    groups = defaultdict(list)
    for key, loc in locs.items():
        groups[find(key)].append(loc)

    out = []
    for root, members in groups.items():
        m = meta.get(root, {"lines": 0, "tokens": 0, "format": ""})
        members.sort(key=lambda l: (l.path, l.start))
        out.append({"locs": members, **m})
    return out


def drop_subsumed(clusters):
    """Drop clusters whose every location is already covered by a larger one.

    jscpd emits overlapping blocks for the same site. A cluster that only
    partially overlaps a kept one survives -- its uncovered locations are
    real findings.
    """
    clusters.sort(key=lambda c: c["tokens"], reverse=True)
    kept = []
    for c in clusters:
        covered = 0
        for k in kept:
            for loc in c["locs"]:
                if any(loc.overlaps(other) for other in k["locs"]):
                    covered += 1
                    break
        if covered < len(c["locs"]):
            kept.append(c)
    return kept


# -------------------------------------------------------------------- holes

def read_block(path, start, end, root):
    full = os.path.join(root, path)
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    return "".join(lines[max(0, start - 1):end])


def find_holes(cluster_locs, root, max_examples=3):
    """
    Token-align every instance against the first one. A 'hole' is a position
    where instances disagree. Holes that are just a bound name (the block's own
    name, a local) are counted separately -- they disappear on extraction. The
    rest become the helper's parameters: zero or one means a clean extraction,
    many means a bad helper.

    Returns (parameters, examples, renames); parameters is None if a copy could
    not be read.
    """
    blocks = []
    for loc in cluster_locs:
        text = read_block(loc.path, loc.start, loc.end, root)
        if text is None:
            return None, [], 0
        blocks.append(TOKEN_RE.findall(text))
    if len(blocks) < 2:
        return 0, [], 0

    rep = blocks[0]
    holes = defaultdict(set)
    for inst in blocks[1:]:
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, rep, inst, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            span = (i1, i2)
            holes[span].add(" ".join(inst[j1:j2]) or "<empty>")

    def is_bound_name(i1, i2):
        return (i2 - i1 == 1
                and bool(IDENT_RE.match(rep[i1]))
                and i1 > 0 and rep[i1 - 1] in BINDERS)

    spans = sorted(holes)
    params = [s for s in spans if not is_bound_name(*s)]
    renames = len(spans) - len(params)

    def clip(s, n=48):
        s = s.replace("\n", " ").strip()
        return s if len(s) <= n else s[:n - 3] + "..."

    examples = []
    for span in params[:max_examples]:
        i1, i2 = span
        base = " ".join(rep[i1:i2]) or "<empty>"
        shown = sorted(v for v in holes[span] if v != base)[:2]
        examples.append(
            f"{clip(base)} -> {' | '.join(clip(v) for v in shown)}" if shown else clip(base)
        )
    return len(params), examples, renames


# ---------------------------------------------------------------- co-change

class GitHistory:
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.enabled = os.path.isdir(os.path.join(root, ".git"))

    def commits(self, path):
        if not self.enabled:
            return set()
        if path not in self.cache:
            proc = subprocess.run(
                ["git", "-C", self.root, "log", "--format=%H", "--", path],
                capture_output=True, text=True)
            self.cache[path] = set(proc.stdout.split()) if proc.returncode == 0 else set()
        return self.cache[path]

    def cochange(self, paths):
        """Commits that touched two or more of these files."""
        paths = sorted(set(paths))
        if not self.enabled or len(paths) < 2:
            return None
        counts = defaultdict(int)
        for p in paths:
            for sha in self.commits(p):
                counts[sha] += 1
        return sum(1 for n in counts.values() if n >= 2)


# ----------------------------------------------------------------- scoring

def score(cluster, holes, cochange):
    copies = len(cluster["locs"])
    tokens = cluster["tokens"]
    saved = tokens * (copies - 1)

    savings_pts = min(50.0, saved / 20.0)
    dirs = {os.path.dirname(l.path) for l in cluster["locs"]}
    spread_pts = min(15.0, 5.0 * (len(dirs) - 1))
    cochange_pts = min(25.0, 5.0 * cochange) if cochange else 0.0
    hole_penalty = 6.0 * max(0, (holes or 0) - 1)

    total = savings_pts + spread_pts + cochange_pts - hole_penalty
    return max(0, round(total)), {
        "saved_tokens": saved,
        "savings_pts": round(savings_pts, 1),
        "spread_pts": round(spread_pts, 1),
        "cochange_pts": round(cochange_pts, 1),
        "hole_penalty": round(hole_penalty, 1),
    }


# ------------------------------------------------------------------ output

def snippet(cluster, root, max_lines):
    loc = cluster["locs"][0]
    text = read_block(loc.path, loc.start, loc.end, root) or ""
    lines = text.rstrip("\n").split("\n")
    if len(lines) > max_lines:
        head = max_lines - 1
        lines = lines[:head] + [f"  ... ({len(lines) - head} more lines)"]
    return "\n".join(lines)


def render(results, rejected, root, snippet_lines, skipped):
    out = []
    if not results and not rejected:
        out.append("No clone clusters survived filtering.")
        if skipped:
            out.append(f"({skipped} pairs excluded as tests/generated/vendor noise.)")
        return "\n".join(out)

    out.append(f"# {len(results)} clone clusters, ranked by extraction value")
    out.append("")
    for i, r in enumerate(results, 1):
        c = r["cluster"]
        copies = len(c["locs"])
        out.append(
            f"## C{i} | score {r['score']} | {copies} copies x {c['lines']}L "
            f"| ~{r['parts']['saved_tokens']} tokens of duplicated maintenance"
        )
        for loc in c["locs"]:
            out.append(f"  {loc}")
        holes = r["holes"]
        if holes is None:
            out.append("  holes: unknown -- could not read every copy from disk")
        elif holes == 0:
            out.append("  holes: 0 -- extracts with no parameters")
        else:
            ex = "; ".join(r["hole_examples"])
            out.append(f"  holes: {holes} -- would become parameters: {ex}")
        if r["renames"]:
            out.append(f"  renames: {r['renames']} (bound names, not parameters)")
        if r["cochange"]:
            out.append(f"  co-change: {r['cochange']} commits touched 2+ of these copies")
        elif r["cochange"] == 0:
            out.append("  co-change: 0 -- copies have never been edited together")
        out.append("")
        out.append("```")
        out.append(snippet(c, root, snippet_lines))
        out.append("```")
        out.append("")

    if rejected:
        out.append(f"## Scored below the bar ({len(rejected)})")
        out.append("")
        out.append("Detected, deliberately not ranked. Report these as considered "
                   "and rejected rather than omitting them.")
        for r in rejected:
            c = r["cluster"]
            why = (f"{r['holes']} holes" if r["holes"]
                   else f"only ~{r['parts']['saved_tokens']} tokens saved")
            out.append(f"  {', '.join(str(l) for l in c['locs'])} "
                       f"({len(c['locs'])} copies x {c['lines']}L, {why})")
        out.append("")

    out.append("---")
    out.append(
        "Scoring: token savings + cross-directory spread + git co-change, "
        "minus a penalty per extra parameter hole. High co-change means the "
        "copies drift together, so the duplication is already costing real "
        "edits. Many holes means an extracted helper would need many "
        "arguments -- usually a sign to leave it alone."
    )
    if skipped:
        out.append(f"{skipped} pairs excluded as tests/generated/vendor noise.")
    return "\n".join(out)


def as_json(results):
    return [{
        "score": r["score"],
        "copies": len(r["cluster"]["locs"]),
        "lines": r["cluster"]["lines"],
        "tokens": r["cluster"]["tokens"],
        "locations": [str(l) for l in r["cluster"]["locs"]],
        "holes": r["holes"],
        "hole_examples": r["hole_examples"],
        "renames": r["renames"],
        "cochange_commits": r["cochange"],
        "score_parts": r["parts"],
    } for r in results]


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", metavar="PATH", help="run jscpd on PATH, then rank")
    src.add_argument("--report", metavar="FILE", help="rank an existing jscpd JSON report")
    ap.add_argument("--root", default=None, help="repo root (default: --run PATH or cwd)")
    ap.add_argument("--min-lines", type=int, default=5)
    ap.add_argument("--min-tokens", type=int, default=50)
    ap.add_argument("--top", type=int, default=10, help="clusters to report")
    ap.add_argument("--min-score", type=int, default=1,
                    help="clusters below this are listed as rejected, not dropped")
    ap.add_argument("--snippet-lines", type=int, default=12)
    ap.add_argument("--include", action="append", default=[],
                    help="path regex that overrides the noise filter (repeatable), "
                         "e.g. --include 'tests/helpers/'")
    ap.add_argument("--exclude", action="append", default=[],
                    help="extra path regex to exclude (repeatable)")
    ap.add_argument("--report-dir", default=None,
                    help="where --run writes the jscpd report (default: a temp "
                         "dir, removed on exit)")
    ap.add_argument("--no-git", action="store_true", help="skip co-change analysis")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    root = os.path.abspath(args.root or (args.run if args.run else os.getcwd()))
    is_noise = make_path_filter(DEFAULT_EXCLUDES + args.exclude, args.include)

    scratch = None
    if args.run:
        outdir = args.report_dir or tempfile.mkdtemp(prefix="clonerank-")
        scratch = None if args.report_dir else outdir
        report = run_jscpd(args.run, args.min_lines, args.min_tokens, outdir)
    else:
        report = args.report

    try:
        pairs, skipped = load_pairs(report, is_noise)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    clusters = drop_subsumed(cluster(pairs))
    git = GitHistory(root)
    if args.no_git:
        git.enabled = False

    scored = []
    for c in clusters:
        holes, examples, renames = find_holes(c["locs"], root)
        co = git.cochange([l.path for l in c["locs"]])
        pts, parts = score(c, holes, co)
        scored.append({"cluster": c, "score": pts, "parts": parts,
                       "holes": holes, "hole_examples": examples,
                       "renames": renames, "cochange": co})

    scored.sort(key=lambda r: r["score"], reverse=True)
    # Below-the-bar clusters are surfaced, not dropped: a cluster that would
    # need six parameters is a decision to report, not a gap in the scan.
    results = [r for r in scored if r["score"] >= args.min_score][:args.top]
    rejected = [r for r in scored if r["score"] < args.min_score]

    if args.json:
        print(json.dumps({"ranked": as_json(results),
                          "rejected": as_json(rejected)}, indent=2))
    else:
        print(render(results, rejected, root, args.snippet_lines, skipped))


if __name__ == "__main__":
    try:  # don't traceback when piped into head/less
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass
    try:  # snippets carry whatever encoding the source used, and the console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    main()

# Issue Localizer

Given a GitHub issue's title and body, Issue Localizer predicts which files
in the repo most likely need to change to fix it — a task usually called
*bug* or *issue localization*, a well-studied problem in software
engineering research (typically tackled with IR-style retrieval or learned
models over bug reports and source). This project builds a small
retrieval-augmented agent for it — a local semantic code index, an LLM
agent with search/grep/read tools, and an eval harness — and measures it
against real historical data: 80 closed [python-pillow/Pillow](https://github.com/python-pillow/Pillow)
issues, each paired with the files their actual merged fix PR changed.
Current result: **0.740 precision / 0.553 recall / 0.591 F1** (macro-avg,
73/80 issues scored), with a **31.5% full-hit rate** (every changed file
found).

## Results

Evaluated against 80 real Pillow issues (73 scored; 7 excluded as API infra
failures, not prediction failures) using `gemini-3.5-flash-lite` as the
agent's reasoning model:

| Metric | Value |
|---|---|
| Precision (macro-avg) | 0.740 |
| Recall (macro-avg) | 0.553 |
| F1 (macro-avg) | 0.591 |
| Avg. tool-call turns per example | 5.5 (cap: 6) |
| Full-hit rate (every correct file found) | 31.5% (23/73) |

![Precision, recall, and F1 by failure category](results/eval_chart.png)

Full category breakdown, failure analysis, and per-example results:
[results/eval_report.md](results/eval_report.md). Regenerate with
`python src/evaluate.py` (see [Setup](#setup--reproduction) below).

### Iteration

The first pass (v1) had strong precision but weak recall: the agent tended
to find one correct file and stop, under-covering issues that plausibly
touched multiple files (e.g. a plugin file + its test file, or a C-level
implementation file behind a Python plugin). Two changes targeted that:
raising `semantic_search`'s `top_k` from 8 to 20 so the agent sees more
candidate chunks per query, and prompting it to explicitly check for likely
sibling files (a test file, a C source file) before finalizing predictions,
rather than stopping at the first plausible match.

| Metric | v1 | v2 (current) |
|---|---|---|
| Precision | 0.758 | 0.740 |
| Recall | 0.447 | 0.553 |
| F1 | 0.512 | 0.591 |
| Full-hit rate | 22.7% | 31.5% |

Recall and F1 both improved meaningfully for only a marginal precision
cost. Full before/after data: [results/eval_report_v1.md](results/eval_report_v1.md)
vs. [results/eval_report.md](results/eval_report.md).

## Architecture

```
GitHub issue/PR history              Target repo source (Pillow)
        |                                      |
        v                                      v
  mine_dataset.py                       build_index.py
  (GitHub GraphQL API)        (shallow clone -> AST-chunk by
        |                      function/class -> embed locally
        v                      with sentence-transformers ->
data/eval_dataset.jsonl        persist to on-disk Chroma index)
  {issue, changed_files}                       |
        |                                      v
        |                          .cache/chroma (semantic index)
        |                                      |
        +------------------+-------------------+
                            v
                        agent.py
        Gemini-driven tool-calling loop, given only the issue
        text -- ground truth is never shown to it:
          - semantic_search: query the Chroma index
          - grep_repo:       exact symbol/text search, incl. C files
          - read_file:       pull more context on a promising hit
          - submit_predictions: ranked files + one-sentence reasoning
                            |
                            v
                       evaluate.py
        Runs the agent over every eval example, scores predicted
        vs. actual changed_files (precision/recall/F1), buckets
        each into complete_miss / partial_hit / full_hit, and
        writes eval_report.md + eval_chart.png
```

1. **Mine ground truth** ([src/mine_dataset.py](src/mine_dataset.py)) --
   walks Pillow's closed issues via the GitHub GraphQL API, keeps only
   those closed by exactly one merged PR (no guessing), and records
   `{issue_title, issue_body, changed_files}` to `data/eval_dataset.jsonl`.
2. **Build the code index** ([src/build_index.py](src/build_index.py)) --
   shallow-clones Pillow, chunks its Python source by function/class/method
   with `ast` (not arbitrary line splits), embeds each chunk locally with
   `sentence-transformers`, and persists the embeddings to an on-disk
   Chroma collection so it's only built once.
3. **Agent** ([src/agent.py](src/agent.py)) -- given just an issue's
   title/body, a Gemini Flash model drives a multi-turn tool-calling loop
   (semantic search, grep, file reads, capped at 6 tool calls) and submits
   a ranked list of predicted files with one-sentence reasoning each.
4. **Eval harness** ([src/evaluate.py](src/evaluate.py)) -- runs the agent
   over every example in `data/eval_dataset.jsonl`, scores predictions
   against the real `changed_files`, categorizes failures, and writes the
   report and chart under `results/`.

## Setup / reproduction

```bash
git clone <this-repo> && cd issue-localizer
pip install -r requirements.txt
cp .env.example .env
# edit .env and set both:
#   GITHUB_TOKEN=<a GitHub personal access token>   (for mine_dataset.py)
#   GEMINI_API_KEY=<a Gemini API key>                (for agent.py / evaluate.py)
```

`data/eval_dataset.jsonl` and everything under `results/` are already
committed, so you don't need to reproduce every step just to look at the
results. Run order to reproduce from scratch:

```bash
# 1. Mine the eval dataset (optional -- already committed)
python src/mine_dataset.py

# 2. Build the local semantic code index (clones Pillow, ~1-2 min)
python src/build_index.py

# 3. Sanity-check the agent standalone against a few real examples
python src/agent.py --indices 0 2 18

# 4. Full eval run (all 80 examples; supports --resume if it gets rate-limited)
python src/evaluate.py
```

Useful flags on `evaluate.py`: `--limit N` (quick test on the first N
examples), `--indices i j k` (specific examples), `--resume` (continue a
partial run without re-scoring finished examples), `--report-only`
(regenerate `eval_report.md`/`eval_chart.png` from an existing results file
without calling the agent again).

## Limitations and what I'd try next

- **Single-repo dataset.** All 80 examples come from Pillow. The agent's
  tools (semantic search over `src/`, grep, file reads) are repo-agnostic,
  but nothing here tests whether these results hold on a codebase with a
  different structure, language mix, or issue-writing culture.
- **Recall is still the weaker metric.** Even after the v2 changes, full
  hits average 1.4 ground-truth files while complete misses and partial
  hits average 3.3-3.6 -- retrieval breadth on heavily multi-file changes
  is still the main bottleneck, not precision/hallucination.
- **Non-source ground-truth files are essentially invisible to the
  agent's tools.** Several complete misses have ground truth that's
  entirely docs, CI config, packaging, or license files (e.g. "Missing TCL
  license in third-party licenses", "Reduce AVIF wheel size?", "Wheel:
  Loading zstd-compressed TIFF fails, even with imagecodecs") --
  `semantic_search` only indexes Python source, and the agent has no
  reason to `grep` for files it doesn't know exist.
- **API rate limits caused real data loss, not deliberate exclusion.** 7
  of 80 examples in the v2 run failed to score because the Gemini free
  tier's per-minute limits were exceeded even after 5 retries with
  backoff -- those are excluded from the metrics as infra failures, but a
  paid tier (or a slower/more patient runner) would recover them.
- **No self-verification.** The agent commits to predictions after a fixed
  tool-call budget with no way to check its own work -- e.g. by running the
  actual test suite, diffing against similar historical PRs, or checking
  whether predicted files even import/reference each other. A production
  version of this would likely need that feedback loop.
- **Small sample size.** 73-80 scored examples means a few percentage
  points of precision/recall shouldn't be over-interpreted as a stable
  estimate; re-running the same code can shift the numbers by a point or
  two just from which examples happen to hit rate limits.

Next steps I'd prioritize: give the agent visibility into non-Python and
non-indexed files (a repo file-tree/glob tool, or indexing docs/CI/config
too); add a lightweight self-check step before submission; and re-run this
same harness against a second, structurally different repo to see how much
of the current performance is Pillow-specific.

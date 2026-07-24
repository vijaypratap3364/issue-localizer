# Issue Localizer — Project Deep Dive

This is an exhaustive internal reference document, not the polished external
README. It's written for interview prep: full technical detail, full build
history, every number that appears anywhere in the project, honest
limitations, and direct answers to likely interview questions. Where a fact
isn't documented anywhere in the codebase or git history, that's said
explicitly below rather than guessed.

---

## 1. What this project is and why

**The problem.** Given a GitHub issue's title and body — plain-English bug
report text, no code, no stack trace guaranteed — predict which files in
the repository would need to change to fix it. This is *bug localization*
(sometimes *issue localization*), a real, long-studied problem in software
engineering research, not something invented for this project. The
standard framings in that literature are information-retrieval-based
(treat the bug report as a query, the codebase as a document collection,
and rank files by relevance — classic techniques use TF-IDF/vector-space
similarity between report text and source, sometimes combined with
structural signals like recently-changed files or stack traces) and, more
recently, learned/neural approaches trained on historical bug-report-to-fix
mappings. This project sits closer to the IR-based tradition but replaces
a static ranking function with an LLM agent that can iteratively search,
grep, and read — closer to how a human engineer actually investigates an
unfamiliar codebase than a single-shot similarity ranking would be.

**Why it's a real problem, not a toy.** Large codebases routinely receive
issue reports from users who have no idea which file is broken — they know
symptoms, not internals. Before anyone can fix a bug, someone (or
something) has to figure out *where to look*. That triage step is
expensive at scale and is exactly what automated bug localization tools
target — either to speed up human triage or as the first stage of an
automated program repair pipeline (you can't generate a patch for a file
you never identified as relevant).

**What makes this different from a generic RAG chatbot.** A RAG chatbot
answers open-ended questions and is judged subjectively ("does this answer
seem helpful"). This project produces one specific, structured,
falsifiable output — a ranked list of file paths — and grades it
automatically against a real, verifiable ground truth: the actual files
changed by the actual merged pull request that closed that actual issue.
There's no human-in-the-loop quality judgment anywhere in the eval; either
the predicted file matches the real changed-file set or it doesn't. That's
a meaningfully harder and more honest bar than "the response looks
reasonable."

**What makes this different from a PR-review wrapper (Copilot PR review,
CodeRabbit, etc.).** Those tools operate on an *existing diff* — a human
or process has already decided which files changed, and the tool's job is
to comment on whether those specific changes look correct. This project
has no diff to look at. It starts from an issue report *before any code
has been written* and has to figure out where a fix would even go. That's
retrieval/localization, a strictly earlier and different problem than
review. See section 7 for a direct comparison.

---

## 2. Full architecture walkthrough

Four scripts under `src/`, one shared `config.py`, connected in a
pipeline:

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
        text -- ground truth is never shown to it
                            |
                            v
                       evaluate.py
        Scores predicted vs. actual changed_files, categorizes
        failures, writes eval_report.md + eval_chart.png
```

### 2.1 `src/config.py` — shared configuration

All tunables for every phase live here, read once. Key values (exact, as
currently set):

- `REPO_OWNER = "python-pillow"`, `REPO_NAME = "Pillow"` — the target repo;
  changing these two values is the entire "point this at a different repo"
  story for both mining and indexing.
- `TARGET_EXAMPLES = 80`, `MAX_ISSUES_SCANNED = 3000`, `MIN_CHANGED_FILES = 1`,
  `MAX_CHANGED_FILES = 20` — dataset mining bounds.
- `INDEX_INCLUDE_DIRS = ["src"]` — only this subtree of the cloned repo is
  chunked/embedded.
- `EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"` — local sentence-transformers
  model.
- `GEMINI_MODEL_NAME = "gemini-3.5-flash-lite"` — see 2.3 and section 3 for
  why this exact string, after several rejected candidates.
- `AGENT_SEMANTIC_SEARCH_TOP_K = 20` (raised from `8`), `AGENT_GREP_MAX_RESULTS = 20`,
  `AGENT_READ_FILE_MAX_LINES = 400`, `AGENT_MAX_TOOL_CALLS = 6` (lowered
  from `10`) — agent tool/loop tuning, both changed mid-project based on
  observed behavior (section 3).
- Both `GITHUB_TOKEN` and `GEMINI_API_KEY` are read via
  `os.environ.get(..., "")` **at import time** — this matters: every
  script that needs either value must call `load_dotenv()` *before*
  `import config`, or the `.env` file's values are never picked up. This
  bit both `mine_dataset.py` and, independently, `agent.py` during
  development (see section 3) — both were fixed by moving `load_dotenv()`
  above the `import config` line.

### 2.2 `src/mine_dataset.py` — ground-truth mining

Builds `data/eval_dataset.jsonl` from Pillow's real closed-issue history
via the **GitHub GraphQL API** (not REST — chosen specifically because
GraphQL's `issue.timelineItems(itemTypes: [CLOSED_EVENT])` field exposes
the actual `closer` of an issue, including whether it was a merged pull
request, directly in one query; the REST API doesn't expose that
relationship as cleanly).

- `ISSUES_QUERY`: paginates closed issues (`orderBy: UPDATED_AT DESC`),
  and for each one fetches its last 10 `CLOSED_EVENT` timeline items, each
  with its `closer` (if a `PullRequest`: `number`, `merged`,
  `changedFiles`, and up to 100 `files.nodes.path`).
- `extract_merged_closer(issue)`: collects every distinct **merged** PR
  across all `CLOSED_EVENT`s for that issue into a dict keyed by PR
  number. Returns a result **only if exactly one** distinct merged PR
  closer exists — an issue closed/reopened by different PRs at different
  times is treated as unattributable and skipped. This is a deliberate
  precision-over-recall choice for the ground truth itself: a noisy
  dataset would make every downstream metric meaningless.
- Additional filters in `main()`: empty title/body skipped; a PR with
  `files.totalCount > 100` skipped (the query only paginates 100 files, so
  a bigger PR can't be confirmed complete); final `changed_files` count
  must fall in `[MIN_CHANGED_FILES, MAX_CHANGED_FILES] = [1, 20]` (keeps
  the dataset to scoped bug-fix-sized PRs, not sprawling refactors).
- `run_graphql_query()`: hand-rolled retry/backoff — reads
  `X-RateLimit-Remaining`/`X-RateLimit-Reset` response headers and
  proactively sleeps if remaining capacity drops under 50; on `403`/`429`
  respects a `Retry-After` header if present, else computes a wait from
  the rate-limit reset time; on `5xx`, exponential backoff starting at 5s,
  doubling, capped at 120s; up to 6 attempts before raising.
- Output: `data/eval_dataset.jsonl`, one JSON object per line:
  `{"issue_title": ..., "issue_body": ..., "changed_files": [...]}`.

### 2.3 `src/build_index.py` — local semantic code index

Four sub-stages, each originally built and tested standalone (see section
3):

1. **`clone_repo()`** — shallow-clones (`--depth 1`) the target repo into
   `.cache/repo` (gitignored — a regenerable build artifact, not source
   the project owns). If already cloned, does `git fetch --depth 1
   origin` + `git reset --hard origin/HEAD` instead of a full re-clone.
2. **`chunk_repository()` / `_collect_chunks()`** — walks every `*.py`
   file under `INDEX_INCLUDE_DIRS` (`src/`) and parses it with Python's
   `ast` module. For every `ClassDef`, emits one chunk containing **only
   the class signature + docstring** (not the full body — methods are
   chunked separately, so a class chunk doesn't duplicate large method
   bodies that already have their own entry). For every
   `FunctionDef`/`AsyncFunctionDef` (top-level → type `"function"`,
   nested under a class → type `"method"`), emits one chunk with the full
   source including decorators. Deliberately does **not** descend into
   `if`/`for`/`try` blocks looking for further nested defs — top-level and
   class/function-nested defs cover the vast majority of real code, and
   chasing every control-flow-nested def is a complexity/completeness
   trade-off that wasn't worth making for this codebase.

   **Why function/class chunking instead of fixed-size line windows** (a
   design decision made deliberately, not a default): a chunk boundary
   drawn by AST node never splits a function's logic mid-body, so every
   chunk reads as a coherent, meaningful unit rather than an arbitrary
   fragment that could mislead the embedding or confuse whatever reads it
   downstream. Each chunk also gets a real identity — a qualified name
   like `Image.resize` — which is directly useful as citable evidence in
   the agent's reasoning and matches how a PR diff is actually scoped in
   practice (PRs change whole functions/methods, not line ranges chosen
   independently of code structure). It also avoids the redundant,
   near-duplicate embeddings a sliding line window produces, and keeps
   almost every chunk within the effective input size of a small
   embedding model without truncation.

3. **`embed_chunks()` / `_embedding_text()`** — each chunk is embedded as
   a short header (`# {file} :: {qualified_name} ({type})`) followed by
   its source, giving the embedding model symbol/location context, not
   just bare code. Uses **`sentence-transformers`'s `all-MiniLM-L6-v2`**
   locally (384-dimensional vectors), batch size 64.

   **Why a local embedding model instead of a paid embedding API:** zero
   marginal cost per embed call (this runs once for the whole repo at
   index-build time, plus once per `semantic_search` call at agent
   runtime — no external quota to manage on this path at all, in direct
   contrast to the very real Gemini API quota problems hit later); no
   added latency/failure mode from an external network call on the
   embedding step; works fully offline once the model weights are cached;
   and the quality bar for this specific role — surface plausibly-related
   code chunks to an LLM that will itself re-verify and re-rank them via
   `grep_repo`/`read_file` — is lower than for a retrieval-only system
   with no downstream verification step. MiniLM's 384-dim vectors are
   adequate for that coarse-recall role.

4. **`get_chroma_collection()` / `persist_chunks()` / `build_index()`** —
   persists chunks + embeddings + metadata (`file`, `qualified_name`,
   `type`, `start_line`, `end_line`) to an on-disk **ChromaDB**
   `PersistentClient` collection (`hnsw:space: "cosine"`), batched at 500
   items per `add()` call. `build_index(rebuild=False)`: if the collection
   already has data, the **entire** clone/chunk/embed pipeline is skipped
   and the existing collection is returned as-is — `clone_repo()` is not
   even called in that path. This means a normal re-run of
   `build_index.py` does **not** refresh either the cloned repo or the
   index; both stay frozen at whatever state they were in when first
   built (or last `--rebuild`). See section 6 for the concrete
   consequence of this.

`query_index()` embeds a query string with the same model and calls
`collection.query(n_results=top_k)`.

### 2.4 `src/agent.py` — the tool-using agent

Given only an issue's title and body (ground truth is never shown to it),
runs a multi-turn Gemini function-calling loop and returns a ranked list
of predicted files with reasoning.

**Three real tools, one terminal "tool":**

- **`semantic_search(query, top_k)`** — wraps `build_index.query_index`.
  `top_k` defaults to `config.AGENT_SEMANTIC_SEARCH_TOP_K` (currently
  `20`). Only ever sees what's in the Chroma index — i.e., only Python
  source under `src/`.
- **`grep_repo(pattern, regex=False)`** — shells out to `git grep -n -I
  --no-color -F/-E <pattern>` inside `.cache/repo`. Covers **every** file
  in the clone, any language, unlike `semantic_search` — this is the only
  way the agent can find hits in Pillow's C sources (`src/*.c`,
  `src/libImaging/*.c`) or any non-Python file. `git grep`'s exit code `1`
  (no matches) is treated as an empty result, not an error.
- **`read_file(path, start_line, end_line)`** — reads a file or line range
  from the clone. Path-traversal guarded: resolves the requested path
  under `.cache/repo` and checks `target.relative_to(repo_dir)` — anything
  that escapes via `..` or an absolute path raises `ValueError`, caught
  and turned into a clean `{"error": ...}` response rather than ever
  reading outside the repo. (Verified standalone against
  `"../../../../etc/passwd"`, which was correctly rejected.)
- **`submit_predictions(predictions)`** — not a real action; it's a
  function declaration the model calls to report its final ranked list
  (`file` + a required one-sentence `reasoning` per item, capped at 5
  entries). Calling it ends the loop.

**`call_gemini()`** — a raw REST client (`requests.post` against
`{GEMINI_API_BASE}/models/{model}:generateContent`), not the official
`google-genai` SDK, chosen for full transparent control over retry
behavior (matching the hand-rolled retry pattern already used in
`mine_dataset.py`). Retries on: HTTP `429` (honoring `Retry-After` if
present), HTTP `5xx`, and **network-level exceptions**
(`requests.exceptions.RequestException` — timeouts, connection resets;
this was a real gap found and fixed, see section 3). Exponential backoff
from 2s, doubling, capped at 60s; 5 attempts by default; 120s per-request
timeout (raised from an original 60s); raises a clear `RuntimeError`
naming the last error after retries are exhausted, rather than hanging or
silently failing.

**`localize_issue()`** — the loop itself. System instruction (built by
`_build_system_instruction(max_tool_calls)`) tells the model: its exact
tool-call budget, to start with `semantic_search` then use `grep_repo` for
exact symbols (especially non-Python ones), to use `read_file` for
confirmation, to explicitly check for likely sibling files (test file,
C-level implementation) before finalizing without exceeding budget doing
so, and to give exactly one decisive sentence of reasoning per prediction
with no visible step-by-step thinking. The loop runs up to
`max_tool_calls` turns; **on the final turn**, `toolConfig` is set to
`{"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames":
["submit_predictions"]}}`, forcing the model to call `submit_predictions`
rather than potentially running out of budget with nothing submitted.

**Why a hard turn cap (currently 6, was 10):**
1. Cost/quota control — an unbounded loop could burn arbitrary numbers of
   requests against a genuinely scarce free-tier quota (section 3).
2. Empirically, more turns did not correlate with correctness: both eval
   runs' "observed patterns" show `complete_miss` examples averaging
   *more* turns than `full_hit` examples (v1: 5.2 vs 4.7; v2: 5.4 vs
   5.1) — the agent that's going to fail isn't rescued by more searching;
   it's out of leads, not out of budget.
3. It was lowered from 10 to 6 after a specific observed failure: on the
   TGA-transparency example, the model used its entire 10-turn budget
   chasing a plausible-but-wrong secondary file (`TgaRleDecode.c`, not
   actually in the ground truth) instead of stopping once it had a
   confident answer.

**Why Gemini Flash, not Pro:** the original stated reasoning — much higher
free-tier rate limits, and this task (retrieval + tool orchestration +
short structured output) doesn't need frontier-level reasoning. In
practice this needed a second layer of investigation: several
current-generation Flash-tier models turned out to have their own very
restrictive free-tier daily quotas, so "Flash not Pro" was the right
principle but had to be narrowed further to a specific model empirically
(full story in section 3).

### 2.5 `src/evaluate.py` — the eval harness

Runs the agent over every example in `data/eval_dataset.jsonl`, scores it,
and writes a report + chart. Explicitly does not modify `agent.py` or
`build_index.py` — it only measures what they already do.

- **`run_example()`** wraps `agent.localize_issue()` in a `try`/`except`.
  A raised exception (i.e. `call_gemini` exhausted its retries) is
  recorded with a `run_error` string and **excluded from scoring** — it's
  an infra failure (API unavailable), not a real prediction outcome, and
  conflating the two would make the metrics answer a worse, less useful
  question. Note: `agent.localize_issue()`'s internal `trace` list (every
  tool call and result for that run) is **not** included in the result
  dict this function returns and is therefore never persisted to
  `results/eval_results.jsonl` — only the final predictions/reasoning are
  kept.
- **`compute_metrics(predicted_files, actual_files)`** — standard set
  precision/recall/F1: `precision = tp / |predicted|`, `recall = tp /
  |actual|`, `f1 = 2·p·r / (p+r)`.
- **`_dedupe_top5()`** — the agent's ranked predictions (already capped at
  5 by the tool schema) are deduped by file path, order preserved, before
  scoring.
- **`categorize(predicted_files, actual_files)`** — see section 4 for the
  exact definitions of `complete_miss` / `partial_hit` / `full_hit`.
- **`category_breakdown()` / `detect_patterns()`** — per-category macro
  averages (precision/recall/F1/turns/issue-body length/ground-truth file
  count) and a handful of threshold comparisons (e.g. is average issue
  body length for misses under 70% of full-hits') that get written into
  the report as concrete, data-backed bullets rather than unverified
  narrative.
- **`write_report()` / `generate_chart()`** — `results/eval_report.md`
  (headline table, category table, patterns, failure-analysis tables with
  ✓-marked hits, full per-example table) and `results/eval_chart.png`
  (matplotlib, `Agg` backend since this never needs a display, grouped bar
  chart of precision/recall/F1 per category).
- **Resumability**: `--resume` skips example indices already present in
  the results file (keyed by `index`, the 0-based line number in
  `data/eval_dataset.jsonl`); the runner stops early after **3
  consecutive** `run_error`s (likely a systemic outage/quota exhaustion,
  not worth grinding through the rest of the batch repeating the same
  failure). In practice, neither the v1 nor v2 full run ever hit this —
  both runs' failures were scattered, not consecutive, so all 80 examples
  were attempted both times.
- **`--report-only`** regenerates the report/chart from an existing
  results file without calling the agent again — used repeatedly during
  this project specifically to avoid burning API quota on report-format
  iteration.

---

## 3. Phase-by-phase build history

Built over 4 calendar days (2026-07-20 through 2026-07-23), as a sequence
of small, individually-tested commits (27 commits as of the commit before
this document, including 4 GitHub-merged pull requests from an
early-project branch-based workflow).

### Phase 1 — Ground-truth mining (2026-07-20, commit `1a85c34`)

Picked Pillow (`python-pillow/Pillow`) as the target repo: mid-size, not a
toy and not enormous, actively maintained, with a long history of issues
closed by clean single-PR merges. Built `mine_dataset.py` against the
GraphQL API for the reason in 2.2.

**Nontrivial things that came up:**
- GitHub API rate limits are real even scanning a few hundred issues — the
  proactive/reactive backoff logic in `run_graphql_query()` exists because
  of that, not speculatively.
- A `load_dotenv()`-ordering bug: `config.py` reads `GITHUB_TOKEN` from the
  environment **at import time**, but `load_dotenv()` was originally
  called inside `main()`, which runs *after* `import config` already
  executed — so the token from `.env` was never actually picked up. Fixed
  by moving `load_dotenv()` to before the `config` import.

**Result** (from that run's console output — not persisted in any
committed file, only the final `data/eval_dataset.jsonl` is): scanned 212
closed issues, collected 80 clean examples, skipped 131 for not having a
single unambiguous merged-PR closer, 0 for empty title/body, 1 for
changed-file count outside `[1, 20]`.

### Phase 2 — Retrieval index (2026-07-21, commits `0441384`–`8c2e5ff`, PR #1)

Built and tested standalone at each sub-step in order: repo cloning →
AST-based chunking (verified with printed sample chunks) → local embedding
(verified 1449 chunks embedded to 384-dim vectors in ~27s on CPU) → Chroma
persistence (verified reuse-vs-rebuild behavior) → live test queries
against the built index (e.g. `"resize an image to a new width and
height"`, `"convert an image between color modes like RGB and CMYK"`,
both returning semantically relevant chunks). Chroma was chosen over FAISS
because it bundles a persistent client and simple metadata storage
together, avoiding hand-rolling a separate metadata layer alongside a raw
vector index.

### Phase 3 — Agent with tool use (2026-07-22, commits `4c1ff5c`–`61effd1`, PR #2)

Built the three tools standalone first, each verified against real data
before any LLM was involved — including a direct confirmation that
`grep_repo('PyDict_GetItemRef')` and `grep_repo('path_subscript')` located
exactly the real changed files (`src/encode.c`, `src/path.c`) for two
actual eval examples, before the agent loop existed to use them
automatically.

**Real, nontrivial problems hit while wiring the agentic loop, in the
order encountered:**

1. **Model selection turned into a genuine investigation, not a config
   default.** The newest model available at build time,
   `gemini-3.6-flash`, turned out to have a free tier capped at exactly
   **20 requests per day per project** — discovered only via the
   `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier` field
   inside an actual `429` response body; this number is not exposed by
   the `ListModels` endpoint or (as far as could be checked — web access
   was unavailable during the build) in reachable documentation. Falling
   back to `gemini-2.5-flash` (the model the project's original
   instructions suggested by name) returned `404`: *"This model
   models/gemini-2.5-flash is no longer available to new users."*
   `gemini-flash-latest` and `gemini-2.0-flash` were already
   429-quota-exhausted at the time they were probed. `gemini-3.5-flash`
   worked initially and passed early standalone tests — but was **later**
   discovered, once the eval harness started making a real volume of
   calls, to have the *same* 20/day cap. The model that actually stuck
   for the rest of the project, and that every real eval run in this
   document used, is **`gemini-3.5-flash-lite`** — available, and (as the
   Lite tier) evidently carries a meaningfully higher daily quota, since
   it survived multiple 73–80-example full-dataset runs on the free tier.
2. **The Gemini REST API rejected `role: "function"`** for the
   tool-response turn of the conversation, with the literal error *"Role
   'function' is not supported. Please use a valid role: SYSTEM,
   SYSTEM_1, USER, ASSISTANT, DEVELOPER, CONTEXT, USER_CONTEXT, MODEL,
   USER."* Fixed by sending `functionResponse` parts with `role: "user"`
   instead — found only by reading the actual error text, since this
   isn't the pattern shown in most function-calling examples.
3. **First live end-to-end loop test ran out of budget with nothing
   submitted** — the model spent its (then 10-call) budget investigating
   without ever calling `submit_predictions`. Fixed by forcing the last
   turn's `toolConfig` to `mode: "ANY"` restricted to
   `submit_predictions`, guaranteeing the loop always terminates with a
   real prediction (see 2.4).
4. **A raw `requests.exceptions.ReadTimeout` crashed a run** because
   `call_gemini`'s retry logic originally only caught HTTP-level status
   codes, not network-level exceptions. Fixed by also catching
   `requests.exceptions.RequestException`; also raised the per-request
   timeout from 60s to 120s, since the "thinking"-enabled model needs more
   headroom under load (confirmed via a direct timed test call — 6.0s for
   a trivial prompt, but multi-turn calls with accumulated context and
   real load took noticeably longer).
5. **The same `load_dotenv()`-ordering bug from Phase 1**, independently
   reintroduced in `agent.py` (its own `import config` also needs
   `GEMINI_API_KEY` at import time) — same fix, same root cause.

After those fixes, standalone verification against real eval examples
produced strong early signal — e.g. the `path_subscript` issue's agent run
predicted exactly `["src/path.c", "Tests/test_imagepath.py"]`, an exact
match to ground truth.

Manual review of that standalone output then surfaced two prompt-quality
issues, fixed in PR #3 (2026-07-22, commit `a15db36`): the model
over-investigating a wrong secondary file (fixed by lowering
`AGENT_MAX_TOOL_CALLS` 10→6) and reasoning text leaking visible
step-by-step thinking like *"let me check"* / *"or wait"* instead of a
clean final answer (fixed by tightening the `submit_predictions` schema
description and system prompt to demand exactly one decisive sentence).

### Phase 4 — Eval harness (2026-07-22, commits `99b1646`–`7459e95`, PR #4)

Built the scoring math and category logic **and verified both against
synthetic/fabricated data before ever calling the live agent** — zero API
cost for that verification pass (exact match, complete miss, partial hit,
and empty-prediction cases for `compute_metrics`; fabricated category rows
for `category_breakdown`/`detect_patterns`). Only after that logic was
confirmed correct did a live run happen.

Explicit design decision: separate `run_error` (infra failure, excluded
from scoring) from a genuinely empty/wrong prediction (a real, scoreable
outcome) — see 2.5.

First full 80-example run (**v1**): 75 scored, 5 excluded as `run_error`
(Gemini `429`s that survived all 5 retries). Result: precision 0.758,
recall 0.447, F1 0.512. Added the matplotlib chart and a first-draft
README Results section in the same PR.

### Phase 5 — Recall iteration, v1 → v2 (2026-07-23, commits `d9ab721`, `7d18315`, `700057d`)

Diagnosis, read directly off v1's `category_breakdown`: `complete_miss`
and `partial_hit` examples averaged far *more* ground-truth files (3.3–3.5)
than `full_hit` examples (1.1) — the agent wasn't guessing wrong so much
as stopping too early on genuinely multi-file changes.

Two targeted changes, each committed and tested standalone before moving
to the next:
1. `AGENT_SEMANTIC_SEARCH_TOP_K`: 8 → 20 (more candidate chunks per query
   = more surface area to spot a sibling file). Verified standalone: a
   sample query returned 20 results instead of 8, with visibly broader
   coverage across related files.
2. Added an explicit breadth-check paragraph to the system prompt (2.4),
   nudging the model to consider a sibling test file or C-level
   implementation file before finalizing — without adding to the turn
   budget. Verified standalone against 3 known multi-file examples that
   previously under-covered; all 3 improved (e.g. `path_subscript` recall
   0.5 → 1.0 by also finding `Tests/test_imagepath.py`).

Re-ran the full 80-example set (**v2**): 73 scored, 7 `run_error`. Result:
precision 0.740, recall 0.553, F1 0.591. Full numeric comparison and the
reasoning for the precision dip are in section 4. `results/eval_report.md`
and `results/eval_results.jsonl` were backed up as `*_v1.md`/`*_v1.jsonl`
*before* being overwritten by the v2 run, specifically so both versions
stay directly comparable.

### Phase 6 — Documentation (2026-07-23, commits `f52777a`, `da01f9e`, and this document)

Updated the README's Results section with v2 numbers and a before/after
Iteration table, then fully restructured `README.md` into a polished,
concise external-facing document (summary → results → architecture →
setup → limitations), replacing a stale "Phase 1 (current)" framing that
no longer matched the repo's actual state. This document is the
exhaustive internal counterpart, written after that restructure.

### A process note, not a code decision

Commits through PR #4 used short-lived feature branches merged via GitHub
(`gh pr create` + merge), mirroring a multi-contributor review workflow.
Since this is a single-contributor project, that added real sync friction
(branch-vs-`main` divergence needing rebases) without the corresponding
benefit of a second reviewer. From Phase 5 onward, the workflow switched
to committing directly to `main` — a deliberate scope-appropriate
simplification once that mismatch was recognized, not a limitation of the
project itself.

---

## 4. The evaluation methodology, in full detail

**Ground truth.** As in 2.2: an issue only enters the dataset if exactly
one merged PR closed it (per the `CLOSED_EVENT`/`closer` timeline data),
that PR's file list could be fully paginated (≤100 files), and the final
file count falls in `[1, 20]`. `changed_files` is that PR's file list,
taken as ground truth.

**Why precision, recall, *and* F1 (not just one).** This is a set
retrieval problem — predict a set of files, compare against a real set of
files — which is exactly what precision/recall/F1 measure jointly.
Precision alone would reward being overly conservative (predict a single
file, and if it's right, precision is a perfect 1.0 regardless of how many
other files were missed). Recall alone would reward predicting every
plausible file regardless of accuracy. Reporting all three separately (not
just a blended F1) answers two different practical questions: precision →
"when the agent names a file, can it be trusted?"; recall → "does the
agent find everything that would actually need touching?"

**Macro- vs. micro-averaging.** This project reports **macro-averages**:
precision/recall/F1 computed per example, then averaged across examples
(`sum(r[metric] for r in scored) / n` in `evaluate.summarize()` and
`category_breakdown()`) — not micro-averages (pooling every example's
tp/fp/fn counts first, then computing one global precision/recall/F1).
Macro-averaging means an issue with 10 ground-truth files doesn't
dominate the score more than an issue with 1 — every issue counts equally,
matching the actual goal of being good on the *average* issue rather than
doing well specifically on issues that happen to have many files.

**Category definitions — exact, from `evaluate.categorize()`:**
- **`complete_miss`**: `tp == 0` — none of the ground-truth files appear
  anywhere in the (top-5, deduped) predicted list.
- **`partial_hit`**: `0 < tp < len(actual_files)` — some but not all
  ground-truth files were found. By construction this bucket only applies
  to multi-file issues; a single-ground-truth-file issue can only be
  `complete_miss` or `full_hit`.
- **`full_hit`**: `tp == len(actual_files)` — every ground-truth file was
  found. Precision can still be below 1.0 here if extra, wrong files were
  also predicted alongside all the correct ones.

These three partition every scored example exactly (each falls into
precisely one).

**`run_error` exclusion.** An example whose agent call raised (Gemini
retries exhausted) is recorded separately and excluded from every
precision/recall/F1/category statistic — it's an infra failure, not a
prediction outcome. Reported instead as a separate `n_failed` count.

### v1 → v2, exact numbers

Pulled directly from `results/eval_report_v1.md` and `results/eval_report.md`:

| | v1 | v2 |
|---|---|---|
| Examples attempted | 80 | 80 |
| Scored | 75 | 73 |
| Failed to run (`run_error`) | 5 | 7 |
| Precision (macro-avg) | 0.758 | 0.740 |
| Recall (macro-avg) | 0.447 | 0.553 |
| F1 (macro-avg) | 0.512 | 0.591 |
| Avg. tool-call turns | 4.9 | 5.5 |
| Full hit — count / rate | 17 / 22.7% | 23 / 31.5% |
| Partial hit — count / rate | 45 / 60.0% | 40 / 54.8% |
| Complete miss — count / rate | 13 / 17.3% | 10 / 13.7% |
| `complete_miss` avg ground-truth files | 3.3 | 3.3 |
| `partial_hit` avg ground-truth files | 3.5 | 3.6 |
| `full_hit` avg ground-truth files | 1.1 | 1.4 |
| `complete_miss` P / R / F1 | 0.00 / 0.00 / 0.00 | 0.00 / 0.00 / 0.00 |
| `partial_hit` P / R / F1 | 0.93 / 0.37 / 0.51 | 0.84 / 0.43 / 0.55 |
| `full_hit` P / R / F1 | 0.87 / 1.00 / 0.91 | 0.89 / 1.00 / 0.93 |

**Why recall improved.** The two v2 changes directly targeted the v1
diagnosis (full-hit issues average far fewer ground-truth files than
miss/partial issues) — full-hit count rose 17 → 23 (six examples moved
from "found some correct files" to "found all of them"), and complete-miss
count fell 13 → 10.

**Why precision dipped slightly (0.758 → 0.740, −0.018 absolute / ≈−2.4%
relative).** Predicting more files per issue — both from more candidate
chunks being visible via the higher `top_k` and from the explicit
sibling-file nudge — mechanically increases the denominator of precision
(`tp / (tp + fp)`) every time an added guess turns out wrong. The category
breakdown shows exactly where this landed: `full_hit` precision actually
*rose* slightly (0.87 → 0.89), while `partial_hit` precision fell more
(0.93 → 0.84) — consistent with the extra breadth-driven guesses landing
disproportionately on issues where the agent found *some* but not *all*
files, where an extra sibling guess was more likely to be wrong than on
the already-easy full-hit issues. This is the standard precision/recall
trade-off, and the net F1 gain (0.512 → 0.591, +15.4% relative) indicates
it was a favorable one here.

**Turn count is not the driver.** Average turns only rose modestly (4.9 →
5.5; `AGENT_MAX_TOOL_CALLS` stayed at 6 throughout) — the recall gain is
attributable to better information per turn (higher `top_k`, better
prompted breadth-checking), not to spending more turns searching.

**A methodological caveat, stated plainly:** the same 80 examples were
used both to *diagnose* the v1 recall problem and to *measure* the v2
improvement. There is no held-out test set. This is closer to training-set
performance than to a held-out generalization measurement — appropriate
for demonstrating the iteration loop itself, but it means "recall improved
0.447 → 0.553" should be read as a real, directionally meaningful result
on *this* dataset, not evidence the same gain would reproduce on unseen
issues.

---

## 5. Every number that appears anywhere in the project

All pulled directly from `results/eval_report.md`, `results/eval_report_v1.md`,
`src/config.py`, git commit history, or (where noted) this session's
directly-observed console/API output not persisted to any file.

**Dataset (`data/eval_dataset.jsonl`):**
- 80 examples total.
- Mining run: 212 closed issues scanned, 131 skipped (no single
  unambiguous merged-PR closer), 0 skipped (empty title/body), 1 skipped
  (changed-file count outside `[1, 20]`). *(Console-output-only; not
  persisted in any committed file.)*
- Per-example changed-file counts: min 1, max 15; 18 single-file examples,
  62 multi-file examples; mean 2.9 files/example.
- Mining config: `TARGET_EXAMPLES = 80`, `MAX_ISSUES_SCANNED = 3000`,
  `ISSUES_PAGE_SIZE = 50`, `MIN_CHANGED_FILES = 1`, `MAX_CHANGED_FILES = 20`.

**Code index:**
- 1449 total chunks: 231 classes, 344 functions, 874 methods.
- 91 distinct Python files indexed (under `src/PIL`, out of the ~97 `.py`
  files present in that directory of the clone — the remainder had no
  top-level function/class definitions to chunk).
- Embedding model: `all-MiniLM-L6-v2`, 384 dimensions; ~27 seconds to
  embed all 1449 chunks on CPU (from the original build's verification).
- The persisted index (and underlying `.cache/repo` clone) is currently
  frozen at Pillow commit `b741b77` (2026-07-22, *"Handle premultiplied
  alpha modes in ImageColor.getcolor() (#9797)"*) — the last time
  `build_index.py` actually ran its clone/chunk/embed pipeline.

**Agent configuration:**
- `AGENT_SEMANTIC_SEARCH_TOP_K = 20` (raised from `8`).
- `AGENT_GREP_MAX_RESULTS = 20`.
- `AGENT_READ_FILE_MAX_LINES = 400`.
- `AGENT_MAX_TOOL_CALLS = 6` (lowered from `10`).
- `submit_predictions` capped at 5 files per issue.
- `call_gemini`: `max_retries = 5`, backoff from 2s doubling to a 60s cap,
  120s per-request timeout (raised from an original 60s).

**Gemini model/quota investigation (all directly observed via the live
API during this project, not documented anywhere by Google that was
reachable):**
- `gemini-3.6-flash` and `gemini-3.5-flash`: both showed
  `quotaValue: "20"` for `GenerateRequestsPerDayPerProjectPerModel-FreeTier`.
- `gemini-2.5-flash`, `gemini-2.5-flash-lite`: HTTP `404`, "no longer
  available to new users."
- `gemini-flash-latest`, `gemini-2.0-flash`, `gemini-2.0-flash-lite`:
  `429`-exhausted at time of testing.
- `gemini-3.5-flash-lite`, `gemini-flash-lite-latest`: HTTP `200`, not
  quota-exhausted — `gemini-3.5-flash-lite` is what every real eval run in
  this document used.

**Eval v1** (`results/eval_report_v1.md`, generated 2026-07-23 04:46 UTC):
- 80 attempted, 75 scored, 5 `run_error`.
- Precision 0.758, recall 0.447, F1 0.512, avg turns 4.9 (cap 6).
- `complete_miss`: 13 (17.3%). `partial_hit`: 45 (60.0%). `full_hit`: 17
  (22.7%).

**Eval v2** (`results/eval_report.md`, generated 2026-07-24 02:57 UTC —
note the README's copy states the same content; the header timestamp in
the committed report file itself reads this date):
- 80 attempted, 73 scored, 7 `run_error`.
- Precision 0.740, recall 0.553, F1 0.591, avg turns 5.5 (cap 6).
- `complete_miss`: 10 (13.7%). `partial_hit`: 40 (54.8%). `full_hit`: 23
  (31.5%).

**Build timeline:** first commit `1a85c34` on 2026-07-20; most recent
commit before this document, `da01f9e`, on 2026-07-23 — 4 calendar days,
27 commits, 4 GitHub-merged PRs (#1–#4, all before the switch to direct
`main` commits in Phase 5).

---

## 6. Limitations, honestly

Everything the README's Limitations section says, with the underlying
technical reason spelled out:

- **Single-repo dataset.** All 80 examples are Pillow issues. The tooling
  itself is repo-agnostic (`build_index.py`/`mine_dataset.py` both take
  `REPO_OWNER`/`REPO_NAME` from `config.py`), but no run against a second
  repo has ever happened — the specific numbers in this document (0.740
  precision / 0.553 recall) are Pillow-and-Pillow's-issue-writing-style-
  specific, not a validated general capability.

- **Recall is still the weaker metric, and still the larger bottleneck.**
  Even after v2, `partial_hit` (54.8% of scored examples — the single
  largest category) means *finding some but not all* correct files is
  still the modal outcome, larger than `full_hit` (31.5%). The
  `avg_actual_files` numbers in section 4 show this precisely: full-hit
  issues average 1.4 ground-truth files; miss/partial issues average
  3.3–3.6. Retrieval *breadth* on heavily multi-file changes remains the
  dominant failure mode, not precision or hallucination — the agent
  rarely predicts a file with zero evidence (partial-hit precision is
  0.84, not low), it just doesn't always find every relevant file.

- **Non-source ground-truth files are structurally invisible to the
  agent's tools**, not just occasionally missed. `build_index.py`'s
  `INDEX_INCLUDE_DIRS = ["src"]` plus `chunk_repository()`'s
  `.rglob("*.py")` means `semantic_search` can *never* surface a
  `docs/`, `Tests/*.png` fixture, `.github/workflows/*`,
  `wheels/dependency_licenses/*`, or `LICENSE` file — not a coverage gap
  that better prompting could fix, a structural one. `grep_repo` *could*
  technically find these (it searches the whole clone, any file type),
  but only if the agent thinks to search for the right literal text, and
  the system prompt never tells it these categories of files exist as
  candidates at all — so it's a blind spot in tool *guidance*, compounding
  the blind spot in index *coverage*. Concrete v2 `complete_miss`
  examples whose ground truth is entirely non-source: *"Missing TCL
  license in third-party licenses"* (ground truth:
  `wheels/dependency_licenses/TCL_TK.txt`), *"Reduce AVIF wheel size?"*
  (ground truth: 8 files, all CI/packaging/docs), *"Wheel: Loading
  zstd-compressed TIFF fails, even with imagecodecs"* (ground truth:
  `.github/workflows/wheels-dependencies.sh` and a license file — the
  agent predicted `src/PIL/TiffImagePlugin.py` instead, a reasonable
  guess from the issue *text* that was structurally unreachable as a
  correct answer).

- **API rate limits caused real data loss, not a deliberate exclusion
  policy.** 5 (v1) and 7 (v2) of 80 examples failed purely because
  Gemini's free tier was exhausted even after 5 retries with exponential
  backoff (up to 32s waits) — excluded from scoring as `run_error`, which
  is the methodologically correct call, but it means the reported metrics
  are implicitly conditional on "the API happened to be available for
  this example," a caveat a production system at real volume would need
  to actually solve (paid tier, slower/queued execution, or both) rather
  than route around statistically. Separately and more fundamentally: the
  discovery that *entire models* have a flat 20-requests/day cap is a wall
  retry/backoff cannot address at all — the actual fix there was picking
  a different model, not retrying harder.

- **No self-verification step.** `submit_predictions` is a blind commit —
  nothing checks whether the predicted files even relate to each other
  sensibly (e.g., does the predicted "test file" actually import/test the
  predicted "plugin file"), nothing runs Pillow's real test suite,
  nothing diffs against similar historical PRs as a sanity check. The
  6-turn budget leaves no room for that even if it were built. A
  production version of this would very likely need that feedback loop —
  bug localization that can check its own guesses before committing to
  them.

- **Small sample size, and no statistical significance testing was
  performed anywhere in this project.** 73–80 scored examples is not a
  large evaluation set. The v1 vs. v2 comparison is a real, data-backed
  before/after on the *same* fixed set of examples — but note that the
  count of scored examples itself varied between the two runs purely from
  which examples happened to hit API rate limits (75 vs. 73 out of the
  same 80 attempted), which is itself a small illustration of the
  run-to-run variance to expect from re-running this exact pipeline
  again.

- **The index is a frozen point-in-time snapshot, not continuously
  synced.** `build_index()` skips its entire clone/chunk/embed pipeline
  whenever the Chroma collection already has data (`collection.count() >
  0 and not rebuild`) — meaning `clone_repo()` isn't even called on a
  normal re-run. The repo clone and the index therefore stay consistent
  *with each other* (both frozen together), but both are stale relative
  to Pillow's actual current `HEAD` unless someone explicitly passes
  `--rebuild`. Right now that snapshot is pinned at commit `b741b77`
  (2026-07-22) — any Pillow change since then is invisible to every tool
  the agent has.

- **Agent tool-call traces aren't persisted.** `agent.localize_issue()`
  builds a full `trace` list (every tool call, its arguments, and its
  result) internally, but `evaluate.run_example()` never includes it in
  the dict written to `results/eval_results.jsonl` — it's discarded after
  each run. Retrospective failure analysis (e.g. understanding exactly
  *why* the agent chased `TgaRleDecode.c` in one early test) is currently
  only possible by rereading captured console output from when a run
  happened to be run with `verbose=True`, not by querying stored data.

---

## 7. Anticipated interview questions with real answers

**"Why no frontend?"**
This is a measurement project, not a product demo — the deliverable is a
reproducible pipeline (mine → index → agent → score) with real numbers,
not a UI. Wrapping `agent.localize_issue()` behind a web frontend would
add auth/hosting surface area that doesn't help answer the actual question
under investigation ("can a tool-using LLM agent localize issues to files,
and how well"). If this were productized, the natural integration point is
a CLI or a GitHub Action/bot that comments predicted files on a new issue
— living inside the existing GitHub workflow the way Copilot/CodeRabbit
do — not a standalone web app.

**"Why did you choose this problem?"**
It has a genuinely free, unambiguous ground truth: any closed issue that
names the PR that fixed it gives you a labeled example with zero manual
annotation. That property is what made it possible to build *and actually
measure* every stage of this pipeline rather than just demo it. It's also
a good vehicle to show a full slice of agent engineering in one project:
real-API data mining with rate-limit handling, retrieval infrastructure,
tool-using LLM orchestration with genuinely necessary retry/error
handling, and a rigorous, category-broken-down eval harness — each piece
independently built and tested.

**"Walk me through a failure case."**
Take v2 example index 42, *"Wheel: Loading zstd-compressed TIFF fails,
even with imagecodecs."* Ground truth is
`.github/workflows/wheels-dependencies.sh` and
`wheels/dependency_licenses/ZSTD.txt` — both non-Python packaging/CI
files; the real bug was in how a CI wheel-build script handled a zstd
dependency, nothing to do with Python decode logic at all. The agent
predicted `src/PIL/TiffImagePlugin.py` — a completely reasonable read of
the issue *text* (it's nominally about TIFF loading), but structurally
unreachable as a correct answer: `semantic_search` never indexes non-Python
files, and nothing in the issue text gave `grep_repo` an obvious literal
string (like "zstd" tied to a specific symbol) to search for in that CI
script. That's the "non-source ground truth is invisible to the tools"
limitation from section 6, concretely.

**"Why did precision drop after your recall fix?"**
Predicting more files per issue mechanically grows the denominator of
precision (`tp / (tp+fp)`) every time one of those extra guesses is wrong.
The category data shows exactly where: `full_hit` precision actually rose
slightly (0.87 → 0.89), while `partial_hit` precision dropped more (0.93 →
0.84) — the extra breadth-driven guesses concentrated on issues where the
agent was already finding *some* but not *all* files, where an extra
sibling guess had a real chance of being wrong. Net F1 still rose 0.512 →
0.591, so it reads as a favorable trade, but it's a genuine trade, not a
free lunch.

**"What would you do differently?"**
1. Give the agent visibility into non-Python file *paths* even without
   embedding their content — a repo file-tree/glob tool would have caught
   several of the concrete `complete_miss` cases in section 6.
2. Run this same harness against a second, structurally different repo
   before trusting these numbers as anything beyond Pillow-specific — no
   generalization claim can currently be made.
3. Smoke-test a candidate Gemini model's quota with a single cheap call
   *before* committing to it for real work, rather than discovering a
   20/day wall mid-run — the model-selection saga in section 3 cost real
   build time that a five-second probe upfront would have saved.
4. Persist `agent.localize_issue()`'s full tool-call trace in
   `evaluate.py`'s results, not just the final prediction — currently
   thrown away, which makes retrospective "why did it guess that" analysis
   harder than it needs to be.
5. Consider a cheaper/faster model for exploratory tool-selection turns
   and reserve more capability for the final `submit_predictions` turn,
   since those two steps arguably need different capability levels.

**"How does this compare to tools like Copilot's PR review or CodeRabbit?"**
Those operate on an *existing diff* — files have already been identified
and changed by a human or another process, and the tool comments on
whether those specific changes look correct. This project answers an
earlier, different question: given *only* an issue report, before any code
has been written, which files *would* need to change. That's
localization/retrieval, not review. The closer analogy is an IR-based or
learned bug-localization/triage tool, or the "which files are relevant"
step that would need to run *before* an automated-program-repair or
AI-pair-programmer's actual patch-generation step. This project could
plausibly feed into a Copilot-Workspace-style "here's a proposed fix" flow
as that first step — but it stops at finding the files; it never attempts
to write a patch.

**"What was the hardest part?"**
Honestly, not the retrieval/agent engineering — it was that the Gemini
free-tier API surface was genuinely undocumented at the exact
model/quota level needed to make a reliable choice. `ListModels` lists
models but not their quotas or deprecation status; quota numbers only ever
appeared inside an actual `429` error body, meaning the only way to know
if a model was viable was to spend a real request against it and read
what came back. That turned model selection into empirical trial-and-error
(section 3) rather than a five-minute docs lookup — and it recurred
*twice*: once during initial agent development, and again once the eval
harness's higher call volume hit `gemini-3.5-flash`'s daily cap that
lighter manual testing hadn't triggered yet.

**"How do you know your eval numbers aren't just noise or overfit to your
own prompt tuning?"**
I don't have full confidence they aren't, and I'd say so directly: the
same 80 examples were used both to diagnose the v1 recall problem and to
measure the v2 improvement — there's no held-out test set. That's closer
to training-set performance than a genuinely held-out generalization
result. It's an appropriate methodology for demonstrating *that the
iteration loop works* (diagnose from data → make a targeted change →
re-measure), which is what this phase of the project was actually testing
— but it would be inaccurate to present 0.740/0.553/0.591 as numbers that
would necessarily hold on a fresh, unseen set of Pillow issues, let alone
another repo.

**"Why not fine-tune a model instead of prompting/tool use?"**
There's no labeled training signal beyond the 80 eval examples themselves
— nowhere near enough for fine-tuning, and using them for training would
also destroy the only evaluation set available. More fundamentally, the
actual thesis being tested here is whether a general-purpose LLM *with
search tools and no repo-specific training* can do this task — that's the
retrieval-augmented-agent premise, not a fine-tuning experiment. If it
didn't work at all without fine-tuning, that itself would be an
interesting (negative) result; it works reasonably well, which is the more
interesting finding to report.

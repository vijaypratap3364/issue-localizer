"""
Issue-localizing agent (Phase 3).

Given a GitHub issue title + body, predicts which repo files most likely
need to change to fix it. The agent has three tools:
  - semantic_search: query the local Chroma code index (see build_index.py)
  - grep_repo:       exact-match search across the full cloned repo
  - read_file:       read a full file (or line range) when a chunk looks
                      promising but incomplete

A Gemini Flash model drives the reasoning/tool-calling loop and produces a
final ranked list of predicted files with short reasoning for each.

Usage (standalone, once tools/loop are wired up):
    python src/agent.py
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # must happen before `import config`, which reads GEMINI_API_KEY at import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_index  # noqa: E402
import config  # noqa: E402


def semantic_search(query, top_k=None, collection=None, model=None):
    """Search the local code index for chunks semantically related to
    `query`. Returns a list of dicts: file, qualified_name, type,
    start_line, end_line, source, distance (lower = more similar)."""
    top_k = top_k or config.AGENT_SEMANTIC_SEARCH_TOP_K
    collection = collection or build_index.get_chroma_collection()
    model = model or build_index.load_embedding_model()

    raw = build_index.query_index(collection, query, model=model, top_k=top_k)

    results = []
    for id_, meta, dist, doc in zip(
        raw["ids"][0], raw["metadatas"][0], raw["distances"][0], raw["documents"][0]
    ):
        results.append(
            {
                "file": meta["file"],
                "qualified_name": meta["qualified_name"],
                "type": meta["type"],
                "start_line": meta["start_line"],
                "end_line": meta["end_line"],
                "source": doc,
                "distance": dist,
            }
        )
    return results


def grep_repo(pattern, max_results=None, regex=False):
    """Exact (or regex) search for `pattern` across the *entire* cloned
    repo -- unlike semantic_search this also covers non-Python files (e.g.
    Pillow's C sources), and is the reliable way to find exact symbol
    names the issue text mentions.

    Returns a list of dicts: file, line, text. Uses `git grep`, which is
    fast and (since the repo cache is a git clone) always available.
    """
    max_results = max_results or config.AGENT_GREP_MAX_RESULTS
    repo_dir = Path(config.REPO_CACHE_DIR)
    if not (repo_dir / ".git").exists():
        raise RuntimeError(
            f"No repo cloned at {repo_dir} -- run `python src/build_index.py` first."
        )
    if not pattern or not pattern.strip():
        return []

    cmd = ["git", "grep", "-n", "-I", "--no-color"]
    cmd.append("-E" if regex else "-F")
    cmd.append(pattern)

    proc = subprocess.run(
        cmd, cwd=repo_dir, capture_output=True, text=True, errors="ignore"
    )

    if proc.returncode == 1:
        return []  # git grep: no matches (not an error)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed (exit {proc.returncode}): {proc.stderr.strip()}")

    results = []
    for line in proc.stdout.splitlines():
        # format: path:line_number:matched_text
        file_path, _, rest = line.partition(":")
        line_no, _, text = rest.partition(":")
        if not line_no.isdigit():
            continue
        results.append({"file": file_path, "line": int(line_no), "text": text})
        if len(results) >= max_results:
            break

    return results


def read_file(path, start_line=None, end_line=None, max_lines=None):
    """Read a file (or a line range of one) from the cloned repo. Use this
    when a semantic_search/grep_repo hit looks promising but you need more
    surrounding context than a single chunk gives you.

    `path` must be a repo-relative path (e.g. "src/PIL/Image.py"); paths
    that resolve outside the repo cache (via ".." or an absolute path) are
    rejected -- this tool only ever reads files from the cloned repo.
    """
    max_lines = max_lines or config.AGENT_READ_FILE_MAX_LINES
    repo_dir = Path(config.REPO_CACHE_DIR).resolve()
    if not (repo_dir / ".git").exists():
        raise RuntimeError(
            f"No repo cloned at {repo_dir} -- run `python src/build_index.py` first."
        )

    target = (repo_dir / path).resolve()
    try:
        target.relative_to(repo_dir)
    except ValueError:
        return {"file": path, "error": f"Path {path!r} is outside the repo; refusing to read it."}

    if not target.is_file():
        return {"file": path, "error": f"File not found: {path}"}

    lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    total_lines = len(lines)
    start = max(1, start_line or 1)
    end = min(end_line or total_lines, total_lines)
    truncated = False
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
        truncated = True

    return {
        "file": path,
        "start_line": start,
        "end_line": end,
        "total_lines": total_lines,
        "truncated": truncated,
        "content": "\n".join(lines[start - 1 : end]),
    }


def call_gemini(contents, tools=None, tool_config=None, system_instruction=None, max_retries=5):
    """POST to the Gemini generateContent endpoint, retrying with backoff on
    429 (rate limit), 5xx responses, and network-level errors (timeouts,
    connection resets). Raises clearly rather than hanging once retries are
    exhausted."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env -- see .env.example."
        )

    url = f"{config.GEMINI_API_BASE}/models/{config.GEMINI_MODEL_NAME}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.GEMINI_API_KEY,
    }
    body = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if tools:
        body["tools"] = tools
    if tool_config:
        body["toolConfig"] = tool_config

    backoff = 2
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.exceptions.RequestException as e:
            # Network-level failures (timeouts, connection resets, ...) are
            # just as retryable as a 5xx -- don't let them crash the loop.
            print(
                f"  Gemini network error ({e.__class__.__name__}). Retrying in "
                f"{backoff}s... (attempt {attempt}/{max_retries})"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            last_error = f"network error: {e}"
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = int(retry_after) if retry_after else backoff
            print(
                f"  Gemini rate limited (429). Retrying in {wait}s... "
                f"(attempt {attempt}/{max_retries})"
            )
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            last_error = f"429: {resp.text[:300]}"
            continue

        if resp.status_code >= 500:
            print(
                f"  Gemini server error {resp.status_code}. Retrying in {backoff}s... "
                f"(attempt {attempt}/{max_retries})"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            last_error = f"{resp.status_code}: {resp.text[:300]}"
            continue

        # Non-retryable (400 bad request, 401/403 auth, 404 unknown model, ...)
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")

    raise RuntimeError(
        f"Gemini API: exceeded {max_retries} retries due to rate limiting/server "
        f"errors. Last error: {last_error}"
    )


def generate_text(prompt, system_instruction=None):
    """Simple single-turn helper (no tools) -- mainly for standalone testing
    of the Gemini client itself."""
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    data = call_gemini(contents, system_instruction=system_instruction)
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if "text" in p)


SEMANTIC_SEARCH_DECL = {
    "name": "semantic_search",
    "description": (
        "Semantically search the repo's indexed Python source for code related "
        "to a natural-language description. Good for finding relevant "
        "functions/classes/methods by meaning. Does NOT cover non-Python files "
        "(e.g. C sources) -- use grep_repo for those."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Natural-language description of the code you're looking for.",
            },
            "top_k": {
                "type": "INTEGER",
                "description": "Number of results to return (default 8).",
            },
        },
        "required": ["query"],
    },
}

GREP_REPO_DECL = {
    "name": "grep_repo",
    "description": (
        "Exact (or regex) text search across the ENTIRE cloned repo, including "
        "non-Python files like C sources. Use this to find exact function/symbol/"
        "error-message names mentioned in the issue text -- semantic_search only "
        "covers Python source."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "pattern": {
                "type": "STRING",
                "description": "Literal text (or regex if regex=true) to search for.",
            },
            "regex": {
                "type": "BOOLEAN",
                "description": "Treat pattern as a regex instead of a literal string. Default false.",
            },
        },
        "required": ["pattern"],
    },
}

READ_FILE_DECL = {
    "name": "read_file",
    "description": (
        "Read a file (optionally a specific line range) from the cloned repo. "
        "Use this when a search hit looks relevant but you need more "
        "surrounding context before deciding."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Repo-relative file path, e.g. 'src/PIL/Image.py'.",
            },
            "start_line": {"type": "INTEGER", "description": "1-indexed start line (optional)."},
            "end_line": {"type": "INTEGER", "description": "1-indexed end line, inclusive (optional)."},
        },
        "required": ["path"],
    },
}

SUBMIT_PREDICTIONS_DECL = {
    "name": "submit_predictions",
    "description": (
        "Submit your FINAL ranked list of predicted files that most likely need "
        "to change to fix the issue. Call this exactly once, when you're done "
        "investigating -- not before you've used at least one search tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "predictions": {
                "type": "ARRAY",
                "description": "Ranked list of predicted files, most likely first (at most 5).",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "file": {"type": "STRING", "description": "Repo-relative file path."},
                        "reasoning": {
                            "type": "STRING",
                            "description": (
                                "EXACTLY ONE decisive sentence justifying this file, stating "
                                "your conclusion directly. Grounded in tool results, e.g. name "
                                "the specific function/symbol you found. Not your thought "
                                "process: no hedging, no 'let me check', no 'or wait', no "
                                "narrating what you're about to do -- state the conclusion only."
                            ),
                        },
                    },
                    "required": ["file", "reasoning"],
                },
            },
        },
        "required": ["predictions"],
    },
}

AGENT_TOOLS = [
    {
        "functionDeclarations": [
            SEMANTIC_SEARCH_DECL,
            GREP_REPO_DECL,
            READ_FILE_DECL,
            SUBMIT_PREDICTIONS_DECL,
        ]
    }
]

def _build_system_instruction(max_tool_calls):
    return f"""You are a code-search agent for the {config.REPO_OWNER}/{config.REPO_NAME} \
GitHub repo. Given a GitHub issue's title and body, find which files in the \
repo most likely need to change to fix it.

You have three investigation tools:
- semantic_search: find Python code related to the issue by meaning
- grep_repo: find exact symbol/function/error-message text anywhere in the \
repo, including non-Python (e.g. C) files that semantic_search can't reach
- read_file: read more surrounding context around a promising hit

Start with semantic_search. If the issue text names specific functions, \
classes, error messages, or identifiers, also use grep_repo -- it's often \
the only way to find hits in non-Python source. Use read_file when a hit \
looks relevant but you need to see more of it before deciding.

You have a HARD BUDGET of {max_tool_calls} tool calls total, across all \
tools -- plan around it. A typical good investigation uses only 3-5: e.g. \
one semantic_search, one grep_repo for a key identifier, maybe one \
read_file for confirmation. Don't spend calls re-confirming something \
you're already confident about.

As soon as you have enough evidence, call submit_predictions EXACTLY ONCE \
with a ranked list (most likely first, at most 5 files) of repo-relative \
file paths. Don't guess at files you have no evidence for, and don't wait \
until you've exhausted your budget to submit.

Each file's reasoning must be exactly one decisive sentence stating your \
conclusion, grounded in what your tools actually showed you (e.g. name the \
specific function/symbol). Never write out your thinking -- no "let me \
check", no "or wait", no narrating what you're about to look at. Reach the \
conclusion first, then write only that."""


def _execute_tool_call(name, args, collection, model):
    if name == "semantic_search":
        return semantic_search(
            args["query"], top_k=args.get("top_k"), collection=collection, model=model
        )
    if name == "grep_repo":
        return grep_repo(args["pattern"], regex=args.get("regex", False))
    if name == "read_file":
        return read_file(
            args["path"], start_line=args.get("start_line"), end_line=args.get("end_line")
        )
    return {"error": f"Unknown tool: {name}"}


def localize_issue(issue_title, issue_body, collection=None, model=None, max_tool_calls=None, verbose=True):
    """Run the full agent loop for one issue. Returns a dict:
    {predictions: [{file, reasoning}, ...], trace: [...], turns: int}
    (predictions is [] and an "error" key is set if the model never called
    submit_predictions within max_tool_calls turns)."""
    max_tool_calls = max_tool_calls or config.AGENT_MAX_TOOL_CALLS
    collection = collection or build_index.get_chroma_collection()
    model = model or build_index.load_embedding_model()

    user_prompt = f"Issue title: {issue_title}\n\nIssue body:\n{issue_body}"
    contents = [{"role": "user", "parts": [{"text": user_prompt}]}]
    system_instruction = _build_system_instruction(max_tool_calls)
    trace = []

    for turn in range(max_tool_calls):
        # On the last available turn, force a submission instead of letting
        # the model keep investigating and blowing the budget with nothing
        # to show for it.
        tool_config = None
        if turn == max_tool_calls - 1:
            tool_config = {
                "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["submit_predictions"]}
            }

        data = call_gemini(
            contents,
            tools=AGENT_TOOLS,
            tool_config=tool_config,
            system_instruction=system_instruction,
        )

        if "candidates" not in data or not data["candidates"]:
            feedback = data.get("promptFeedback", {})
            raise RuntimeError(f"Gemini returned no candidates: {feedback}")

        parts = data["candidates"][0]["content"]["parts"]
        contents.append({"role": "model", "parts": parts})

        function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not function_calls:
            text = "".join(p.get("text", "") for p in parts)
            if verbose:
                print(f"  [turn {turn}] model replied with text, no tool call: {text[:200]!r}")
            trace.append({"turn": turn, "type": "text_no_call", "text": text})
            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": "Please call submit_predictions now with your ranked file list."}],
                }
            )
            continue

        response_parts = []
        submitted = None
        for fc in function_calls:
            name, args = fc["name"], fc.get("args", {})
            if verbose:
                print(f"  [turn {turn}] tool call: {name}({args})")

            if name == "submit_predictions":
                submitted = args.get("predictions", [])
                response_parts.append(
                    {"functionResponse": {"name": name, "response": {"status": "received"}}}
                )
                continue

            try:
                result = _execute_tool_call(name, args, collection, model)
            except Exception as e:
                result = {"error": str(e)}

            trace.append({"turn": turn, "type": "tool_call", "name": name, "args": args, "result": result})
            response_parts.append({"functionResponse": {"name": name, "response": {"result": result}}})

        # The API rejects a "function" role for functionResponse parts (only
        # USER/MODEL etc. are accepted) -- "user" is the correct role here.
        contents.append({"role": "user", "parts": response_parts})

        if submitted is not None:
            return {"predictions": submitted, "trace": trace, "turns": turn + 1}

    return {
        "predictions": [],
        "trace": trace,
        "turns": max_tool_calls,
        "error": f"Exceeded max_tool_calls ({max_tool_calls}) without submit_predictions.",
    }


def load_eval_examples(indices=None, path=None):
    """Load examples from data/eval_dataset.jsonl. `indices` selects specific
    (0-based) lines; defaults to a small, diverse hand-picked sample."""
    path = path or config.OUTPUT_PATH
    if indices is None:
        indices = (0, 2, 18)  # mixed Python+C, C-only, pure-Python -- see below

    with open(path, encoding="utf-8") as f:
        all_examples = [json.loads(line) for line in f]

    return [all_examples[i] for i in indices]


def run_demo(indices=None, max_tool_calls=None):
    """Run the agent standalone against a handful of real eval examples and
    print predictions + reasoning next to the real ground-truth
    changed_files, for manual sanity-checking. Not scored -- that's the
    eval harness's job, which doesn't exist yet."""
    collection = build_index.get_chroma_collection()
    if collection.count() == 0:
        print("Index is empty -- run `python src/build_index.py` first.")
        sys.exit(1)
    model = build_index.load_embedding_model()

    examples = load_eval_examples(indices)
    for i, example in enumerate(examples, start=1):
        print("=" * 70)
        print(f"[{i}/{len(examples)}] {example['issue_title']}")
        print("-" * 70)

        result = localize_issue(
            example["issue_title"],
            example["issue_body"],
            collection=collection,
            model=model,
            max_tool_calls=max_tool_calls,
        )

        print(f"\nturns used: {result['turns']}")
        if result.get("error"):
            print(f"ERROR: {result['error']}")

        print("\npredicted:")
        for rank, p in enumerate(result["predictions"], start=1):
            print(f"  {rank}. {p['file']}")
            print(f"     {p['reasoning']}")

        print("\nactual changed_files (ground truth, not shown to the agent):")
        for f in example["changed_files"]:
            hit = "*" if f in {p["file"] for p in result["predictions"]} else " "
            print(f"  [{hit}] {f}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--indices",
        type=int,
        nargs="+",
        default=None,
        help="0-based line indices into data/eval_dataset.jsonl to run (default: a curated diverse sample of 3).",
    )
    parser.add_argument("--max-tool-calls", type=int, default=None)
    args = parser.parse_args()

    run_demo(indices=args.indices, max_tool_calls=args.max_tool_calls)

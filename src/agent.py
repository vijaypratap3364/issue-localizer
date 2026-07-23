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


def _print_read_file_result(path, result, **kwargs):
    print(f"\nread_file({path!r}, {kwargs}) ->")
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return
    print(
        f"  lines {result['start_line']}-{result['end_line']} of "
        f"{result['total_lines']} (truncated={result['truncated']})"
    )
    print("  " + result["content"].splitlines()[0][:100])


def _print_grep_results(pattern, results):
    print(f"\ngrep_repo({pattern!r}) -> {len(results)} results")
    for r in results:
        print(f"  {r['file']}:{r['line']}: {r['text'].strip()[:100]}")


def _print_semantic_search_results(query, results):
    print(f"\nsemantic_search({query!r}) -> {len(results)} results")
    for rank, r in enumerate(results, start=1):
        print(
            f"  {rank}. {r['file']} :: {r['qualified_name']} "
            f"({r['type']}, lines {r['start_line']}-{r['end_line']}) "
            f"[distance={r['distance']:.3f}]"
        )


def call_gemini(contents, tools=None, tool_config=None, system_instruction=None, max_retries=5):
    """POST to the Gemini generateContent endpoint, retrying with backoff on
    429 (rate limit) and 5xx responses. Raises clearly rather than hanging
    once retries are exhausted."""
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
        resp = requests.post(url, headers=headers, json=body, timeout=60)

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


if __name__ == "__main__":
    collection = build_index.get_chroma_collection()
    if collection.count() == 0:
        print("Index is empty -- run `python src/build_index.py` first.")
        sys.exit(1)

    model = build_index.load_embedding_model()
    for query in [
        "cur file saved as png has wrong transparency/alpha",
        "reference leak PyDict_GetItemRef not decref'd",
    ]:
        results = semantic_search(query, collection=collection, model=model)
        _print_semantic_search_results(query, results)

    for pattern in ["PyDict_GetItemRef", "path_subscript", "DoesNotExistXYZ123"]:
        results = grep_repo(pattern)
        _print_grep_results(pattern, results)

    r = read_file("src/path.c", start_line=590, end_line=610)
    _print_read_file_result("src/path.c", r, start_line=590, end_line=610)

    r = read_file("../../../../etc/passwd")
    _print_read_file_result("../../../../etc/passwd", r)

    r = read_file("src/does_not_exist.py")
    _print_read_file_result("src/does_not_exist.py", r)

    print("\ncalling Gemini ({})...".format(config.GEMINI_MODEL_NAME))
    text = generate_text("Reply with exactly the two words: hello world")
    print(f"  response: {text!r}")

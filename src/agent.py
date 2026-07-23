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
from pathlib import Path

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

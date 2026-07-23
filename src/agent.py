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

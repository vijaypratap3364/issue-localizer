"""
Eval harness (Phase 4). Runs the agent (src/agent.py) against every example
in data/eval_dataset.jsonl and scores its predicted files against the real
ground-truth changed_files, computing precision/recall/F1 per example and
averaged overall, plus tool-call turns per example.

Does not modify agent.py or build_index.py -- this only measures what's
already there.

Usage:
    python src/evaluate.py                  # full dataset, resumable
    python src/evaluate.py --limit 5         # quick test on first 5 examples
    python src/evaluate.py --indices 0 2 18  # specific examples
    python src/evaluate.py --resume          # continue a partial run
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402
import build_index  # noqa: E402
import config  # noqa: E402

RESULTS_PATH = Path(config.PROJECT_ROOT) / "results" / "eval_results.jsonl"


def load_all_examples(path=None):
    path = path or config.OUTPUT_PATH
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def compute_metrics(predicted_files, actual_files):
    """Precision/recall/F1 for one example. predicted_files is the ranked
    list of predicted paths (already capped/deduped by the caller);
    actual_files is the ground-truth changed_files list."""
    predicted_set = set(predicted_files)
    actual_set = set(actual_files)
    tp = len(predicted_set & actual_set)
    fp = len(predicted_set - actual_set)
    fn = len(actual_set - predicted_set)

    precision = tp / len(predicted_set) if predicted_set else 0.0
    recall = tp / len(actual_set) if actual_set else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def _dedupe_top5(predictions):
    """predictions is agent.localize_issue()'s ranked [{file, reasoning}, ...].
    Return the top-5 file paths, order preserved, duplicates dropped."""
    seen = set()
    files = []
    for p in predictions[:5]:
        f = p["file"]
        if f not in seen:
            seen.add(f)
            files.append(f)
    return files


def run_example(example, index, collection, model, max_tool_calls=None):
    """Run the agent on one eval example and score it. Returns a result dict.
    If the agent itself raises (API error exhausted its retries), that's
    recorded as `run_error` and NOT scored -- it's an infra failure, not a
    real prediction outcome."""
    t0 = time.time()
    try:
        result = agent.localize_issue(
            example["issue_title"],
            example["issue_body"],
            collection=collection,
            model=model,
            max_tool_calls=max_tool_calls,
            verbose=False,
        )
    except Exception as e:
        return {
            "index": index,
            "issue_title": example["issue_title"],
            "issue_body": example["issue_body"],
            "actual_files": example["changed_files"],
            "predicted_files": [],
            "turns": None,
            "run_error": str(e),
            "wall_seconds": round(time.time() - t0, 1),
        }

    predicted_files = _dedupe_top5(result["predictions"])
    metrics = compute_metrics(predicted_files, example["changed_files"])
    category = categorize(predicted_files, example["changed_files"])

    return {
        "index": index,
        "issue_title": example["issue_title"],
        "issue_body": example["issue_body"],
        "actual_files": example["changed_files"],
        "predicted_files": predicted_files,
        "reasoning": {p["file"]: p["reasoning"] for p in result["predictions"][:5]},
        "turns": result["turns"],
        "category": category,
        "agent_error": result.get("error"),
        "run_error": None,
        "wall_seconds": round(time.time() - t0, 1),
        **metrics,
    }


def categorize(predicted_files, actual_files):
    """Partition every scored example into exactly one bucket:
      - complete_miss: none of the actual files were predicted (recall = 0)
      - partial_hit:   actual has >1 file and only some were predicted
      - full_hit:      every actual file was predicted (recall = 1)
    """
    predicted_set = set(predicted_files)
    actual_set = set(actual_files)
    tp = len(predicted_set & actual_set)

    if tp == 0:
        return "complete_miss"
    if tp < len(actual_set):
        return "partial_hit"
    return "full_hit"


CATEGORIES = ("complete_miss", "partial_hit", "full_hit")


def category_breakdown(scored):
    """Per-category example count and macro-averaged precision/recall/F1/turns."""
    breakdown = {}
    for cat in CATEGORIES:
        rows = [r for r in scored if r["category"] == cat]
        n = len(rows)
        if n == 0:
            breakdown[cat] = {"n": 0}
            continue
        breakdown[cat] = {
            "n": n,
            "precision": sum(r["precision"] for r in rows) / n,
            "recall": sum(r["recall"] for r in rows) / n,
            "f1": sum(r["f1"] for r in rows) / n,
            "avg_turns": sum(r["turns"] for r in rows) / n,
            "avg_body_len": sum(len(r["issue_body"]) for r in rows) / n,
            "avg_actual_files": sum(len(r["actual_files"]) for r in rows) / n,
        }
    return breakdown


def detect_patterns(scored):
    """Compute a few data-driven signals comparing complete_miss/partial_hit
    against full_hit, to surface in the report's "observed patterns"
    section rather than relying on unverified hand-wavy commentary."""
    breakdown = category_breakdown(scored)
    patterns = []

    miss = breakdown.get("complete_miss", {})
    hit = breakdown.get("full_hit", {})

    if miss.get("n") and hit.get("n"):
        if miss["avg_body_len"] < 0.7 * hit["avg_body_len"]:
            patterns.append(
                f"Complete misses have noticeably shorter issue bodies on average "
                f"({miss['avg_body_len']:.0f} chars vs {hit['avg_body_len']:.0f} for full hits) "
                f"-- vague/short issue text may be harder to localize from."
            )
        if miss["avg_actual_files"] > 1.3 * hit["avg_actual_files"]:
            patterns.append(
                f"Complete misses involve more ground-truth files on average "
                f"({miss['avg_actual_files']:.1f} vs {hit['avg_actual_files']:.1f} for full hits) "
                f"-- cross-file changes are harder to fully localize."
            )
        if miss["avg_turns"] > hit["avg_turns"]:
            patterns.append(
                f"Complete misses use more tool-call turns on average "
                f"({miss['avg_turns']:.1f} vs {hit['avg_turns']:.1f} for full hits) -- "
                f"more investigation doesn't correlate with a correct answer here; "
                f"the agent isn't running out of budget, it's running out of leads."
            )
        else:
            patterns.append(
                f"Complete misses use FEWER tool-call turns on average "
                f"({miss['avg_turns']:.1f} vs {hit['avg_turns']:.1f} for full hits) -- "
                f"the agent may be giving up/submitting too early on these rather than "
                f"investigating further."
            )

    partial = breakdown.get("partial_hit", {})
    if partial.get("n") and hit.get("n") and partial["avg_actual_files"] > hit["avg_actual_files"]:
        patterns.append(
            f"Partial hits involve more ground-truth files on average "
            f"({partial['avg_actual_files']:.1f} vs {hit['avg_actual_files']:.1f} for full hits) "
            f"-- as expected, finding *some but not all* changed files is a multi-file-change problem."
        )

    return patterns


def summarize(results):
    """Macro-averaged precision/recall/F1 and average turns over the
    successfully-scored examples (run_error examples are excluded -- they're
    infra failures, not prediction outcomes)."""
    scored = [r for r in results if r["run_error"] is None]
    failed = [r for r in results if r["run_error"] is not None]

    n = len(scored)
    if n == 0:
        return {"n_scored": 0, "n_failed": len(failed)}

    return {
        "n_scored": n,
        "n_failed": len(failed),
        "precision": sum(r["precision"] for r in scored) / n,
        "recall": sum(r["recall"] for r in scored) / n,
        "f1": sum(r["f1"] for r in scored) / n,
        "avg_turns": sum(r["turns"] for r in scored) / n,
        "category_breakdown": category_breakdown(scored),
        "patterns": detect_patterns(scored),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N examples.")
    parser.add_argument("--indices", type=int, nargs="+", default=None, help="Run only these 0-based indices.")
    parser.add_argument("--resume", action="store_true", help="Skip examples already present in the results file.")
    parser.add_argument("--output", type=str, default=None, help="Results JSONL path (default: results/eval_results.jsonl).")
    parser.add_argument("--max-tool-calls", type=int, default=None)
    args = parser.parse_args()

    results_path = Path(args.output) if args.output else RESULTS_PATH
    results_path.parent.mkdir(parents=True, exist_ok=True)

    all_examples = load_all_examples()
    indexed = list(enumerate(all_examples))
    if args.indices is not None:
        wanted = set(args.indices)
        indexed = [(i, e) for i, e in indexed if i in wanted]
    elif args.limit is not None:
        indexed = indexed[: args.limit]

    existing = {}
    if args.resume and results_path.exists():
        with open(results_path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                existing[r["index"]] = r

    collection = build_index.get_chroma_collection()
    if collection.count() == 0:
        print("Index is empty -- run `python src/build_index.py` first.")
        sys.exit(1)
    model = build_index.load_embedding_model()

    results = []
    mode = "a" if args.resume else "w"
    consecutive_run_errors = 0

    with open(results_path, mode, encoding="utf-8") as out_f:
        for n, (i, example) in enumerate(indexed, start=1):
            if i in existing:
                results.append(existing[i])
                print(f"[{n}/{len(indexed)}] (resumed) {example['issue_title'][:70]}")
                continue

            print(f"[{n}/{len(indexed)}] {example['issue_title'][:70]}")
            r = run_example(example, i, collection, model, max_tool_calls=args.max_tool_calls)
            results.append(r)
            out_f.write(json.dumps(r) + "\n")
            out_f.flush()

            if r["run_error"]:
                consecutive_run_errors += 1
                print(f"    RUN ERROR: {r['run_error'][:150]}")
                if consecutive_run_errors >= 3:
                    print(
                        "\nStopping early: 3 consecutive run errors, likely a "
                        "systemic API issue (quota/outage). Partial results "
                        f"saved to {results_path}. Re-run with --resume to continue."
                    )
                    break
            else:
                consecutive_run_errors = 0
                print(
                    f"    precision={r['precision']:.2f} recall={r['recall']:.2f} "
                    f"f1={r['f1']:.2f} turns={r['turns']}"
                )

    summary = summarize(results)
    print("\n" + "=" * 60)
    print(f"Scored: {summary['n_scored']}  Failed to run: {summary['n_failed']}")
    if summary["n_scored"] > 0:
        print(
            f"Precision: {summary['precision']:.3f}  Recall: {summary['recall']:.3f}  "
            f"F1: {summary['f1']:.3f}  Avg turns: {summary['avg_turns']:.1f}"
        )
        print("\nBy category:")
        for cat in CATEGORIES:
            b = summary["category_breakdown"][cat]
            if b["n"] == 0:
                print(f"  {cat}: 0 examples")
                continue
            print(
                f"  {cat}: n={b['n']}  precision={b['precision']:.2f}  "
                f"recall={b['recall']:.2f}  f1={b['f1']:.2f}  avg_turns={b['avg_turns']:.1f}"
            )
        if summary["patterns"]:
            print("\nObserved patterns:")
            for p in summary["patterns"]:
                print(f"  - {p}")


if __name__ == "__main__":
    main()

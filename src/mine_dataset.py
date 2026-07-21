"""
Mine a clean (issue -> changed files) eval dataset from a real GitHub repo.

For each closed issue in the configured repo, we look at the issue's timeline
for a CLOSED_EVENT whose `closer` is a merged pull request. If exactly one
such merged PR closed the issue, we record the issue's title/body and the
list of files that PR changed. Issues without a single, unambiguous, merged
PR closer are skipped -- we don't guess.

Usage:
    python src/mine_dataset.py

Requires a GitHub personal access token in .env (GITHUB_TOKEN=...) since the
GraphQL API used here requires authentication. See .env.example.
"""

import json
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()  # must happen before `import config`, which reads GITHUB_TOKEN at import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

ISSUES_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(
      states: CLOSED
      first: $pageSize
      after: $cursor
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        body
        timelineItems(itemTypes: [CLOSED_EVENT], last: 10) {
          nodes {
            ... on ClosedEvent {
              closer {
                ... on PullRequest {
                  number
                  merged
                  changedFiles
                  files(first: 100) {
                    totalCount
                    nodes {
                      path
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def run_graphql_query(session, query, variables, max_retries=6):
    """POST a GraphQL query, handling primary/secondary rate limits with backoff."""
    backoff = 5
    for attempt in range(1, max_retries + 1):
        resp = session.post(
            config.GITHUB_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            timeout=30,
        )

        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset_at = resp.headers.get("X-RateLimit-Reset")

        if resp.status_code == 200:
            payload = resp.json()
            errors = payload.get("errors")
            if errors:
                messages = " | ".join(e.get("message", "") for e in errors)
                if any("rate limit" in e.get("message", "").lower() for e in errors):
                    _sleep_for_rate_limit(reset_at, backoff)
                    backoff = min(backoff * 2, 120)
                    continue
                raise RuntimeError(f"GraphQL error: {messages}")

            if remaining is not None and int(remaining) < 50:
                _sleep_for_rate_limit(reset_at, backoff, proactive=True)

            return payload["data"]

        if resp.status_code in (403, 429):
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = int(retry_after)
            else:
                wait = _sleep_for_rate_limit(reset_at, backoff, dry_run=True)
            print(f"  Rate limited (status {resp.status_code}). Sleeping {wait}s...")
            time.sleep(wait)
            backoff = min(backoff * 2, 120)
            continue

        if resp.status_code >= 500:
            print(f"  Server error {resp.status_code}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        resp.raise_for_status()

    raise RuntimeError("Exceeded max retries against GitHub API")


def _sleep_for_rate_limit(reset_at, backoff, dry_run=False, proactive=False):
    if reset_at:
        wait = max(int(reset_at) - int(time.time()), 1) + 1
    else:
        wait = backoff
    if dry_run:
        return wait
    if proactive:
        print(f"  Approaching rate limit, pausing {wait}s until reset...")
    time.sleep(wait)
    return wait


def extract_merged_closer(issue):
    """Return the (unique) merged PR that closed this issue, or None.

    Only returns a result when every CLOSED_EVENT with a PR closer points to
    the *same* merged PR -- if the issue was closed/reopened by different
    PRs we can't cleanly attribute it, so we skip it.
    """
    timeline_nodes = issue.get("timelineItems", {}).get("nodes", [])
    merged_prs = {}
    for node in timeline_nodes:
        closer = (node or {}).get("closer")
        if closer and closer.get("merged"):
            merged_prs[closer["number"]] = closer

    if len(merged_prs) != 1:
        return None
    return next(iter(merged_prs.values()))


def main():
    if not config.GITHUB_TOKEN:
        print(
            "ERROR: GITHUB_TOKEN not set. Copy .env.example to .env and add a "
            "GitHub personal access token (the GraphQL API requires auth).",
            file=sys.stderr,
        )
        sys.exit(1)

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "issue-localizer-miner",
        }
    )

    output_path = Path(config.OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    collected = 0
    scanned = 0
    skipped_no_clean_pr = 0
    skipped_empty_body = 0
    skipped_file_count = 0
    cursor = None

    print(
        f"Mining {config.REPO_OWNER}/{config.REPO_NAME} for issue->file examples "
        f"(target: {config.TARGET_EXAMPLES})..."
    )

    with output_path.open("w", encoding="utf-8") as out_file:
        while collected < config.TARGET_EXAMPLES and scanned < config.MAX_ISSUES_SCANNED:
            data = run_graphql_query(
                session,
                ISSUES_QUERY,
                {
                    "owner": config.REPO_OWNER,
                    "name": config.REPO_NAME,
                    "pageSize": config.ISSUES_PAGE_SIZE,
                    "cursor": cursor,
                },
            )

            issues_conn = data["repository"]["issues"]
            nodes = issues_conn["nodes"]
            if not nodes:
                break

            for issue in nodes:
                scanned += 1

                closer = extract_merged_closer(issue)
                if closer is None:
                    skipped_no_clean_pr += 1
                    continue

                title = (issue.get("title") or "").strip()
                body = (issue.get("body") or "").strip()
                if not title or not body:
                    skipped_empty_body += 1
                    continue

                files_conn = closer.get("files") or {}
                if files_conn.get("totalCount", 0) > 100:
                    # PR touched more files than we paginated; treat as unclean.
                    skipped_no_clean_pr += 1
                    continue

                changed_files = [n["path"] for n in files_conn.get("nodes", [])]
                if not (
                    config.MIN_CHANGED_FILES
                    <= len(changed_files)
                    <= config.MAX_CHANGED_FILES
                ):
                    skipped_file_count += 1
                    continue

                record = {
                    "issue_title": title,
                    "issue_body": body,
                    "changed_files": changed_files,
                }
                out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_file.flush()
                collected += 1

                if collected % 10 == 0:
                    print(f"  Collected {collected}/{config.TARGET_EXAMPLES}...")

                if collected >= config.TARGET_EXAMPLES:
                    break

            page_info = issues_conn["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

    print()
    print(f"Done. Scanned {scanned} closed issues.")
    print(f"  Collected:              {collected}")
    print(f"  Skipped (no clean PR):  {skipped_no_clean_pr}")
    print(f"  Skipped (empty body):   {skipped_empty_body}")
    print(f"  Skipped (file count):   {skipped_file_count}")
    print(f"Output written to {output_path}")


if __name__ == "__main__":
    main()

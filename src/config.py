"""Configuration for Issue Localizer, Phase 1 (dataset mining)."""

import os

# --- Target repository ---
# Pick a real, actively maintained, mid-size repo so the codebase (and the
# issue/PR history) stays manageable. Pillow is a good fit: ~12.5k stars,
# very active, long history of issues closed by a single merged PR.
# Change these two values to point the miner at a different repo.
REPO_OWNER = "python-pillow"
REPO_NAME = "Pillow"

# --- GitHub API ---
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# --- Mining parameters ---
# Stop once we've collected this many clean examples (dataset is meant to
# land in the 50-100 range).
TARGET_EXAMPLES = 80

# Safety cap on how many closed issues we'll scan through looking for
# TARGET_EXAMPLES matches, so the script can't run forever on a repo where
# few issues have clean issue->PR links.
MAX_ISSUES_SCANNED = 3000

# Issues are fetched from the GraphQL API in pages of this size.
ISSUES_PAGE_SIZE = 50

# Only keep examples where the linked merged PR changed between MIN and MAX
# files (inclusive). This keeps the dataset focused on issues with a
# meaningful, learnable file-localization signal rather than issues closed
# by huge, unrelated refactor PRs.
MIN_CHANGED_FILES = 1
MAX_CHANGED_FILES = 20

# --- Output ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "eval_dataset.jsonl")

# --- Code index (Phase 2: build_index.py) ---
# Where the target repo is shallow-cloned so it can be chunked/embedded.
# This is a build cache, not source we own -- it's gitignored, not committed.
REPO_GIT_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git"
REPO_CACHE_DIR = os.path.join(PROJECT_ROOT, ".cache", "repo")

# Only files under these subdirectories of the repo are indexed (skips
# tests/docs/vendored code that would otherwise dominate the index).
INDEX_INCLUDE_DIRS = ["src"]

# Local, free sentence-transformers model used to embed code chunks.
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# On-disk Chroma index (persisted so it isn't rebuilt on every run).
CHROMA_PERSIST_DIR = os.path.join(PROJECT_ROOT, ".cache", "chroma")
CHROMA_COLLECTION_NAME = f"{REPO_NAME.lower()}_code_chunks"

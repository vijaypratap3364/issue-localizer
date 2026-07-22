"""
Build a local, on-disk semantic code index for the configured repo (Phase 2).

Pipeline (each stage added incrementally):
  1. Shallow-clone the repo's source into a local cache dir.
  2. Chunk its Python source by function/class using the `ast` module.
  3. Embed each chunk locally with sentence-transformers (no paid API).
  4. Persist the embeddings + chunk metadata to an on-disk Chroma index.

Usage:
    python src/build_index.py
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def clone_repo():
    """Shallow-clone (depth=1) the configured repo into the local cache dir.

    If the cache dir already contains a clone, just fetch+reset to the latest
    default-branch commit instead of re-cloning from scratch.
    """
    dest = Path(config.REPO_CACHE_DIR)

    if (dest / ".git").exists():
        print(f"Repo cache already exists at {dest}, updating...")
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin"],
            cwd=dest,
            check=True,
        )
        subprocess.run(
            ["git", "reset", "--hard", "origin/HEAD"],
            cwd=dest,
            check=True,
        )
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Shallow-cloning {config.REPO_GIT_URL} into {dest}...")
    subprocess.run(
        ["git", "clone", "--depth", "1", config.REPO_GIT_URL, str(dest)],
        check=True,
    )
    return dest


def main():
    repo_dir = clone_repo()
    print(f"Repo ready at: {repo_dir}")


if __name__ == "__main__":
    main()

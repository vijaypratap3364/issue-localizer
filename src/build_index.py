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

import ast
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


def _function_source(source_lines, node):
    """Full source of a function/method, including its decorators."""
    start = node.lineno
    if node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    end = node.end_lineno
    return "\n".join(source_lines[start - 1 : end])


def _class_summary_source(source_lines, node):
    """Class signature + docstring only (methods are chunked separately,
    so the class chunk itself stays short and doesn't duplicate them)."""
    start = node.lineno
    docstring = ast.get_docstring(node)
    if docstring and node.body and isinstance(node.body[0], ast.Expr):
        end = node.body[0].end_lineno
    else:
        end = node.lineno
    return "\n".join(source_lines[start - 1 : end])


def _collect_chunks(body, class_stack, file_rel_path, source_lines, chunks):
    """Recursively collect function/method/class chunks from a list of
    statements (a module, class, or function body). Deliberately doesn't
    descend into if/for/try/etc. blocks -- top-level and class/function-
    nested defs cover the vast majority of real code."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            qualified_name = ".".join(class_stack + [node.name])
            chunks.append(
                {
                    "id": f"{file_rel_path}::{qualified_name}:{node.lineno}",
                    "file": file_rel_path,
                    "qualified_name": qualified_name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "source": _class_summary_source(source_lines, node),
                }
            )
            _collect_chunks(
                node.body, class_stack + [node.name], file_rel_path, source_lines, chunks
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified_name = ".".join(class_stack + [node.name])
            chunks.append(
                {
                    "id": f"{file_rel_path}::{qualified_name}:{node.lineno}",
                    "file": file_rel_path,
                    "qualified_name": qualified_name,
                    "type": "method" if class_stack else "function",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "source": _function_source(source_lines, node),
                }
            )
            _collect_chunks(
                node.body, class_stack + [node.name], file_rel_path, source_lines, chunks
            )


def chunk_repository(repo_dir):
    """Parse every .py file under the configured include-dirs and return a
    list of function/method/class chunks (see _collect_chunks)."""
    repo_dir = Path(repo_dir)
    chunks = []
    py_files = []
    for include_dir in config.INDEX_INCLUDE_DIRS:
        py_files.extend(sorted((repo_dir / include_dir).rglob("*.py")))

    for py_file in py_files:
        rel_path = py_file.relative_to(repo_dir).as_posix()
        source = py_file.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as e:
            print(f"  Skipping {rel_path}: syntax error ({e})")
            continue
        source_lines = source.splitlines()
        _collect_chunks(tree.body, [], rel_path, source_lines, chunks)

    return chunks


def main():
    repo_dir = clone_repo()
    print(f"Repo ready at: {repo_dir}")

    chunks = chunk_repository(repo_dir)
    print(f"\nChunked into {len(chunks)} function/method/class chunks.")

    by_type = {}
    for c in chunks:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    print("By type:", by_type)

    print("\nSample chunks:")
    for c in chunks[:3]:
        print("-" * 60)
        print(f"id: {c['id']}")
        print(f"type: {c['type']}  lines: {c['start_line']}-{c['end_line']}")
        print(c["source"][:300])


if __name__ == "__main__":
    main()

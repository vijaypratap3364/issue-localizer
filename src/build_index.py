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

import argparse
import ast
import subprocess
import sys
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

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


def _embedding_text(chunk):
    """Text actually fed to the embedding model for a chunk: a small header
    (file + qualified name) so the model has symbol/location context, plus
    the source itself."""
    return f"# {chunk['file']} :: {chunk['qualified_name']} ({chunk['type']})\n{chunk['source']}"


def load_embedding_model():
    return SentenceTransformer(config.EMBEDDING_MODEL_NAME)


def embed_chunks(chunks, model=None):
    """Embed every chunk locally and return a list of embedding vectors
    (one per chunk, same order as `chunks`)."""
    model = model or load_embedding_model()
    texts = [_embedding_text(c) for c in chunks]
    embeddings = model.encode(
        texts,
        batch_size=64,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    return embeddings


def get_chroma_collection():
    """Open (creating if needed) the on-disk Chroma collection. The client
    persists to CHROMA_PERSIST_DIR, so data survives across runs."""
    client = chromadb.PersistentClient(path=config.CHROMA_PERSIST_DIR)
    return client.get_or_create_collection(
        name=config.CHROMA_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def persist_chunks(collection, chunks, embeddings, batch_size=500):
    """Write chunks + their embeddings into the Chroma collection in
    batches (Chroma caps how many items can be added in a single call)."""
    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_embeddings = embeddings[i : i + batch_size]
        collection.add(
            ids=[c["id"] for c in batch_chunks],
            embeddings=[e.tolist() for e in batch_embeddings],
            documents=[c["source"] for c in batch_chunks],
            metadatas=[
                {
                    "file": c["file"],
                    "qualified_name": c["qualified_name"],
                    "type": c["type"],
                    "start_line": c["start_line"],
                    "end_line": c["end_line"],
                }
                for c in batch_chunks
            ],
        )


def build_index(rebuild=False):
    """Build (or reuse) the on-disk Chroma index. Returns the collection.

    If the collection already has data and `rebuild` is False, the whole
    clone/chunk/embed pipeline is skipped -- that's the point of persisting
    to disk: this doesn't need to happen on every run.
    """
    collection = get_chroma_collection()

    if collection.count() > 0 and not rebuild:
        print(
            f"Index already persisted at {config.CHROMA_PERSIST_DIR} "
            f"with {collection.count()} chunks. Skipping rebuild "
            "(pass --rebuild to force)."
        )
        return collection

    if collection.count() > 0 and rebuild:
        print("Rebuilding: clearing existing collection...")
        client = chromadb.PersistentClient(path=config.CHROMA_PERSIST_DIR)
        client.delete_collection(config.CHROMA_COLLECTION_NAME)
        collection = client.create_collection(
            name=config.CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    repo_dir = clone_repo()
    print(f"Repo ready at: {repo_dir}")

    chunks = chunk_repository(repo_dir)
    print(f"Chunked into {len(chunks)} function/method/class chunks.")

    print(f"Embedding {len(chunks)} chunks with '{config.EMBEDDING_MODEL_NAME}'...")
    model = load_embedding_model()
    embeddings = embed_chunks(chunks, model=model)

    print(f"Persisting {len(chunks)} chunks to Chroma at {config.CHROMA_PERSIST_DIR}...")
    persist_chunks(collection, chunks, embeddings)
    print(f"Done. Collection now has {collection.count()} chunks.")

    return collection


def query_index(collection, query_text, model=None, top_k=5):
    """Embed `query_text` with the same model used to build the index and
    return the top_k nearest chunks."""
    model = model or load_embedding_model()
    query_embedding = model.encode([query_text], convert_to_numpy=True)[0]
    return collection.query(query_embeddings=[query_embedding.tolist()], n_results=top_k)


def print_query_results(query_text, results):
    print(f"\nQuery: {query_text!r}")
    ids = results["ids"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]
    for rank, (id_, meta, dist, doc) in enumerate(
        zip(ids, metadatas, distances, documents), start=1
    ):
        print(
            f"  {rank}. {meta['file']} :: {meta['qualified_name']} "
            f"({meta['type']}, lines {meta['start_line']}-{meta['end_line']}) "
            f"[distance={dist:.3f}]"
        )
        snippet = doc.strip().splitlines()[0][:100]
        print(f"     {snippet}")


DEMO_QUERIES = [
    "resize an image to a new width and height",
    "open and identify an image file's format",
    "convert an image between color modes like RGB and CMYK",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force a full re-clone/re-chunk/re-embed even if an index is already persisted.",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Run this single query against the index instead of the built-in demo queries.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    collection = build_index(rebuild=args.rebuild)
    print(
        f"\nIndex ready: '{collection.name}' at {config.CHROMA_PERSIST_DIR} "
        f"({collection.count()} chunks)."
    )

    model = load_embedding_model()
    queries = [args.query] if args.query else DEMO_QUERIES
    for q in queries:
        results = query_index(collection, q, model=model, top_k=args.top_k)
        print_query_results(q, results)


if __name__ == "__main__":
    main()

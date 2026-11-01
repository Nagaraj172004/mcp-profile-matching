"""Chunk resumes by section, embed them, and store them in ChromaDB.

Resumes are loaded with list_files and read_file from fs_tools.py. Each
section (Summary, Skills, Experience, Education) becomes its own chunk so a
job can match the relevant part of a resume. Embeddings come from the
Hugging Face model all-MiniLM-L6-v2.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from fs_tools import list_files, read_file
from tls import use_system_certificates

EMBED_MODEL = "all-MiniLM-L6-v2"
COLLECTION_NAME = "resumes"
SECTION_NAMES = ("summary", "skills", "experience", "education", "projects")
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

_model = None
_client = None


def _model_is_cached() -> bool:
    hub_cache = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    snapshots = Path(hub_cache) / "models--sentence-transformers--{0}".format(EMBED_MODEL) / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def get_model():
    """Load the embedding model once per process, offline when it is already cached."""
    global _model
    if _model is None:
        use_system_certificates()
        if _model_is_cached():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def get_client(persist_dir: str = "chroma_db"):
    """Open the on-disk Chroma client once per process."""
    global _client
    if _client is None:
        import chromadb

        _client = chromadb.PersistentClient(path=persist_dir)
    return _client


def get_collection(persist_dir: str = "chroma_db"):
    """Return the resume collection. Run build_index first."""
    return get_client(persist_dir).get_collection(COLLECTION_NAME)


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _pack(section: str, text: str) -> list:
    """Keep short sections whole. Split long ones with a small overlap."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_SIZE:
        return [{"section": section, "text": text}]

    parts = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_SIZE)
        piece = text[start:end].strip()
        if piece:
            parts.append({"section": section, "text": piece})
        if end >= len(text):
            break
        next_start = end - CHUNK_OVERLAP
        if next_start <= start:
            next_start = end
        start = next_start
    return parts


def chunk_by_section(text: str) -> list:
    """Split a resume on section headings such as Skills and Experience."""
    chunks = []
    current = "header"
    buffer = []
    for line in _clean(text).splitlines():
        key = line.strip().lower()
        if key in SECTION_NAMES:
            chunks.extend(_pack(current, "\n".join(buffer)))
            current = key
            buffer = [line]
        else:
            buffer.append(line)
    chunks.extend(_pack(current, "\n".join(buffer)))
    return chunks


def _section_body(text: str, section: str) -> str:
    collecting = False
    body = []
    for line in _clean(text).splitlines():
        key = line.strip().lower()
        if key in SECTION_NAMES:
            if collecting:
                break
            collecting = key == section
            continue
        if collecting:
            body.append(line)
    return "\n".join(body).strip()


def extract_metadata(text: str, resume_path: str) -> dict:
    """Pull name, skills, years of experience, and education from a resume."""
    lines = [line.strip() for line in _clean(text).splitlines() if line.strip()]
    name = lines[0] if lines else "Unknown"
    years_match = re.search(r"(\d+)\s*\+?\s*years", _clean(text), flags=re.IGNORECASE)
    years = int(years_match.group(1)) if years_match else 0
    return {
        "candidate_name": name,
        "skills": " ".join(_section_body(text, "skills").split()),
        "experience_years": years,
        "education": " ".join(_section_body(text, "education").split()),
        "resume_path": resume_path,
    }


def relative_resume_path(filepath: str) -> str:
    """Store resume paths with forward slashes, relative to the project root."""
    path = Path(filepath)
    try:
        relative = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        relative = path
    return relative.as_posix()


def iter_resumes(persist_dir: str = "chroma_db") -> dict:
    """Group stored chunks by resume path."""
    collection = get_collection(persist_dir)
    payload = collection.get(include=["metadatas", "documents"])
    grouped = {}
    documents = payload.get("documents") or []
    metadatas = payload.get("metadatas") or []
    for document, meta in zip(documents, metadatas):
        slot = grouped.setdefault(meta["resume_path"], {"meta": meta, "chunks": []})
        slot["chunks"].append({"section": meta["section"], "text": document})
    return grouped


def index_documents(resumes: list, replace: bool = True, persist_dir: str = "chroma_db") -> dict:
    """Chunk, embed, and store resumes given as {"path", "content"} dicts.

    replace=True rebuilds the collection from these resumes only; replace=False
    adds them, first removing any chunks already stored for the same paths.
    """
    started = time.perf_counter()
    ids = []
    documents = []
    metadatas = []
    paths = []
    for item in resumes:
        resume_path = Path(item["path"]).as_posix()
        meta = extract_metadata(item["content"], resume_path)
        paths.append(resume_path)
        for index, chunk in enumerate(chunk_by_section(item["content"])):
            ids.append("{0}::{1}::{2}".format(Path(resume_path).name, chunk["section"], index))
            documents.append(chunk["text"])
            chunk_meta = dict(meta)
            chunk_meta["section"] = chunk["section"]
            metadatas.append(chunk_meta)

    if not documents:
        raise ValueError("No resume text to index.")

    vectors = get_model().encode(documents, show_progress_bar=False)
    client = get_client(persist_dir)
    if replace:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
    collection = client.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    if not replace:
        for resume_path in paths:
            collection.delete(where={"resume_path": resume_path})
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=vectors.tolist(),
        metadatas=metadatas,
    )
    return {
        "resumes": len(paths),
        "chunks": len(ids),
        "total_chunks": collection.count(),
        "paths": paths,
        "seconds": round(time.perf_counter() - started, 2),
    }


def remove_documents(paths: list, persist_dir: str = "chroma_db") -> dict:
    """Delete every chunk stored for the given resume paths."""
    collection = get_client(persist_dir).get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    before = collection.count()
    for resume_path in paths:
        collection.delete(where={"resume_path": Path(resume_path).as_posix()})
    return {"removed_chunks": before - collection.count(), "total_chunks": collection.count(), "paths": list(paths)}


def build_index(resumes_dir: str = "resumes", persist_dir: str = "chroma_db") -> dict:
    """Offline rebuild straight from disk. The agent uses ingest.py, which goes through MCP."""
    loaded = []
    for item in list_files(resumes_dir):
        result = read_file(item["path"])
        if not result.get("success"):
            print("skip {0}: {1}".format(item["path"], result.get("error")))
            continue
        loaded.append({"path": relative_resume_path(item["path"]), "content": result["content"]})
    if not loaded:
        raise SystemExit("No resumes indexed. Run python generate_resumes.py first.")
    return index_documents(loaded, replace=True, persist_dir=persist_dir)


def main() -> None:
    stats = build_index()
    print(
        "Indexed {resumes} resumes into {chunks} chunks in {seconds} seconds.".format(**stats)
    )


if __name__ == "__main__":
    main()

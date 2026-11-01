"""Resume search MCP server: the Milestone 2 vector index behind the Model Context Protocol.

The server owns the Chroma collection and the embedding model, so the model
loads once per server process instead of once per agent. It never reads
files itself: the agent reads resumes through the filesystem MCP server and
hands the text to index_resumes / upsert_resumes.

    python resume_search_mcp_server.py                    # stdio
    python resume_search_mcp_server.py --transport http   # http://127.0.0.1:8102/mcp

Tools: index_resumes, upsert_resumes, remove_resumes, rank_resumes,
semantic_search, skill_vocabulary, index_stats.
Resources: index://stats, index://resumes.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

import mcp_errors as E
import resume_rag
from job_matcher import _excerpt, candidate_id, rank_resumes as score_resumes, skill_vocabulary as vocabulary
from mcp_common import add_http_routes, guarded, run_server
from mcp_settings import load_settings, server_logger

SETTINGS = load_settings()
CONF = SETTINGS.resume_search
LOG = server_logger("resume_search")
CHROMA_DIR = str(SETTINGS.project_path(CONF.chroma_dir))
resume_rag.COLLECTION_NAME = CONF.collection


class ResumeDocument(BaseModel):
    path: str = Field(description="Project-relative path, e.g. resumes/resume_nagaraj.txt")
    content: str = Field(description="Full resume text")


class Requirements(BaseModel):
    title: str = ""
    must_have: List[str] = Field(default_factory=list)
    nice_to_have: List[str] = Field(default_factory=list)
    min_years: int = 0
    description: str = ""


def _count() -> int:
    try:
        return resume_rag.get_collection(CHROMA_DIR).count()
    except Exception:
        return 0


def _require_index() -> None:
    if _count() == 0:
        raise E.ToolFailure(E.INDEX_EMPTY, "The resume index is empty. Run python ingest.py to index resumes through MCP.")


server = MCPServer(
    CONF.server_name,
    title="Resume vector search",
    description="Section-level embeddings of resumes in ChromaDB with hybrid scoring.",
    instructions="Call index_stats first. rank_resumes scores every resume against structured requirements; semantic_search answers open questions.",
    version="1.0.0",
    log_level=SETTINGS.logging.level,
)


@server.tool()
@guarded
def index_resumes(resumes: List[ResumeDocument], replace: bool = True) -> dict:
    """Chunk, embed, and store resumes. replace=True rebuilds the index from exactly these resumes."""
    if not resumes:
        raise E.ToolFailure(E.INVALID_PARAMS, "No resumes given.")
    stats = resume_rag.index_documents([r.model_dump() for r in resumes], replace=replace, persist_dir=CHROMA_DIR)
    LOG.info("indexed %d resumes (%d chunks, replace=%s)", stats["resumes"], stats["chunks"], replace)
    return stats


@server.tool()
@guarded
def upsert_resumes(resumes: List[ResumeDocument]) -> dict:
    """Add or replace a few resumes without rebuilding the whole index (used when watch_directory finds new files)."""
    if not resumes:
        raise E.ToolFailure(E.INVALID_PARAMS, "No resumes given.")
    stats = resume_rag.index_documents([r.model_dump() for r in resumes], replace=False, persist_dir=CHROMA_DIR)
    LOG.info("upserted %s", ", ".join(stats["paths"]))
    return stats


@server.tool()
@guarded
def remove_resumes(paths: List[str]) -> dict:
    """Remove resumes from the index, e.g. when watch_directory reports them deleted."""
    if not paths:
        raise E.ToolFailure(E.INVALID_PARAMS, "No paths given.")
    stats = resume_rag.remove_documents(paths, persist_dir=CHROMA_DIR)
    LOG.info("removed %s (%d chunks)", ", ".join(paths), stats["removed_chunks"])
    return stats


@server.tool()
@guarded
def rank_resumes(requirements: Requirements, strict: bool = False) -> dict:
    """Score every indexed resume against requirements (hybrid: similarity, skills, years) and return them best first, with score breakdowns."""
    _require_index()
    result = score_resumes(requirements.model_dump(), k=10, strict=strict, persist_dir=CHROMA_DIR)
    return {"pool_size": result["pool_size"], "ranked": result["all"]}


@server.tool()
@guarded
def semantic_search(query: str, k: int = 5) -> dict:
    """Semantic search over resume sections for open questions such as 'who has payments experience?'."""
    _require_index()
    if not query.strip():
        raise E.ToolFailure(E.INVALID_PARAMS, "query must not be empty.")
    collection = resume_rag.get_collection(CHROMA_DIR)
    k = max(1, min(int(k), 25))
    payload = collection.query(
        query_embeddings=[resume_rag.get_model().encode(query).tolist()],
        n_results=min(collection.count(), k * 5),
        include=["documents", "metadatas", "distances"],
    )
    best: Dict[str, Dict[str, Any]] = {}
    for document, meta, distance in zip(payload["documents"][0], payload["metadatas"][0], payload["distances"][0]):
        cid = candidate_id(meta["resume_path"])
        similarity = round(1.0 - float(distance), 3)
        if cid not in best or similarity > best[cid]["similarity"]:
            best[cid] = {
                "candidate_id": cid,
                "candidate_name": meta["candidate_name"],
                "resume_path": meta["resume_path"],
                "similarity": similarity,
                "section": meta["section"],
                "excerpt": _excerpt(document, 200),
                "years": meta["experience_years"],
                "skills": meta["skills"],
            }
    hits = sorted(best.values(), key=lambda h: -h["similarity"])[:k]
    return {"query": query, "hits": hits}


@server.tool()
@guarded
def skill_vocabulary() -> dict:
    """Every skill listed on any indexed resume, in its original spelling."""
    _require_index()
    return {"skills": vocabulary(CHROMA_DIR)}


@server.tool()
@guarded
def index_stats() -> dict:
    """How many resumes and chunks are indexed, and which embedding model is used."""
    return _stats()


def _stats() -> dict:
    count = _count()
    resumes = len(resume_rag.iter_resumes(CHROMA_DIR)) if count else 0
    return {"resumes": resumes, "chunks": count, "embedding_model": resume_rag.EMBED_MODEL, "collection": CONF.collection}


@server.resource("index://stats", name="index_stats", description="Resume and chunk counts.", mime_type="application/json")
def stats_resource() -> dict:
    return _stats()


@server.resource("index://resumes", name="indexed_resumes", description="Every indexed resume with name, years, and skills.", mime_type="application/json")
def indexed_resumes() -> dict:
    if not _count():
        return {"count": 0, "resumes": []}
    grouped = resume_rag.iter_resumes(CHROMA_DIR)
    items = [
        {"path": path, "candidate_name": slot["meta"]["candidate_name"], "years": slot["meta"]["experience_years"], "skills": slot["meta"]["skills"]}
        for path, slot in sorted(grouped.items())
    ]
    return {"count": len(items), "resumes": items}


def describe() -> dict:
    return {"server_name": CONF.server_name, "chroma_dir": CONF.chroma_dir, "collection": CONF.collection, "embedding_model": resume_rag.EMBED_MODEL}


add_http_routes(server, "resume_search", describe)


if __name__ == "__main__":
    resume_rag.get_model()
    run_server(server, "resume_search")

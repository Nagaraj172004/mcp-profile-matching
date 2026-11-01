"""Configuration shared by the MCP servers and the agent's MCP client.

Values come from mcp_config.json (or the file named by MCP_CONFIG) and can be
overridden with environment variables, so the same code runs locally, in
tests, and in Docker without edits:

    MCP_TRANSPORT            stdio | http
    MCP_HOST                 host the HTTP servers bind to / the client connects to
    MCP_LOG_LEVEL            DEBUG | INFO | WARNING | ERROR
    MCP_FS_ALLOWED_DIRS      comma-separated folders the filesystem server may read
    MCP_FS_WRITABLE_DIRS     comma-separated folders it may write
    MCP_FS_MAX_FILE_MB       largest file it will open
    MCP_BATCH_MAX_WORKERS    threads used by batch_process
    MCP_<SERVER>_URL         full URL of an HTTP server, e.g. MCP_FILESYSTEM_URL
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parent
SERVER_NAMES = ("filesystem", "resume_search", "candidate_db")


class FilesystemSettings(BaseModel):
    server_name: str = "resume-filesystem"
    allowed_directories: List[str] = Field(default_factory=lambda: ["resumes", "jobs", "reports"])
    writable_directories: List[str] = Field(default_factory=lambda: ["reports"])
    supported_extensions: List[str] = Field(default_factory=lambda: [".pdf", ".txt", ".docx"])
    writable_extensions: List[str] = Field(default_factory=lambda: [".md", ".txt", ".json"])
    max_file_size_mb: float = 5
    max_batch_size: int = 200
    batch_max_workers: int = 8
    watch_poll_interval_seconds: float = 1.0
    watch_max_timeout_seconds: float = 120


class ResumeSearchSettings(BaseModel):
    server_name: str = "resume-search"
    chroma_dir: str = "chroma_db"
    collection: str = "resumes"


class CandidateDbSettings(BaseModel):
    server_name: str = "candidate-db"
    database_path: str = "data/candidates.db"


class TransportSettings(BaseModel):
    mode: str = "stdio"
    host: str = "127.0.0.1"
    ports: Dict[str, int] = Field(default_factory=lambda: {"filesystem": 8101, "resume_search": 8102, "candidate_db": 8103})


class LoggingSettings(BaseModel):
    level: str = "INFO"
    directory: str = "logs"


class Settings(BaseModel):
    filesystem: FilesystemSettings = Field(default_factory=FilesystemSettings)
    resume_search: ResumeSearchSettings = Field(default_factory=ResumeSearchSettings)
    candidate_db: CandidateDbSettings = Field(default_factory=CandidateDbSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    def server_url(self, server: str) -> str:
        override = os.environ.get("MCP_{0}_URL".format(server.upper()))
        if override:
            return override
        return "http://{0}:{1}/mcp".format(self.transport.host, self.transport.ports[server])

    def project_path(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else PROJECT_DIR / path


def _split(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _apply_env(data: dict) -> dict:
    env = os.environ
    fs = data.setdefault("filesystem", {})
    transport = data.setdefault("transport", {})
    logs = data.setdefault("logging", {})
    if env.get("MCP_TRANSPORT"):
        transport["mode"] = env["MCP_TRANSPORT"].strip().lower()
    if env.get("MCP_HOST"):
        transport["host"] = env["MCP_HOST"].strip()
    if env.get("MCP_LOG_LEVEL"):
        logs["level"] = env["MCP_LOG_LEVEL"].strip().upper()
    if env.get("MCP_FS_ALLOWED_DIRS"):
        fs["allowed_directories"] = _split(env["MCP_FS_ALLOWED_DIRS"])
    if env.get("MCP_FS_WRITABLE_DIRS"):
        fs["writable_directories"] = _split(env["MCP_FS_WRITABLE_DIRS"])
    if env.get("MCP_FS_MAX_FILE_MB"):
        fs["max_file_size_mb"] = float(env["MCP_FS_MAX_FILE_MB"])
    if env.get("MCP_BATCH_MAX_WORKERS"):
        fs["batch_max_workers"] = int(env["MCP_BATCH_MAX_WORKERS"])
    return data


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    path = Path(os.environ.get("MCP_CONFIG", PROJECT_DIR / "mcp_config.json"))
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return Settings.model_validate(_apply_env(data))


def server_logger(server: str) -> logging.Logger:
    """Log to logs/<server>.log and stderr. stdout is reserved for JSON-RPC on stdio."""
    settings = load_settings()
    logger = logging.getLogger("mcp." + server)
    if logger.handlers:
        return logger
    logger.setLevel(settings.logging.level)
    directory = settings.project_path(settings.logging.directory)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(directory / "{0}.log".format(server), encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger

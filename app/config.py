"""Central configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    app_name: str = "DataPilot AI"
    version: str = "1.2.0"

    # --- Google Gemini & Vertex AI --------------------------------------
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    use_vertex_ai: bool = field(default_factory=lambda: os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true"))
    gcp_project: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    gcp_location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    llm_timeout_s: int = field(default_factory=lambda: _int("LLM_TIMEOUT_S", 25))

    # --- Google Cloud BigQuery -------------------------------------------
    bigquery_enabled: bool = field(default_factory=lambda: os.getenv("ENABLE_BIGQUERY", "true").lower() in ("1", "true"))
    bigquery_project: str = field(default_factory=lambda: os.getenv("BIGQUERY_PROJECT", os.getenv("GOOGLE_CLOUD_PROJECT", "")))
    bigquery_dataset: str = field(default_factory=lambda: os.getenv("BIGQUERY_DATASET", "datapilot_analytics"))
    bigquery_max_bytes_billed: int = field(default_factory=lambda: _int("BIGQUERY_MAX_BYTES_BILLED", 100 * 1024 * 1024))  # 100MB cap

    # --- Firebase / Google Identity --------------------------------------
    firebase_project_id: str = field(default_factory=lambda: os.getenv("FIREBASE_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "datapilot-ai-demo")))
    firebase_api_key: str = field(default_factory=lambda: os.getenv("FIREBASE_API_KEY", ""))

    # --- Security --------------------------------------------------------
    api_keys: list[str] = field(default_factory=lambda: _list("APP_API_KEYS", ""))  # empty = auth off (local demo)
    allowed_origins: list[str] = field(default_factory=lambda: _list("ALLOWED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"))
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 60))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 10))
    max_rows: int = field(default_factory=lambda: _int("MAX_ROWS", 500_000))
    max_cols: int = field(default_factory=lambda: _int("MAX_COLS", 100))
    max_question_chars: int = field(default_factory=lambda: _int("MAX_QUESTION_CHARS", 500))
    query_timeout_ms: int = field(default_factory=lambda: _int("QUERY_TIMEOUT_MS", 3000))
    result_row_limit: int = field(default_factory=lambda: _int("RESULT_ROW_LIMIT", 200))
    max_datasets: int = field(default_factory=lambda: _int("MAX_DATASETS", 50))

    # --- Agent / efficiency ----------------------------------------------
    max_repair_attempts: int = field(default_factory=lambda: _int("MAX_REPAIR_ATTEMPTS", 2))
    cache_ttl_s: int = field(default_factory=lambda: _int("CACHE_TTL_S", 900))
    cache_size: int = field(default_factory=lambda: _int("CACHE_SIZE", 256))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gemini_api_key) or (self.use_vertex_ai and bool(self.gcp_project))

    def update_runtime_keys(self, gemini_api_key: str | None = None, gcp_project: str | None = None, model: str | None = None):
        """Allows dynamically configuring keys in-memory from the UI without restarting the server."""
        if gemini_api_key is not None:
            self.gemini_api_key = gemini_api_key.strip()
        if gcp_project is not None:
            self.gcp_project = gcp_project.strip()
            self.bigquery_project = gcp_project.strip()
        if model is not None:
            self.gemini_model = model.strip()


settings = Settings()

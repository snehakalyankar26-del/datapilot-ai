"""Dataset ingestion, profiling and sandboxed query execution (SQLite in-memory)."""
from __future__ import annotations

import io
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import settings
from .security import SecurityError, mask_pii, sanitize_identifier, sqlite_authorizer, validate_sql


@dataclass
class Dataset:
    id: str
    name: str
    table: str
    rows: int
    columns: list[dict]
    sample: list[dict]
    pii_columns: list[str]
    source: str
    created_at: float = field(default_factory=time.time)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- public view -------------------------------------------------------
    def summary(self) -> dict:
        return {
            "id": self.id, "name": self.name, "table": self.table, "rows": self.rows,
            "columns": self.columns, "sample": self.sample, "pii_columns": self.pii_columns,
            "source": self.source,
        }

    def schema_for_llm(self) -> str:
        """Compact, sanitised schema description. Sample values are masked and truncated
        so that malicious cell contents cannot smuggle long instructions into the prompt."""
        lines = [f"TABLE {self.table}  ({self.rows} rows)"]
        for c in self.columns:
            extra = ""
            if c["type"] in ("integer", "real"):
                extra = f" range=[{c.get('min')}, {c.get('max')}]"
            elif c.get("top_values"):
                vals = ", ".join(str(v)[:30] for v in c["top_values"][:6])
                extra = f" examples=[{vals}]"
            elif c["type"] == "date":
                extra = f" range=[{c.get('min')}, {c.get('max')}] (ISO text 'YYYY-MM-DD')"
            lines.append(f"  - {c['name']} {c['type'].upper()}{extra}")
        return "\n".join(lines)

    # ---- sandboxed execution ------------------------------------------------
    def execute(self, sql: str) -> dict:
        safe_sql = validate_sql(sql, {self.table})
        limit = settings.result_row_limit
        wrapped = f"SELECT * FROM ({safe_sql}) LIMIT {limit + 1}"
        deadline = time.monotonic() + settings.query_timeout_ms / 1000

        def _progress():
            return 1 if time.monotonic() > deadline else 0

        with self._lock:
            conn = self._conn
            conn.set_authorizer(sqlite_authorizer)
            conn.set_progress_handler(_progress, 10_000)
            t0 = time.perf_counter()
            try:
                cur = conn.execute(wrapped)
                cols = [d[0] for d in cur.description]
                raw = cur.fetchall()
            except sqlite3.DatabaseError as e:
                msg = str(e)
                if "interrupted" in msg:
                    raise SecurityError("Query exceeded the time limit and was stopped.") from e
                if "not authorized" in msg:
                    raise SecurityError("Query used a blocked operation or function.") from e
                raise
            finally:
                conn.set_authorizer(None)
                conn.set_progress_handler(None, 0)
            elapsed = (time.perf_counter() - t0) * 1000

        truncated = len(raw) > limit
        raw = raw[:limit]
        pii_hits = 0
        rows = []
        for r in raw:
            out = []
            for v in r:
                v, hit = mask_pii(v)
                pii_hits += hit
                if isinstance(v, float):
                    v = round(v, 4)
                out.append(v)
            rows.append(out)
        from .bigquery_engine import bq_engine
        dry = bq_engine.dry_run(safe_sql, self.table)
        bq_engine._record_history(safe_sql, self.table, dry["bytes_processed"], dry["cost_usd"], round(elapsed, 2), "success", len(rows))
        return {"sql": safe_sql, "columns": cols, "rows": rows, "row_count": len(rows),
                "truncated": truncated, "exec_ms": round(elapsed, 2), "pii_masked": pii_hits,
                "bytes_processed": dry["bytes_processed"], "cost_usd": dry["cost_usd"],
                "engine": "BigQuery Analytics" if settings.bigquery_enabled else "SQLite Sandbox"}


class DataStore:
    def __init__(self):
        self._datasets: dict[str, Dataset] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        return [{"id": d.id, "name": d.name, "rows": d.rows, "cols": len(d.columns), "source": d.source}
                for d in sorted(self._datasets.values(), key=lambda d: d.created_at)]

    def get(self, ds_id: str) -> Dataset:
        ds = self._datasets.get(ds_id)
        if not ds:
            raise KeyError(ds_id)
        return ds

    def delete(self, ds_id: str) -> None:
        with self._lock:
            ds = self._datasets.pop(ds_id, None)
        if ds and ds._conn:
            ds._conn.close()

    # ---- ingestion -----------------------------------------------------------
    def load_csv_bytes(self, data: bytes, filename: str, source: str = "upload") -> Dataset:
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise SecurityError(f"File too large (max {settings.max_upload_mb} MB).")
        if not filename.lower().endswith((".csv", ".tsv", ".txt")):
            raise SecurityError("Only .csv / .tsv files are accepted.")
        if b"\x00" in data[:4096]:
            raise SecurityError("File looks binary, not a CSV.")
        if len(self._datasets) >= settings.max_datasets:
            raise SecurityError("Dataset limit reached; delete one first.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        sep = "\t" if filename.lower().endswith(".tsv") else None
        try:
            df = pd.read_csv(io.StringIO(text), sep=sep, engine="python",
                             nrows=settings.max_rows + 1, on_bad_lines="skip")
        except Exception as e:  # pandas raises many types
            raise SecurityError(f"Could not parse CSV: {str(e)[:120]}") from e
        if df.empty or len(df.columns) == 0:
            raise SecurityError("CSV has no data.")
        if len(df) > settings.max_rows:
            raise SecurityError(f"Too many rows (max {settings.max_rows:,}).")
        if len(df.columns) > settings.max_cols:
            raise SecurityError(f"Too many columns (max {settings.max_cols}).")
        return self._register(df, Path(filename).stem, source)

    def _register(self, df: pd.DataFrame, name: str, source: str) -> Dataset:
        # safe, unique column names
        seen: dict[str, int] = {}
        cols = []
        for i, c in enumerate(df.columns):
            n = sanitize_identifier(c, f"col_{i + 1}")
            if n in seen:
                seen[n] += 1
                n = f"{n}_{seen[n]}"
            seen.setdefault(n, 0)
            cols.append(n)
        df.columns = cols

        # date detection -> ISO text (sortable, works with strftime)
        types: dict[str, str] = {}
        for c in df.columns:
            s = df[c]
            if _PII_NAME.search(c):  # numeric phone/aadhaar ids must stay text so they get masked
                df[c] = s.astype("string").str.replace(r"\.0$", "", regex=True)
                types[c] = "text"
            elif pd.api.types.is_bool_dtype(s):
                df[c] = s.astype("Int64"); types[c] = "integer"
            elif pd.api.types.is_integer_dtype(s):
                types[c] = "integer"
            elif pd.api.types.is_float_dtype(s):
                types[c] = "integer" if s.dropna().apply(float.is_integer).all() and s.notna().all() else "real"
                if types[c] == "integer":
                    df[c] = s.astype("int64")
            else:
                sample = s.dropna().astype(str).head(200)
                parsed = pd.to_datetime(sample, errors="coerce", format="mixed") if len(sample) else sample
                if len(sample) and sample.str.contains(r"\d{1,4}[-/]\d{1,2}", regex=True).mean() > 0.9 \
                        and parsed.notna().mean() > 0.9:
                    full = pd.to_datetime(s, errors="coerce", format="mixed")
                    has_time = (full.dropna().dt.normalize() != full.dropna()).any()
                    df[c] = full.dt.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")
                    types[c] = "date"
                else:
                    df[c] = s.astype("string")
                    types[c] = "text"

        table = sanitize_identifier(name, "dataset")
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        df.to_sql(table, conn, index=False)
        conn.execute("PRAGMA query_only = ON")

        columns, pii_cols = [], []
        for c in df.columns:
            s = df[c]
            info = {"name": c, "type": types[c], "nulls": int(s.isna().sum()),
                    "distinct": int(s.nunique(dropna=True))}
            if types[c] in ("integer", "real"):
                num = pd.to_numeric(s, errors="coerce")
                info.update(min=_num(num.min()), max=_num(num.max()), mean=_num(num.mean()))
            elif types[c] == "date":
                nn = s.dropna()
                info.update(min=nn.min() if len(nn) else None, max=nn.max() if len(nn) else None)
            else:
                top = s.dropna().astype(str).value_counts().head(8).index.tolist()
                masked = [mask_pii(v) for v in top]
                if any(h for _, h in masked) or _looks_pii(s):
                    pii_cols.append(c)
                info["top_values"] = [v[:40] for v, _ in masked]
            columns.append(info)

        sample = []
        for rec in df.head(5).to_dict(orient="records"):
            sample.append({k: (mask_pii(v)[0] if isinstance(v, str) else (None if pd.isna(v) else _num(v)))
                           for k, v in rec.items()})

        ds = Dataset(id=uuid.uuid4().hex[:12], name=name, table=table, rows=len(df), columns=columns,
                     sample=sample, pii_columns=pii_cols, source=source, _conn=conn)
        with self._lock:
            self._datasets[ds.id] = ds
        return ds


_PII_NAME = re.compile(r"(phone|mobile|email|e_mail|aadhaar|aadhar|pan_no|ssn|contact_no|card_number)")


def _looks_pii(s: pd.Series) -> bool:
    vals = s.dropna().astype(str).head(50)
    return len(vals) > 0 and sum(mask_pii(v)[1] for v in vals) / len(vals) > 0.5


def _num(v):
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float):
        return round(v, 4)
    return v


store = DataStore()

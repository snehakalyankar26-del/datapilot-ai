"""The DataPilot agent: guard -> plan -> generate SQL -> validate -> execute ->
self-repair -> visualise -> explain -> verify grounding. Every step is traced."""
from __future__ import annotations

import re
import sqlite3
import time

from .config import settings
from .datastore import Dataset
from .llm import EXPLAIN_PROMPT, SYSTEM_PROMPT, LLMError, LLMResult, gemini, offline_explain, offline_plan
from .security import SecurityError, clean_text, detect_prompt_injection
from .telemetry import Metrics, TTLCache, audit

cache = TTLCache(settings.cache_size, settings.cache_ttl_s)
metrics = Metrics()


class Trace:
    def __init__(self):
        self.steps: list[dict] = []
        self._t = time.perf_counter()

    def add(self, name: str, status: str, detail: str = "", **extra):
        now = time.perf_counter()
        self.steps.append({"step": name, "status": status, "detail": detail,
                           "ms": round((now - self._t) * 1000, 1), **extra})
        self._t = now


def _plan_prompt(question: str, ds: Dataset, history: list[dict]) -> str:
    hist = ""
    if history:
        hist = "Previous attempts that FAILED (fix them):\n" + "\n".join(
            f"- SQL: {h['sql']}\n  Error: {h['error']}" for h in history) + "\n\n"
    return (f"Schema:\n{ds.schema_for_llm()}\n\n{hist}"
            f"<user_question>{question}</user_question>\nReturn the JSON object.")


def _generate(question: str, ds: Dataset, history: list[dict], state: dict) -> LLMResult:
    if settings.llm_enabled and not state.get("fallback"):
        try:
            res = gemini.generate_json(SYSTEM_PROMPT, _plan_prompt(question, ds, history))
            state["llm_calls"] += 1
            return res
        except LLMError as e:
            state["fallback"] = True
            state["fallback_reason"] = str(e)
    return offline_plan(question, ds)


def _fix_chart(chart: dict | None, columns: list[str], rows: list[list]) -> dict:
    chart = dict(chart or {})
    ctype = chart.get("type", "none")
    if ctype not in ("bar", "line", "pie", "scatter", "none") or not rows or len(columns) < 2:
        return {"type": "none"}
    numeric = [c for i, c in enumerate(columns)
               if all(isinstance(r[i], (int, float)) or r[i] is None for r in rows)]
    if not numeric:
        return {"type": "none"}
    if chart.get("y") not in numeric:
        chart["y"] = numeric[-1]
    if chart.get("x") not in columns or chart.get("x") == chart["y"]:
        chart["x"] = next((c for c in columns if c not in numeric), columns[0])
    if ctype == "pie" and len(rows) > 12:
        chart["type"] = "bar"
    # a 3rd categorical column => multi-series line/bar
    others = [c for c in columns if c not in (chart["x"], chart["y"]) and c not in numeric]
    chart["series"] = others[0] if others else None
    if chart.get("type") == "none":
        chart["type"] = "line" if chart["x"] == "period" else "bar"
    chart.setdefault("title", "")
    return chart


_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def grounding_score(answer: str, result: dict) -> float:
    """Share of numbers in the narrative that are traceable to the result set
    (allowing rounding, percentages and simple differences/sums)."""
    nums = []
    for tok in _NUM.findall(answer or ""):
        try:
            v = float(tok.replace(",", ""))
        except ValueError:
            continue
        if not (1900 <= v <= 2100 and float(v).is_integer()) and not (float(v).is_integer() and 0 <= v <= 12):
            nums.append(v)
    if not nums:
        return 1.0
    values = [float(c) for r in result["rows"] for c in r if isinstance(c, (int, float))]
    for r in result["rows"]:
        for c in r:
            if isinstance(c, str):
                values += [float(x.replace(",", "")) for x in _NUM.findall(c) if x.replace(",", "").replace(".", "").lstrip("-").isdigit()]
    if not values:
        return 0.0
    total = sum(values)
    head = values[:50]
    derived = set(values) | {total} | {a - b for a in head for b in head} | {
        round(a / b, 2) for a in head for b in head if b}
    def ok(n):
        for v in derived:
            if abs(v - n) <= max(0.01, abs(v) * 0.006):
                return True
            if total and abs(v / total * 100 - n) < 0.06:
                return True
            if v and any(abs((w - v) / v * 100 - n) < 0.06 for w in values[:50]):
                return True
        return False
    return round(sum(ok(n) for n in nums) / len(nums), 2)


def answer_question(raw_question: str, ds: Dataset) -> dict:
    t_start = time.perf_counter()
    trace = Trace()
    state = {"llm_calls": 0, "fallback": False}
    tokens_in = tokens_out = 0

    # 1. Guard ---------------------------------------------------------------
    try:
        question = clean_text(raw_question, settings.max_question_chars)
    except SecurityError as e:
        trace.add("guard", "blocked", str(e))
        metrics.record(outcome="blocked", ms=(time.perf_counter() - t_start) * 1000)
        return _blocked(str(e), trace)
    hit = detect_prompt_injection(question)
    if hit:
        trace.add("guard", "blocked", f"Possible prompt injection detected: \"{hit}\"")
        audit("prompt_injection_blocked", dataset=ds.id, question=question[:200])
        metrics.record(outcome="blocked", ms=(time.perf_counter() - t_start) * 1000)
        return _blocked("This request looks like an attempt to override the assistant's safety rules "
                        "or modify data, so it was blocked. Ask a question about the data instead.", trace)
    trace.add("guard", "ok", "Input sanitised; no injection patterns found")

    # 2. Cache ---------------------------------------------------------------
    ck = TTLCache.key(ds.id, question)
    cached = cache.get(ck)
    if cached:
        trace.add("cache", "hit", "Served from answer cache (no LLM call, no query)")
        out = {**cached, "trace": trace.steps, "cached": True,
               "total_ms": round((time.perf_counter() - t_start) * 1000, 1)}
        metrics.record(outcome="answered", ms=out["total_ms"], engine="cache")
        return out
    trace.add("cache", "miss", "Not cached")

    # 3-5. Plan / validate / execute with self-repair -------------------------
    history: list[dict] = []
    plan, result, engine = None, None, ""
    for attempt in range(settings.max_repair_attempts + 1):
        res = _generate(question, ds, history, state)
        tokens_in += res.tokens_in
        tokens_out += res.tokens_out
        plan, engine = res.data, res.engine
        if state.get("fallback") and state.get("fallback_reason") and attempt == 0:
            trace.add("llm", "fallback", state["fallback_reason"] + " - using offline planner")
        sql = (plan.get("sql") or "").strip()
        trace.add("plan", "ok", plan.get("reasoning", "")[:400], engine=engine, attempt=attempt + 1)
        if not sql:
            break
        try:
            result = ds.execute(sql)
            trace.add("execute", "ok", f"{result['row_count']} rows in {result['exec_ms']} ms",
                      sql=result["sql"])
            break
        except SecurityError as e:
            trace.add("validate", "rejected", str(e), sql=sql)
            history.append({"sql": sql, "error": str(e)})
        except sqlite3.Error as e:
            trace.add("execute", "error", str(e)[:200], sql=sql)
            history.append({"sql": sql, "error": str(e)[:200]})
        if not settings.llm_enabled or state.get("fallback"):
            break  # deterministic planner would produce the same SQL again
        trace.add("repair", "retry", f"Self-repair attempt {attempt + 1}")

    if result is None:
        reason = (plan or {}).get("reasoning") or "Could not build a valid query for this question."
        if history:
            reason = f"{reason} Last error: {history[-1]['error']}"
        metrics.record(outcome="failed", ms=(time.perf_counter() - t_start) * 1000, engine=engine,
                       tokens_in=tokens_in, tokens_out=tokens_out, llm_calls=state["llm_calls"],
                       repairs=len(history), fallback=state["fallback"])
        audit("question_failed", dataset=ds.id, question=question[:200], error=reason[:200])
        return {"status": "failed", "answer": "I couldn't answer that from this dataset. " + reason,
                "insights": [], "trace": trace.steps, "result": None, "chart": {"type": "none"},
                "followups": (plan or {}).get("followups", [])[:3], "engine": engine,
                "total_ms": round((time.perf_counter() - t_start) * 1000, 1)}

    # 6. Visualise -----------------------------------------------------------
    chart = _fix_chart(plan.get("chart"), result["columns"], result["rows"])
    trace.add("visualise", "ok", f"Chart: {chart['type']}")

    # 7. Explain -------------------------------------------------------------
    exp = None
    if settings.llm_enabled and not state["fallback"]:
        preview = {"columns": result["columns"], "rows": result["rows"][:40]}
        try:
            exp = gemini.generate_json(
                EXPLAIN_PROMPT,
                f"<user_question>{question}</user_question>\nSQL: {result['sql']}\nResult: {preview}")
            state["llm_calls"] += 1
            tokens_in += exp.tokens_in
            tokens_out += exp.tokens_out
        except LLMError as e:
            trace.add("llm", "fallback", f"{e} - template narrative used")
    if exp is None:
        exp = offline_explain(question, result)
    answer = str(exp.data.get("answer", ""))[:1500]
    insights = [str(i)[:300] for i in (exp.data.get("insights") or [])][:4]

    # 8. Verify grounding -----------------------------------------------------
    g = grounding_score(answer + " " + " ".join(insights), result)
    trace.add("verify", "ok" if g >= 0.8 else "warn", f"Grounding score {int(g * 100)}% of numbers traced to results")

    out = {"status": "answered", "question": question, "answer": answer, "insights": insights,
           "caveats": exp.data.get("caveats", ""), "result": result, "chart": chart,
           "followups": [str(f)[:120] for f in (plan.get("followups") or [])][:3],
           "engine": engine, "grounding": g, "tokens": {"in": tokens_in, "out": tokens_out},
           "llm_calls": state["llm_calls"], "cached": False}
    cache.set(ck, {k: v for k, v in out.items()})
    out["trace"] = trace.steps
    out["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    metrics.record(outcome="answered", ms=out["total_ms"], engine=engine, tokens_in=tokens_in,
                   tokens_out=tokens_out, llm_calls=state["llm_calls"], repairs=len(history),
                   fallback=state["fallback"])
    audit("question_answered", dataset=ds.id, engine=engine, rows=result["row_count"],
          ms=out["total_ms"], grounding=g)
    return out


def _blocked(msg: str, trace: Trace) -> dict:
    return {"status": "blocked", "answer": msg, "insights": [], "result": None,
            "chart": {"type": "none"}, "followups": [], "trace": trace.steps, "engine": "guard"}

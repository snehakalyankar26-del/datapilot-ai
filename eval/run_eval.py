"""Accuracy + latency benchmark.

Each case pairs a natural-language question with a hand-written GOLD SQL query.
The agent's result set is compared with the gold result (order-insensitive, values rounded).
Works in offline mode and with Gemini (set GEMINI_API_KEY) so the two engines can be compared.

    python -m eval.run_eval            # prints a table + summary, writes eval/report.json
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.datastore import DataStore  # noqa: E402

F, H, E = "food_delivery_orders", "hospital_admissions", "energy_emissions"
CASES = [
    (F, "What is the total order value by city?", "SELECT city, ROUND(SUM(order_value_inr),2) FROM food_delivery_orders GROUP BY city"),
    (F, "Average rating by cuisine", "SELECT cuisine, ROUND(AVG(rating),2) FROM food_delivery_orders GROUP BY cuisine"),
    (F, "How many orders were paid by UPI?", "SELECT COUNT(*) FROM food_delivery_orders WHERE payment_method='UPI'"),
    (F, "Top 3 cuisines by food waste", "SELECT cuisine, ROUND(SUM(food_waste_kg),2) s FROM food_delivery_orders GROUP BY cuisine ORDER BY s DESC LIMIT 3"),
    (F, "Which city has the highest average delivery minutes?", "SELECT city, ROUND(AVG(delivery_minutes),2) FROM food_delivery_orders GROUP BY city"),
    (F, "Monthly trend of order value", "SELECT strftime('%Y-%m',order_date), ROUND(SUM(order_value_inr),2) FROM food_delivery_orders GROUP BY 1"),
    (F, "Share of orders by payment method", "SELECT payment_method, COUNT(*) FROM food_delivery_orders GROUP BY 1"),
    (F, "average order value in Pune", "SELECT ROUND(AVG(order_value_inr),2) FROM food_delivery_orders WHERE city='Pune'"),
    (F, "How many orders are there?", "SELECT COUNT(*) FROM food_delivery_orders"),
    (F, "Average delivery minutes in Mumbai", "SELECT ROUND(AVG(delivery_minutes),2) FROM food_delivery_orders WHERE city='Mumbai'"),
    (H, "Average length of stay by department", "SELECT department, ROUND(AVG(length_of_stay_days),2) FROM hospital_admissions GROUP BY 1"),
    (H, "Readmission rate by age group", "SELECT age_group, ROUND(100.0*AVG(readmitted_30d),2) FROM hospital_admissions GROUP BY 1"),
    (H, "Total treatment cost per month", "SELECT strftime('%Y-%m',admission_date), ROUND(SUM(treatment_cost),2) FROM hospital_admissions GROUP BY 1"),
    (H, "Which department has the highest total treatment cost?", "SELECT department, ROUND(SUM(treatment_cost),2) FROM hospital_admissions GROUP BY 1"),
    (H, "Number of admissions by insurance", "SELECT insurance, COUNT(*) FROM hospital_admissions GROUP BY 1"),
    (E, "Total CO2 by energy source", "SELECT energy_source, ROUND(SUM(co2_kg),2) FROM energy_emissions GROUP BY 1"),
    (E, "Which facility has the highest emissions?", "SELECT facility, ROUND(SUM(co2_kg),2) FROM energy_emissions GROUP BY 1"),
    (E, "Monthly trend of solar energy kwh", "SELECT strftime('%Y-%m',month), ROUND(SUM(energy_kwh),2) FROM energy_emissions WHERE energy_source='Solar' GROUP BY 1"),
    (E, "Total cost by facility", "SELECT facility, ROUND(SUM(cost_inr),2) FROM energy_emissions GROUP BY 1"),
    (E, "Average energy kwh by energy source", "SELECT energy_source, ROUND(AVG(energy_kwh),2) FROM energy_emissions GROUP BY 1"),
]


def _norm(rows):
    out = set()
    for r in rows:
        out.add(tuple(round(v, 1) if isinstance(v, float) else v for v in r))
    return out


def run_benchmark(custom_store: DataStore | None = None) -> dict[str, Any]:
    """Runs the 20-case golden benchmark and returns summary + detailed results."""
    store = custom_store or DataStore()
    ds = {}
    if not custom_store:
        for f in sorted((ROOT / "data" / "samples").glob("*.csv")):
            d = store.load_csv_bytes(f.read_bytes(), f.name, "sample")
            ds[d.name] = d
    else:
        for d in store.list():
            dataset_obj = store.get(d["id"])
            ds[dataset_obj.name] = dataset_obj

    agent.cache._d.clear()
    results, lat = [], []
    for name, q, gold in CASES:
        if name not in ds:
            continue
        d = ds[name]
        expected = _norm(d._conn.execute(gold).fetchall())
        t0 = time.perf_counter()
        r = agent.answer_question(q, d)
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        got = _norm(r["result"]["rows"]) if r.get("result") else set()
        ok = got == expected or (len(expected) <= 3 and expected <= got)
        results.append({
            "dataset": name,
            "question": q,
            "correct": ok,
            "ms": round(ms, 1),
            "engine": r.get("engine", "unknown"),
            "grounding": r.get("grounding", 1.0),
            "sql": (r.get("result") or {}).get("sql")
        })

    if not lat:
        return {"summary": {"error": "No benchmark datasets found"}, "results": []}

    # cache efficiency: re-ask everything
    t0 = time.perf_counter()
    for name, q, _ in CASES:
        if name in ds:
            agent.answer_question(q, ds[name])
    cached_ms = (time.perf_counter() - t0) * 1000 / len(lat)

    acc = sum(r["correct"] for r in results) / len(results)
    summary = {
        "engine": settings.gemini_model if settings.llm_enabled else "offline-planner",
        "cases": len(results),
        "accuracy": round(acc, 3),
        "accuracy_pct": f"{round(acc * 100, 1)}%",
        "latency_ms": {
            "p50": round(statistics.median(lat), 1),
            "p95": round(sorted(lat)[int(len(lat) * .95) - 1], 1),
            "mean": round(statistics.mean(lat), 1)
        },
        "cached_latency_ms_avg": round(cached_ms, 2),
        "avg_grounding": round(statistics.mean(r["grounding"] or 0 for r in results), 3),
        "avg_grounding_pct": f"{round(statistics.mean(r['grounding'] or 0 for r in results) * 100, 1)}%"
    }
    return {"summary": summary, "results": results}


def main():
    res = run_benchmark()
    summary = res["summary"]
    results = res["results"]
    for r in results:
        status = "PASS" if r["correct"] else "FAIL"
        print(f"{status}  {r['ms']:7.1f} ms  {r['question']}")
    print("\n" + json.dumps(summary, indent=2))
    report_file = ROOT / "eval" / "report.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

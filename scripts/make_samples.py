"""Generate the synthetic demo datasets in data/samples (deterministic, seed=42).
All data is synthetic; e-mails/phones are fake and exist only to demonstrate PII masking."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
OUT = Path(__file__).resolve().parent.parent / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)


def daterange(start: date, days: int):
    return start + timedelta(days=random.randint(0, days))


# 1. Food delivery & waste (food + sustainability) -------------------------
cities = {"Pune": 1.0, "Mumbai": 1.3, "Bengaluru": 1.2, "Delhi": 1.1, "Hyderabad": 0.9}
cuisines = {"North Indian": 380, "South Indian": 260, "Chinese": 320, "Pizza": 450, "Biryani": 340, "Healthy Bowls": 410}
pay = ["UPI", "Card", "Cash", "Wallet"]
with open(OUT / "food_delivery_orders.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Order ID", "Order Date", "City", "Cuisine", "Order Value (INR)", "Delivery Minutes",
                "Rating", "Payment Method", "Food Waste (kg)", "Customer Email"])
    for i in range(1, 3001):
        city = random.choices(list(cities), weights=[3, 4, 4, 3, 2])[0]
        cui = random.choice(list(cuisines))
        d = daterange(date(2025, 1, 1), 364)
        seasonal = 1.15 if d.month in (10, 11, 12) else 1.0
        value = round(random.gauss(cuisines[cui], 90) * cities[city] * seasonal, 2)
        mins = max(12, int(random.gauss(32, 9) + (6 if city in ("Mumbai", "Delhi") else 0)))
        rating = max(1.0, min(5.0, round(random.gauss(4.6 - (mins - 30) * 0.03, 0.4), 1)))
        waste = round(max(0, random.gauss(0.12 if cui != "Healthy Bowls" else 0.05, 0.05)), 3)
        w.writerow([f"ORD{i:05d}", d.isoformat(), city, cui, max(99, value), mins, rating,
                    random.choice(pay), waste, f"user{random.randint(100, 999)}@example.com"])

# 2. Hospital admissions (healthcare) ----------------------------------------
depts = {"Cardiology": (6.5, 0.18, 185000), "Orthopedics": (4.2, 0.08, 120000), "Oncology": (8.1, 0.22, 240000),
         "Pediatrics": (2.9, 0.05, 45000), "Neurology": (5.8, 0.14, 160000), "General Medicine": (3.4, 0.11, 38000)}
ages = ["0-17", "18-34", "35-49", "50-64", "65+"]
with open(OUT / "hospital_admissions.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["admission_id", "admission_date", "department", "age_group", "length_of_stay_days",
                "readmitted_30d", "treatment_cost", "insurance", "patient_phone"])
    for i in range(1, 2501):
        dep = random.choice(list(depts))
        los_m, readm, cost = depts[dep]
        age = "0-17" if dep == "Pediatrics" else random.choices(ages[1:], weights=[2, 3, 4, 4])[0]
        age_f = 1.35 if age == "65+" else 1.0
        los = max(1, round(random.gauss(los_m * age_f, 1.6)))
        w.writerow([f"ADM{i:05d}", daterange(date(2025, 1, 1), 364).isoformat(), dep, age, los,
                    int(random.random() < readm * age_f), round(abs(random.gauss(cost, cost * 0.25)) * (los / los_m), 2),
                    random.choice(["Government", "Private", "Self-pay"]), f"9{random.randint(100000000, 999999999)}"])

# 3. Energy & emissions (sustainability) ------------------------------------
facilities = {"Pune Plant": 1.0, "Chennai Plant": 1.2, "Nagpur Warehouse": 0.4, "Bengaluru Office": 0.25, "Ahmedabad Plant": 0.9}
sources = {"Grid": 0.71, "Solar": 0.04, "Wind": 0.011, "Diesel Generator": 0.82}
with open(OUT / "energy_emissions.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["month", "facility", "energy_source", "energy_kwh", "co2_kg", "cost_inr"])
    for m in range(24):
        month = date(2024 + m // 12, m % 12 + 1, 1)
        solar_growth = 1 + m * 0.06
        for fac, scale in facilities.items():
            for src, ef in sources.items():
                base = {"Grid": 90000, "Solar": 18000 * solar_growth, "Wind": 9000, "Diesel Generator": 6000 / (1 + m * 0.05)}[src]
                kwh = round(base * scale * random.uniform(0.85, 1.15))
                price = {"Grid": 8.2, "Solar": 3.1, "Wind": 3.6, "Diesel Generator": 22.0}[src]
                w.writerow([month.isoformat(), fac, src, kwh, round(kwh * ef, 1), round(kwh * price, 2)])
print("samples written to", OUT)

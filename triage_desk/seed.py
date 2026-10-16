"""Synthetic patients only. No real patient data is ever used in this repo."""
from . import db

W = {"mobility": "walking", "trauma": "no", "avpu": "A", "temp_c": 36.8}  # shared 'well' defaults

DEMO = [
    dict(pid="P-001", name="Synthetic A. (chest pain)", age=54, sex="F",
         symptoms=["Chest pain since morning, kifua kinabana, sweating. Walked in on her own."],
         vitals=[{**W, "resp_rate": 24, "heart_rate": 112, "sbp": 95}]),
    dict(pid="P-002", name="Synthetic B. (minor)", age=27, sex="M",
         symptoms=["Sore throat and mild cough for 3 days."],
         vitals=[{**W, "resp_rate": 16, "heart_rate": 84, "sbp": 122}]),
    dict(pid="P-003", name="Synthetic C. (missing vitals)", age=41, sex="F",
         symptoms=["Headache and dizziness since yesterday."],
         vitals=[{"mobility": "walking", "resp_rate": 18, "temp_c": 37.1}]),
    dict(pid="P-004", name="Synthetic D. (child)", age=4, sex="M",
         symptoms=["Fever and not eating for two days. Mother says he is very sleepy."],
         vitals=[{"temp_c": 39.2, "heart_rate": 150}]),
    dict(pid="P-005", name="Synthetic E. (worsening)", age=63, sex="M",
         symptoms=["Cough and fever for 5 days, now short of breath."],
         vitals=[{**W, "resp_rate": 20, "heart_rate": 98, "sbp": 118, "temp_c": 38.0},
                 {**W, "resp_rate": 28, "heart_rate": 118, "sbp": 96, "temp_c": 38.9}]),
    dict(pid="P-006", name="Synthetic F. (kifafa)", age=19, sex="M",
         symptoms=["Amepata kifafa nyumbani, sasa amelala. Baada ya kifafa hajaamka vizuri."],
         vitals=[{"mobility": "immobile", "trauma": "no", "avpu": "V", "temp_c": 36.9,
                  "resp_rate": 18, "heart_rate": 102, "sbp": 130}]),
]


def seed_demo() -> int:
    db.init()
    for d in DEMO:
        db.add_patient(d["pid"], d["name"], d["age"], d["sex"], True, d["symptoms"], d["vitals"])
    return len(DEMO)


if __name__ == "__main__":
    print(f"Seeded {seed_demo()} synthetic patients")

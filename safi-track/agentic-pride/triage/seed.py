"""Load the SYNTHETIC demo patients (arrived, not yet scored).

    python -m triage.seed            # add patients to the database
    python -m triage.seed --reset    # wipe the database first
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import db

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic_patients.json"


def load():
    return json.loads(DATA.read_text(encoding="utf-8"))


def seed(reset=False):
    if reset:
        db.reset()
    else:
        db.init()
    n = 0
    for p in load()["patients"]:
        if db.get_patient(p["id"]):
            continue
        arrived = db.now() - timedelta(minutes=p["arrived_minutes_ago"])
        db.add_patient(p["id"], p["age"], p["sex"], p["complaint"], arrived, synthetic=True)
        n += 1
    return n


if __name__ == "__main__":
    print(f"Seeded {seed('--reset' in sys.argv)} synthetic patients into {db.db_path()}")

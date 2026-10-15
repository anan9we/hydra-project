"""Run with:  python -m unittest discover -s tests -v   (no extra packages needed)"""
import json
import os
import tempfile
import unittest

from triage import db, rules, seed, tools

# What the agent extracts from each nurse note (hand-copied from data/synthetic_patients.json)
V = {
    "P-001": dict(rr=24, hr=118, sbp=95, temp_c=38.9, spo2=91, avpu="A", mobility="walking",
                  very_urgent_signs=["chest_pain"]),
    "P-002": dict(rr=16, hr=82, sbp=128, temp_c=36.8, spo2=98, avpu="A", mobility="walking", trauma=True),
    "P-003": dict(rr=28, hr=104, sbp=150, temp_c=37.2, spo2=88, avpu="A", mobility="with_help",
                  very_urgent_signs=["shortness_of_breath"]),
    "P-004": dict(rr=18, hr=96, sbp=118, temp_c=38.6, spo2=98, avpu="A", mobility="walking"),
    "P-005": dict(rr=20, hr=108, sbp=130, temp_c=37.0, spo2=96, avpu="V", mobility="with_help",
                  very_urgent_signs=["post_seizure"]),
    "P-006": dict(rr=14, hr=76, sbp=142, temp_c=36.6, spo2=99, avpu="A", mobility="walking"),
    "P-007": dict(rr=30, hr=132, sbp=78, temp_c=34.8, spo2=89, avpu="P", mobility="immobile"),
    "P-008": dict(rr=20, hr=110, sbp=102, temp_c=37.0, spo2=98, avpu="A", mobility="walking",
                  very_urgent_signs=["pregnancy_bleeding"]),
    "P-010": dict(rr=18, hr=100, sbp=130, temp_c=36.8, spo2=95, avpu="A", mobility="walking",
                  emergency_signs=["airway_compromised"]),
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["TRIAGE_DB"] = os.path.join(self.tmp, "t.db")
        os.environ.pop("CLINICIANS", None)
        db.reset()
        seed.seed()


class TestRules(unittest.TestCase):
    def test_table_boundaries(self):
        self.assertEqual(rules._rr(8)[0], 2)
        self.assertEqual(rules._rr(9)[0], 0)
        self.assertEqual(rules._rr(14)[0], 0)
        self.assertEqual(rules._rr(15)[0], 1)
        self.assertEqual(rules._rr(29)[0], 2)
        self.assertEqual(rules._rr(30)[0], 3)
        self.assertEqual(rules._hr(40)[0], 2)
        self.assertEqual(rules._hr(100)[0], 0)
        self.assertEqual(rules._hr(101)[0], 1)
        self.assertEqual(rules._hr(130)[0], 3)
        self.assertEqual(rules._sbp(70)[0], 3)
        self.assertEqual(rules._sbp(100)[0], 1)
        self.assertEqual(rules._sbp(101)[0], 0)
        self.assertEqual(rules._sbp(200)[0], 2)
        self.assertEqual(rules._temp(38.4)[0], 0)
        self.assertEqual(rules._temp(38.5)[0], 2)
        self.assertEqual(rules._temp(34.9)[0], 2)

    def test_fahrenheit_rejected(self):
        errs = rules.validate(rules.Vitals(temp_c=101.3))
        self.assertTrue(errs and "Fahrenheit" in errs[0])

    def test_every_nonzero_point_is_cited(self):
        sc = rules.score(rules.Vitals(rr=24, hr=118, sbp=95, temp_c=38.9, spo2=91, avpu="A", mobility="walking"))
        text = " ".join(sc.evidence)
        for needle in ("RR 24", "HR 118", "SBP 95", "Temp 38.9", "TEWS total 7"):
            self.assertIn(needle, text)


class TestScoringCases(Base):
    def test_synthetic_cases_match_hand_calculation(self):
        expected = {p["id"]: p for p in seed.load()["patients"] if p.get("expected_band")}
        for pid, exp in expected.items():
            r = tools.score_priority(pid, **V[pid])
            self.assertTrue(r["ok"], f"{pid}: {r}")
            self.assertEqual(r["tews"], exp["expected_tews"], pid)
            self.assertEqual(r["proposed_band"], exp["expected_band"], pid)

    def test_discriminator_can_raise_band_above_tews(self):
        r = tools.score_priority("P-008", **V["P-008"])
        self.assertEqual((r["tews"], r["proposed_band"]), (2, "ORANGE"))
        self.assertTrue(any("pregnancy_bleeding" in e for e in r["evidence"]))

    def test_emergency_sign_forces_red_and_skips_missing_vitals(self):
        r = tools.score_priority("P-010", emergency_signs=["airway_compromised"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["proposed_band"], "RED")

    def test_missing_vitals_not_guessed(self):
        r = tools.score_priority("P-002", rr=16, hr=82)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "missing_vitals")
        self.assertIn("sbp", r["missing"])

    def test_paediatric_is_refused_not_scored(self):
        r = tools.score_priority("P-009", rr=26, hr=120, sbp=90, temp_c=39.1, avpu="A", mobility="walking")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "out_of_scope_paediatric")
        self.assertIsNone(db.last_observation("P-009"))

    def test_invalid_sign_rejected(self):
        r = tools.score_priority("P-001", **V["P-001"] | {"very_urgent_signs": ["heart_attack"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "invalid_sign")

    def test_new_patient_needs_registration_details(self):
        r = tools.score_priority("X-1", **V["P-006"])
        self.assertEqual(r["error"], "unknown_patient")
        r = tools.score_priority("X-1", **V["P-006"], age=40, sex="F", complaint="headache")
        self.assertTrue(r["ok"])


class TestHumanGate(Base):
    def test_agent_proposal_is_not_confirmed(self):
        tools.score_priority("P-001", **V["P-001"])
        row = [q for q in tools.get_queue()["queue"] if q["patient_id"] == "P-001"][0]
        self.assertIsNone(row["confirmed_band"])
        self.assertFalse(row["sent_to_bay_allowed"])
        self.assertEqual(row["proposed_band"], "RED")

    def test_no_approval_tool_exposed_to_agent(self):
        for name in tools.TOOL_NAMES:
            self.assertNotIn("decide", name)
            self.assertNotIn("approve", name)
            self.assertNotIn("confirm", name)
        self.assertEqual(len(tools.TOOL_NAMES), 4)

    def test_clinician_confirms(self):
        r = tools.score_priority("P-001", **V["P-001"])
        db.decide_proposal(r["proposal_id"], "Nurse Wanjiru", True)
        row = [q for q in tools.get_queue()["queue"] if q["patient_id"] == "P-001"][0]
        self.assertEqual(row["confirmed_band"], "RED")
        self.assertEqual(row["confirmed_by"], "Nurse Wanjiru")
        self.assertTrue(row["sent_to_bay_allowed"])

    def test_anonymous_or_unlisted_clinician_refused(self):
        r = tools.score_priority("P-001", **V["P-001"])
        with self.assertRaises(PermissionError):
            db.decide_proposal(r["proposal_id"], "  ", True)
        os.environ["CLINICIANS"] = "Dr Otieno"
        with self.assertRaises(PermissionError):
            db.decide_proposal(r["proposal_id"], "Random Person", True)
        db.decide_proposal(r["proposal_id"], "dr otieno", True)

    def test_override_requires_note(self):
        r = tools.score_priority("P-003", **V["P-003"])
        with self.assertRaises(ValueError):
            db.decide_proposal(r["proposal_id"], "Dr Otieno", True, final_band="RED")
        p = db.decide_proposal(r["proposal_id"], "Dr Otieno", True, note="looks worse", final_band="RED")
        self.assertEqual(p["final_band"], "RED")

    def test_rejected_proposal_confirms_nothing(self):
        r = tools.score_priority("P-001", **V["P-001"])
        db.decide_proposal(r["proposal_id"], "Dr Otieno", False, note="re-measure")
        self.assertIsNone(db.confirmed("P-001"))

    def test_cannot_decide_twice(self):
        r = tools.score_priority("P-001", **V["P-001"])
        db.decide_proposal(r["proposal_id"], "Dr Otieno", True)
        with self.assertRaises(ValueError):
            db.decide_proposal(r["proposal_id"], "Dr Otieno", True)

    def test_newer_proposal_supersedes_stale_pending(self):
        a = tools.score_priority("P-001", **V["P-001"])
        b = tools.score_priority("P-001", **V["P-001"])
        self.assertEqual(db.get_proposal(a["proposal_id"])["status"], "superseded")
        self.assertEqual(db.get_proposal(b["proposal_id"])["status"], "pending")


class TestDeterioration(Base):
    def _baseline(self, pid):
        r = tools.score_priority(pid, **V[pid])
        db.decide_proposal(r["proposal_id"], "Dr Otieno", True)

    def test_worsening_raises_flag_with_cited_values(self):
        self._baseline("P-004")
        f = seed.load()["followups"][0]
        r = tools.raise_recheck_flag(
            "P-004", rr=26, hr=124, sbp=92, temp_c=39.4, spo2=93, avpu="V", mobility="with_help")
        self.assertTrue(r["flag_raised"])
        self.assertEqual((r["tews_before"], r["tews_now"]), (3, f["expected_tews"]))
        self.assertEqual(r["proposed_band"], "RED")
        self.assertIn("RR 18/min -> 26/min", " ".join(r["evidence"]))
        # confirmed band does NOT change until a clinician acts
        self.assertEqual(db.confirmed("P-004")["final_band"], "YELLOW")
        db.decide_proposal(r["proposal_id"], "Dr Otieno", True)
        self.assertEqual(db.confirmed("P-004")["final_band"], "RED")

    def test_stable_patient_not_flagged(self):
        self._baseline("P-006")
        r = tools.raise_recheck_flag("P-006", rr=14, hr=78, sbp=140, temp_c=36.7, spo2=99,
                                     avpu="A", mobility="walking")
        self.assertTrue(r["ok"])
        self.assertFalse(r["flag_raised"])
        self.assertNotIn("proposal_id", r)

    def test_no_baseline_no_flag(self):
        r = tools.raise_recheck_flag("P-006", **V["P-006"])
        self.assertEqual(r["error"], "no_baseline")

    def test_complaint_text_alone_cannot_flag(self):
        self._baseline("P-006")
        r = tools.raise_recheck_flag("P-006", reported_change="feels much worse")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "missing_vitals")


class TestReferral(Base):
    def test_referral_cites_recorded_values_only(self):
        tools.score_priority("P-003", **V["P-003"])
        r = tools.route_referral("P-003", "general_medical", ["spo2", "shortness_of_breath"])
        self.assertTrue(r["ok"])
        self.assertIn("spo2: 88", r["evidence"])
        bad = tools.route_referral("P-003", "surgical", ["sbp_is_low"])
        self.assertEqual(bad["error"], "unsourced")

    def test_referral_needs_observation_and_valid_department(self):
        self.assertEqual(tools.route_referral("P-003", "general_medical", ["spo2"])["error"], "no_observation")
        tools.score_priority("P-003", **V["P-003"])
        self.assertEqual(tools.route_referral("P-003", "cardiology", ["spo2"])["error"], "invalid_department")
        self.assertEqual(tools.route_referral("P-003", "general_medical", [])["error"], "unsourced")

    def test_referral_is_pending_and_not_duplicated(self):
        tools.score_priority("P-003", **V["P-003"])
        a = tools.route_referral("P-003", "general_medical", ["spo2"])
        b = tools.route_referral("P-003", "general_medical", ["rr"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["proposal_id"], b["proposal_id"])
        self.assertEqual(db.get_proposal(a["proposal_id"])["status"], "pending")


class TestAudit(Base):
    def test_every_call_logged_including_failures_and_decisions(self):
        r = tools.score_priority("P-001", **V["P-001"])
        tools.score_priority("P-002", rr=16)  # fails: missing vitals
        tools.get_queue()
        db.decide_proposal(r["proposal_id"], "Nurse Wanjiru", True)
        rows = db.audit_rows()
        self.assertEqual([x["tool"] for x in rows],
                         ["score_priority", "score_priority", "get_queue", "decide_proposal"])
        self.assertEqual([x["ok"] for x in rows], [1, 0, 1, 1])
        self.assertEqual(rows[3]["actor"], "Nurse Wanjiru")
        self.assertEqual(json.loads(rows[0]["input_json"])["patient_id"], "P-001")
        self.assertTrue(rows[0]["ts"])

    def test_future_timestamp_rejected(self):
        r = tools.score_priority("P-001", **V["P-001"], observed_at="2999-01-01T00:00:00+03:00")
        self.assertEqual(r["error"], "invalid_time")


class TestQueue(Base):
    def test_unscored_patients_flagged_and_paediatric_routed_to_clinician(self):
        q = {x["patient_id"]: x for x in tools.get_queue()["queue"]}
        self.assertTrue(q["P-001"]["needs_scoring"])
        self.assertTrue(q["P-009"]["needs_clinician_direct"])
        self.assertFalse(q["P-009"]["needs_scoring"])

    def test_overdue_uses_confirmed_band_target_time(self):
        r = tools.score_priority("P-001", **V["P-001"])  # RED target = 0 min, waiting 12 min
        db.decide_proposal(r["proposal_id"], "Dr Otieno", True)
        q = {x["patient_id"]: x for x in tools.get_queue()["queue"]}
        self.assertTrue(q["P-001"]["overdue"])


if __name__ == "__main__":
    unittest.main()

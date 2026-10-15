"""Triage Desk MCP server (stdio).

Run it directly to check it starts:   python -m triage.mcp_server
Agents launch it as a subprocess over stdio (see triage/agent.py in bit 2).

Exposes four tools. There is NO tool that confirms a priority: only a named
clinician can do that, through the CLI / Telegram bot, outside the agent.
"""
from typing import List, Literal, Optional

from mcp.server.fastmcp import FastMCP

from . import db, tools

mcp = FastMCP("triage-desk")

Avpu = Literal["A", "V", "P", "U"]
Mobility = Literal["walking", "with_help", "immobile"]
Emergency = Literal[
    "airway_compromised", "not_breathing", "active_seizure",
    "uncontrolled_haemorrhage", "facial_burn_or_inhalation",
]
VeryUrgent = Literal[
    "chest_pain", "shortness_of_breath", "severe_pain", "focal_neuro_deficit",
    "post_seizure", "poisoning_overdose", "controlled_haemorrhage", "pregnancy_bleeding",
]
Department = Literal[
    "resuscitation", "general_medical", "surgical", "maternity", "orthopaedics", "paediatrics"
]


@mcp.tool()
def get_queue(include_pending: bool = True) -> dict:
    """READ-ONLY. List every waiting patient with their clinician-CONFIRMED priority band,
    minutes waiting, whether they are overdue for their band's target time, and any proposals
    still awaiting a clinician. Patients with no confirmed band must NOT be sent to a bay."""
    return tools.get_queue(include_pending)


@mcp.tool()
def score_priority(
    patient_id: str,
    rr: Optional[float] = None,
    hr: Optional[float] = None,
    sbp: Optional[float] = None,
    temp_c: Optional[float] = None,
    spo2: Optional[float] = None,
    avpu: Optional[Avpu] = None,
    mobility: Optional[Mobility] = None,
    trauma: bool = False,
    emergency_signs: Optional[List[Emergency]] = None,
    very_urgent_signs: Optional[List[VeryUrgent]] = None,
    observed_at: Optional[str] = None,
    age: Optional[int] = None,
    sex: Optional[Literal["M", "F"]] = None,
    complaint: Optional[str] = None,
) -> dict:
    """Score MEASURED vitals with an adult SATS-style scale and PROPOSE a priority band
    (GREEN/YELLOW/ORANGE/RED). The proposal is PENDING: a named clinician must confirm it
    before the patient goes to a bay. You never decide who is seen, diagnose or prescribe.
    Units: rr and hr per minute, sbp in mmHg, temp_c in Celsius, spo2 in %.
    avpu: A alert, V responds to voice, P responds to pain, U unresponsive.
    NEVER guess a value the nurse did not give: leave it out and the tool will list what is missing.
    Only set emergency_signs / very_urgent_signs when the nurse explicitly reported them.
    age, sex and complaint are needed only the first time a patient_id is seen."""
    return tools.score_priority(
        patient_id, rr, hr, sbp, temp_c, spo2, avpu, mobility, trauma,
        emergency_signs, very_urgent_signs, observed_at, age, sex, complaint,
    )


@mcp.tool()
def raise_recheck_flag(
    patient_id: str,
    rr: Optional[float] = None,
    hr: Optional[float] = None,
    sbp: Optional[float] = None,
    temp_c: Optional[float] = None,
    spo2: Optional[float] = None,
    avpu: Optional[Avpu] = None,
    mobility: Optional[Mobility] = None,
    trauma: bool = False,
    emergency_signs: Optional[List[Emergency]] = None,
    very_urgent_signs: Optional[List[VeryUrgent]] = None,
    observed_at: Optional[str] = None,
    reported_change: str = "",
) -> dict:
    """Use when a patient ALREADY scored is re-checked while waiting. Pass the NEW measured
    vitals. The tool compares them with the previous observation and raises a re-check flag for
    a clinician ONLY if the measured values worsened (TEWS up 2+, band up, or a new emergency
    sign). A patient saying they feel worse is recorded in reported_change but cannot raise a
    flag on its own: ask the nurse to re-measure vitals. You can raise the flag; you cannot act on it."""
    return tools.raise_recheck_flag(
        patient_id, rr, hr, sbp, temp_c, spo2, avpu, mobility, trauma,
        emergency_signs, very_urgent_signs, observed_at, reported_change,
    )


@mcp.tool()
def route_referral(
    patient_id: str,
    department: Department,
    cited_fields: List[str],
) -> dict:
    """PROPOSE sending a patient to a department (resuscitation, general_medical, surgical,
    maternity, orthopaedics, paediatrics). cited_fields lists the recorded values that justify it,
    using names such as 'spo2', 'rr', 'sbp', 'complaint', 'chest_pain'. The tool checks each one
    exists in the patient's latest observation and rejects unsourced referrals. Pending until a
    named clinician approves."""
    return tools.route_referral(patient_id, department, cited_fields)


def main():
    db.init()
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()

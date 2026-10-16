"""MCP server (stdio). Run with:  python -m triage_desk.mcp_server
Why MCP instead of in-process functions: the model can only reach patient data through these six typed,
logged tools; the boundary is a process boundary; and any MCP client can reuse the same server."""
from mcp.server.mcpserver import MCPServer

from . import db, sats, tools

mcp = MCPServer(
    "triage-desk",
    instructions="Triage Desk tools. Priorities come from the South African Triage Scale; every claim must "
                 "cite a nurse entry; nothing here changes the live queue (clinician approval does).",
)


@mcp.tool()
def get_intake(patient_id: str) -> dict:
    """Read one patient's intake: age, sex, the nurse's symptom notes (s1, s2..) and vitals (v1.rr ...).
    Each entry has an entry_id you must cite. Also returns keyword hints for discriminators (hints only)."""
    return tools.get_intake(patient_id)


@mcp.tool()
def score_tews(patient_id: str) -> dict:
    """Compute the SATS Triage Early Warning Score from the latest vitals, in code. Returns per-vital points
    with entry ids, the total and colour, any missing vitals, and the change since the previous round."""
    return tools.score_tews(patient_id)


@mcp.tool(description="Assert a SATS clinical discriminator for a patient. You MUST give the symptom entry_id "
         "and a verbatim quote copied from that entry; the server rejects quotes not found in the entry. "
         f"discriminator_id is one of: {sats.discriminator_catalogue_text()}.")
def assert_discriminator(patient_id: str, discriminator_id: str, entry_id: str, quote: str) -> dict:
    return tools.assert_discriminator(patient_id, discriminator_id, entry_id, quote)


@mcp.tool()
def submit_priority(patient_id: str, rationale: str) -> dict:
    """Propose the priority colour. You do NOT pass a colour: the server derives it from the TEWS and the
    discriminators you asserted. Creates a PENDING proposal for a clinician. rationale: one or two plain
    sentences about observed vitals/symptoms; never a diagnosis or treatment."""
    return tools.submit_priority(patient_id, rationale)


@mcp.tool()
def flag_recheck(patient_id: str, entry_ids: list[str], reason: str, minutes: int | None = None) -> dict:
    """Propose a re-check of this patient (missing vitals, or worsening). entry_ids: the entries that justify
    it (may be empty only if no vitals exist). minutes defaults to the SATS target time for the colour."""
    return tools.flag_recheck(patient_id, entry_ids, reason, minutes)


@mcp.tool(description="Propose a referral to a department, citing one entry (entry_id + verbatim quote for a "
         f"symptom entry). department_id is one of: {', '.join(f'{k} ({v})' for k, v in sats.DEPARTMENTS.items())}."
         " Creates a PENDING proposal; a clinician approves it.")
def route_referral(patient_id: str, department_id: str, entry_id: str, quote: str, reason: str) -> dict:
    return tools.route_referral(patient_id, department_id, entry_id, quote, reason)


if __name__ == "__main__":
    db.init()
    mcp.run("stdio")

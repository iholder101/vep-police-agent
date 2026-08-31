"""Deterministic VEP status snapshot and self-consistency realizations.

Produces diff-friendly output files after each agent cycle:
- output/vep_snapshot_YYYYMMDD_HHMM.yaml - timestamped VEP status
- output/realizations.txt - changes and anomalies vs previous run

Keeps the last 10 snapshots, older ones are pruned automatically.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from services.utils import log
from state import VEPState

OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Compliance field display names -> VEPCompliance attribute names
_COMPLIANCE_FIELDS = {
    "template": "template_complete",
    "sigs-signed-off": "all_sigs_signed_off",
    "vep-merged": "vep_merged",
    "prs-linked": "prs_linked",
    "docs-pr": "docs_pr_created",
    "labels": "labels_valid",
}

# Alert severity ordering (lower = higher priority)
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Attention levels ordered from healthiest to most urgent, for level-transition
# anomaly detection (e.g. ok -> needs_attention is worth flagging).
_ATTENTION_ORDER = {"ok": 0, "watch": 1, "needs_attention": 2}

# Maximum number of snapshot files to keep
_MAX_SNAPSHOTS = 10


def dump_snapshot(state: VEPState, cycle_duration: float = 0) -> None:
    """Write deterministic VEP status snapshot to output/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    veps = state.get("veps", [])
    summary_table = state.get("vep_summary_table", [])
    alerts = state.get("alerts", [])
    release_schedule = state.get("release_schedule")
    now = datetime.now(UTC)

    # Build urgency lookup from summary table
    urgency_by_vep = {}
    for row in summary_table:
        vep_num = row.get("vep_number")
        if vep_num is not None:
            urgency_by_vep[vep_num] = row.get("urgency")

    # Build alerts lookup by VEP ID
    alerts_by_vep: dict[int, list[dict[str, Any]]] = {}
    for alert in alerts:
        vep_id = alert.get("vep_id")
        if vep_id is not None:
            alerts_by_vep.setdefault(vep_id, []).append(alert)

    # Deadline info from release schedule
    ef_date = None
    cf_date = None
    if release_schedule:
        ef_date = release_schedule.enhancement_freeze
        cf_date = release_schedule.code_freeze

    # Build snapshot data sorted by tracking_issue_id
    sorted_veps = sorted(veps, key=lambda v: v.tracking_issue_id)
    vep_records = []

    for vep in sorted_veps:
        record = _build_vep_record(
            vep, urgency_by_vep, alerts_by_vep, ef_date, cf_date, now
        )
        vep_records.append(record)

    snapshot = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "cycle_duration_seconds": round(cycle_duration),
        "vep_count": len(vep_records),
        "veps": vep_records,
    }

    # Write timestamped YAML
    timestamp = now.strftime("%Y%m%d_%H%M")
    yaml_path = OUTPUT_DIR / f"vep_snapshot_{timestamp}.yaml"
    yaml_path.write_text(yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True))

    # Prune old snapshots
    _prune_snapshots()

    log(f"Snapshot written: {len(vep_records)} VEPs", node="snapshot")


def generate_realizations(state: VEPState, cycle_duration: float = 0) -> None:
    """Compare current vs previous snapshot, write output/realizations.txt."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    snapshots = _get_sorted_snapshots()
    now = datetime.now(UTC)

    if not snapshots:
        log("No snapshots found, skipping realizations", node="snapshot")
        return

    current = yaml.safe_load(snapshots[-1].read_text())

    if len(snapshots) < 2:
        lines = [
            f"Generated: {now.strftime('%Y-%m-%dT%H:%M:%S')} | cycle: {round(cycle_duration)}s",
            "",
            "=== Changes ===",
            "First run - no previous data",
            "",
            "=== Anomalies ===",
            "(none)",
            "",
        ]
        (OUTPUT_DIR / "realizations.txt").write_text("\n".join(lines))
        log("Realizations written (first run)", node="snapshot")
        return

    previous = yaml.safe_load(snapshots[-2].read_text())
    changes, anomalies = _diff_snapshots(previous, current)

    lines = [
        f"Generated: {now.strftime('%Y-%m-%dT%H:%M:%S')} | cycle: {round(cycle_duration)}s",
        "",
        "=== Changes ===",
    ]
    if changes:
        lines.extend(f"- {c}" for c in changes)
    else:
        lines.append("(no changes)")
    lines.append("")
    lines.append("=== Anomalies ===")
    if anomalies:
        lines.extend(f"- {a}" for a in anomalies)
    else:
        lines.append("(none)")
    lines.append("")

    (OUTPUT_DIR / "realizations.txt").write_text("\n".join(lines))
    log(f"Realizations written: {len(changes)} changes, {len(anomalies)} anomalies",
        node="snapshot")


def snapshot_node(state: VEPState) -> Any:
    """Graph node that writes per-cycle snapshot and realizations."""
    import time as _time

    last_checks = state.get("last_check_times", {})
    fetch_time = last_checks.get("fetch_veps")
    cycle_duration = 0.0
    if fetch_time:
        try:
            epoch = fetch_time.timestamp() if hasattr(fetch_time, 'timestamp') else 0
            cycle_duration = _time.time() - epoch
        except (AttributeError, TypeError):
            pass

    try:
        dump_snapshot(state, cycle_duration=cycle_duration)
    except (ValueError, TypeError, OSError, KeyError) as e:
        log(f"Snapshot failed: {e}", node="snapshot", level="ERROR")

    try:
        generate_realizations(state, cycle_duration=cycle_duration)
    except (ValueError, TypeError, OSError, KeyError) as e:
        log(f"Realizations failed: {e}", node="snapshot", level="ERROR")

    return {"last_check_times": {"snapshot": datetime.now(UTC)}}


# -- Internal helpers --


def _build_vep_record(
    vep, urgency_by_vep, alerts_by_vep, ef_date, cf_date, now
) -> dict[str, Any]:
    """Build a single VEP record for the snapshot."""
    vep_num = vep.tracking_issue_id

    # Attention assessment from analysis
    attention = vep.analysis.get("attention", {}) if vep.analysis else {}
    attention_level = attention.get("attention_level", "unknown")
    staleness = attention.get("staleness", {}) or {}
    is_stale = staleness.get("is_stale", False)

    # Compliance
    compliance = {}
    for display_name, attr_name in _COMPLIANCE_FIELDS.items():
        key = display_name.replace("-", "_")
        compliance[key] = getattr(vep.compliance, attr_name, False) if vep.compliance else False

    # Activity
    days_since_update = vep.activity.days_since_update if vep.activity else None
    review_lag_days = vep.activity.review_lag_days if vep.activity else None

    # Deadlines with pre-computed day deltas
    deadlines = {}
    if ef_date:
        deadlines["ef"] = ef_date.strftime("%Y-%m-%d") if hasattr(ef_date, "strftime") else str(ef_date)
        try:
            deadlines["ef_days"] = (ef_date - now).days
        except TypeError:
            pass
    if cf_date:
        deadlines["cf"] = cf_date.strftime("%Y-%m-%d") if hasattr(cf_date, "strftime") else str(cf_date)
        try:
            deadlines["cf_days"] = (cf_date - now).days
        except TypeError:
            pass

    # PRs
    proposal_prs = _format_pr_list(vep.enhancement_prs)
    impl_prs = _format_pr_list(vep.implementation_prs)

    # Alerts for this VEP
    vep_alerts = alerts_by_vep.get(vep_num, [])
    formatted_alerts = _format_alert_list(vep_alerts)

    # Milestone info
    milestone = vep.current_milestone
    promotion_phase = milestone.promotion_phase if milestone else None
    status = milestone.status if milestone else vep.status

    return {
        "vep_number": vep_num,
        "name": vep.name,
        "title": vep.title,
        "owner": vep.owner,
        "sig": vep.owning_sig,
        "target_release": vep.target_release,
        "status": status,
        "promotion_phase": promotion_phase,
        "urgency": urgency_by_vep.get(vep_num),
        "attention_level": attention_level,
        "is_stale": is_stale,
        "compliance": compliance,
        "days_since_update": days_since_update,
        "review_lag_days": review_lag_days,
        "deadlines": deadlines,
        "proposal_prs": proposal_prs,
        "impl_prs": impl_prs,
        "alerts": formatted_alerts,
    }


def _format_pr_list(prs) -> list[dict[str, Any]]:
    """Format PR list sorted by number."""
    if not prs:
        return []
    sorted_prs = sorted(prs, key=lambda p: p.number)
    result = []
    for pr in sorted_prs:
        state = "unknown"
        if pr.merged:
            state = "merged"
        elif pr.state:
            state = pr.state.lower()
        result.append({"number": pr.number, "state": state})
    return result


def _format_alert_list(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format alert list sorted by severity then subject."""
    if not alerts:
        return []
    sorted_alerts = sorted(
        alerts,
        key=lambda a: (_SEVERITY_ORDER.get(a.get("severity", "low"), 99), a.get("subject", "")),
    )
    return [
        {
            "severity": a.get("severity", "low"),
            "subject": a.get("subject", ""),
            "message": a.get("message", ""),
        }
        for a in sorted_alerts
    ]


def _get_sorted_snapshots() -> list[Path]:
    """Return snapshot files sorted by timestamp (oldest first)."""
    return sorted(OUTPUT_DIR.glob("vep_snapshot_*.yaml"))


def _prune_snapshots() -> None:
    """Remove oldest snapshots, keeping the last _MAX_SNAPSHOTS."""
    snapshots = _get_sorted_snapshots()
    for old in snapshots[:-_MAX_SNAPSHOTS]:
        old.unlink()


def _diff_snapshots(
    previous: dict[str, Any], current: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Diff two snapshot JSONs, return (changes, anomalies)."""
    changes: list[str] = []
    anomalies: list[str] = []

    prev_veps = {v["vep_number"]: v for v in previous.get("veps", [])}
    curr_veps = {v["vep_number"]: v for v in current.get("veps", [])}

    prev_nums = set(prev_veps.keys())
    curr_nums = set(curr_veps.keys())

    # New VEPs
    for num in sorted(curr_nums - prev_nums):
        changes.append(f"VEP-{num:04d}: new VEP appeared")

    # Disappeared VEPs
    for num in sorted(prev_nums - curr_nums):
        changes.append(f"VEP-{num:04d}: VEP disappeared")
        anomalies.append(f"VEP-{num:04d}: was in previous run but missing now (VEPs should not disappear)")

    # Compare existing VEPs
    for num in sorted(prev_nums & curr_nums):
        pv = prev_veps[num]
        cv = curr_veps[num]
        vep_id = f"VEP-{num:04d}"

        # Scalar fields
        # Exclude days_since_update and review_lag_days - they change daily and
        # create noise in every diff. They're still in the snapshot for reading.
        for field in ["status", "promotion_phase", "urgency", "attention_level", "owner",
                       "target_release"]:
            old_val = pv.get(field)
            new_val = cv.get(field)
            if old_val != new_val:
                changes.append(f"{vep_id}: {field} {old_val} -> {new_val}")

        # Attention level-transition anomaly (e.g. ok -> needs_attention is a
        # regression worth flagging; the reverse or same-severity moves are not).
        old_level = pv.get("attention_level")
        new_level = cv.get("attention_level")
        if old_level != new_level:
            old_rank = _ATTENTION_ORDER.get(old_level)
            new_rank = _ATTENTION_ORDER.get(new_level)
            if old_rank is not None and new_rank is not None and new_rank > old_rank:
                anomalies.append(
                    f"{vep_id}: attention regressed {old_level} -> {new_level}"
                )

        # Compliance sub-fields
        old_comp = pv.get("compliance", {})
        new_comp = cv.get("compliance", {})
        for key in _COMPLIANCE_FIELDS:
            comp_key = key.replace("-", "_")
            old_v = old_comp.get(comp_key)
            new_v = new_comp.get(comp_key)
            if old_v != new_v:
                changes.append(f"{vep_id}: compliance.{comp_key} {old_v} -> {new_v}")
                if old_v is True and new_v is False:
                    anomalies.append(f"{vep_id}: compliance regressed: {comp_key} true -> false")

        # PR diffs
        _diff_pr_list(pv, cv, "proposal_prs", "proposal-pr", vep_id, changes)
        _diff_pr_list(pv, cv, "impl_prs", "impl-pr", vep_id, changes, anomalies)

        # Alert diffs
        _diff_alert_list(pv, cv, vep_id, changes)

    return changes, anomalies


def _diff_pr_list(
    prev_vep: dict, curr_vep: dict, field: str, label: str, vep_id: str,
    changes: list[str], anomalies: list[str] | None = None,
) -> None:
    """Diff PR lists by number, report added/removed/state-changed."""
    old_prs = {p["number"]: p["state"] for p in prev_vep.get(field, [])}
    new_prs = {p["number"]: p["state"] for p in curr_vep.get(field, [])}

    for num in sorted(set(new_prs) - set(old_prs)):
        changes.append(f"{vep_id}: {label} #{num} added ({new_prs[num]})")

    for num in sorted(set(old_prs) - set(new_prs)):
        changes.append(f"{vep_id}: {label} #{num} removed")

    for num in sorted(set(old_prs) & set(new_prs)):
        if old_prs[num] != new_prs[num]:
            changes.append(f"{vep_id}: {label} #{num} {old_prs[num]} -> {new_prs[num]}")

    # Anomaly: PR count decreased (only for impl_prs)
    if anomalies is not None and len(new_prs) < len(old_prs):
        anomalies.append(
            f"{vep_id}: {label} count decreased {len(old_prs)} -> {len(new_prs)} "
            f"(PRs should not vanish)"
        )


def _diff_alert_list(
    prev_vep: dict, curr_vep: dict, vep_id: str, changes: list[str]
) -> None:
    """Diff alert lists by (severity, subject)."""
    old_keys = {(a["severity"], a["subject"]) for a in prev_vep.get("alerts", [])}
    new_keys = {(a["severity"], a["subject"]) for a in curr_vep.get("alerts", [])}

    for sev, subj in sorted(new_keys - old_keys):
        changes.append(f"{vep_id}: new alert [{sev}] {subj}")

    for sev, subj in sorted(old_keys - new_keys):
        changes.append(f"{vep_id}: resolved alert [{sev}] {subj}")

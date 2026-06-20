#!/usr/bin/env python3
"""PreToolUse ExitPlanMode hook — enforce plan exit gate; show DeepSeek contribution summary."""
import hashlib
import json
from pathlib import Path

METRICS_LOG = Path.home() / ".claude" / "plans" / "reviewer-metrics.jsonl"
GATE_FLAG = Path.home() / ".claude" / ".plan-gate-ok"


def read_entries(metrics_log: Path) -> list:
    if not metrics_log.exists():
        return []
    entries = []
    for line in metrics_log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def find_latest_pending(entries: list) -> "dict | None":
    for e in reversed(entries):
        if e.get("type") == "influence-pending":
            return e
    return None


def find_quality_for_plan(entries: list, plan_name: str) -> "dict | None":
    for e in reversed(entries):
        if e.get("type") == "quality" and e.get("plan") == plan_name:
            return e
    return None


def compute_sha(plan_path: Path) -> str:
    return hashlib.sha256(plan_path.read_bytes()).hexdigest()[:7]


def build_report(pending: dict, quality: "dict | None") -> str:
    bar = "━" * 12
    lines = [f"\n{bar} DEEPSEEK CONTRIBUTION REPORT {bar}\n"]
    lines.append(f"Plan: {pending.get('plan', 'unknown')}  (reviewed {pending.get('date', '?')})")

    if quality:
        avg = quality.get("avg", 0)
        pct_high = quality.get("pct_high", 0)
        n = quality.get("n", 0)
        lines.append(f"Quality: {avg}/3.0 avg · {pct_high}% high-value ({n} findings total)\n")
        findings = quality.get("findings", [])
        if findings:
            lines.append("Findings flagged:")
            for f in findings[:5]:
                lines.append(f"  • {f}")
        lines.append("")

    plan_path = Path(pending.get("path", ""))
    if plan_path.exists():
        current_sha = compute_sha(plan_path)
        sha_at_review = pending.get("sha_at_review", "")
        changed = current_sha != sha_at_review
        lines_now = len(plan_path.read_text(encoding="utf-8", errors="replace").splitlines())
        if changed:
            lines.append(f"Plan delta: ✅ Revised since review ({lines_now} lines now)")
        else:
            lines.append("Plan delta: ⚠️  Unchanged since DeepSeek's review — findings may not be addressed")
    else:
        lines.append("Plan delta: plan file not found")

    lines.append(f"\n{bar}{bar}━━━━")
    return "\n".join(lines)


GATE_BLOCK = (
    "PLAN EXIT GATE: AskUserQuestion must be called before ExitPlanMode.\n"
    "Call AskUserQuestion with:\n"
    "  question: 'Plan ready — how to proceed?'  header: 'Plan exit'\n"
    "  option 1: 'Exit & implement now'\n"
    "  option 2: 'DeepSeek Council review (~$0.004–$0.007)'\n"
    "  option 3: 'Ultra web review'\n"
    "After the user answers, run Bash(`touch ~/.claude/.plan-gate-ok`) to authorize the gate.\n"
    "Then call ExitPlanMode."
)


def main(metrics_log: Path = METRICS_LOG, gate_flag: Path = GATE_FLAG) -> None:
    # If flag exists, gate was authorized — consume and allow ExitPlanMode
    if gate_flag.exists():
        gate_flag.unlink()
        entries = read_entries(metrics_log)
        pending = find_latest_pending(entries)
        if pending:
            quality = find_quality_for_plan(entries, pending.get("plan", ""))
            report = build_report(pending, quality)
            print(json.dumps({"systemMessage": report}))
        return

    # No flag — block ExitPlanMode and instruct Claude to show the gate first
    print(json.dumps({"decision": "block", "reason": GATE_BLOCK}))


if __name__ == "__main__":
    main()

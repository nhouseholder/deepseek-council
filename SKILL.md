---
name: deepseek-council
description: Adversarial plan review — runs 4 LLM roles against your implementation plan before you write code
---

Run the DeepSeek Council against the current plan file before implementation.

## When to invoke

Use this skill when:
- You are about to exit plan mode and start coding
- You want an adversarial review of an implementation plan
- You want a second opinion from multiple LLM perspectives

## What it does

Runs 4 roles in parallel against your plan:

| Role | Focus |
|------|-------|
| Risk Analyst | Data loss, race conditions, missing rollbacks, security gaps |
| Implementation Realist | Ambiguous steps, undefined functions, missing success criteria |
| Simplicity Challenger | Over-engineering, scope creep, unnecessary abstractions |
| Senior Dev Realist | Code quality: idioms, testability, stdlib vs. custom, tech debt |

A synthesis agent returns APPROVED / REVISE / MAJOR_REVISE.

## How to invoke

```bash
python3 /path/to/deepseek-council/review.py --plan PLAN.md --council
```

Replace `/path/to/deepseek-council` with your local clone path. The council costs ~$0.004–$0.007 per run.

## Setup

```bash
git clone https://github.com/nhouseholder/deepseek-council
cd deepseek-council
bash setup.sh
echo 'DEEPSEEK_API_KEY=your_key_here' >> ~/.plan-council/.env
```

## Output

Results append to `PLAN-REVIEW-LOG.md` next to your plan file. Per-run metrics (cost, verdict, quality) are logged to `~/.plan-council/reviewer-metrics.jsonl`.

## Hook integration

To gate every `ExitPlanMode` call behind this review, add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "ExitPlanMode",
        "hooks": [
          { "type": "command", "command": "python3 /path/to/deepseek-council/contribution_report.py" }
        ]
      }
    ]
  }
}
```

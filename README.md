# deepseek-council

Runs 4 LLM roles in parallel against your implementation plan and returns a structured verdict before you write code. Each role has a distinct focus; a synthesis agent consolidates findings into APPROVED / REVISE / MAJOR_REVISE.

---

| Stat | Value |
|------|-------|
| Cost per council run | $0.003–$0.007 |
| Plan-change rate | 98% — when flagged REVISE, the plan changed 166/169 times |
| Meta-judge quality | 2.21/3.0 avg (findings rated by a separate judge call) |
| Non-obvious findings | 29% scored 3/3 — genuinely non-obvious catches |
| Reviews run | 90+ (tracked since 2026-06-13) |
| Estimated total spend | ~$0.23 across all tracked runs |

---

## How it works

Four roles review your plan independently in parallel:

| Role | Lane |
|------|------|
| Risk Analyst | Data loss, race conditions, missing rollbacks, security gaps, undefined behavior under load |
| Implementation Realist | Ambiguous steps, undefined functions/files, missing success criteria, unclear ordering |
| Simplicity Challenger | Over-engineering, scope creep, unnecessary abstractions, simpler paths to the same outcome |
| Senior Dev Realist | Production-quality code: idiomatic patterns, stdlib vs. custom, testability, tech debt, would-a-PR-reviewer-approve |

Each role can be assigned a different LLM provider via `council_role_providers` in `providers.json`. Different models have different training data and blind spots, so role diversity tends to surface more than running the same model with different prompts.

A Synthesis agent consolidates their findings and gives a final verdict:

- **APPROVED** — no MAJOR_REVISE votes and at most 1 minor REVISE
- **REVISE** — one or more concrete, actionable issues to address
- **MAJOR_REVISE** — serious issues from any reviewer; do not start coding

Results append to `PLAN-REVIEW-LOG.md` next to your plan file. A meta-judge call rates each finding on a 1–3 quality scale so you know which issues are worth acting on.

---

## Example output

CLV tracking plan, $0.0051:

```
$ python3 review.py --plan PLAN.md --council

[council] deepseek-v4-pro/anthropic-haiku/gemini-flash — 4 roles in parallel... est. cost: ~$0.0051
[council] Simplicity Challenger [gemini-flash]: REVISE
[council] Implementation Realist [anthropic-haiku]: REVISE
[council] Risk Analyst [deepseek-v4-pro]: REVISE
[council] Senior Dev Realist [deepseek-v4-pro]: REVISE
[council] Synthesizing... REVISE

━━━━━━━━━━━━━━━ COUNCIL DEBATE ━━━━━━━━━━━━━━━

Simplicity Challenger — REVISE
  • The entire outcome can be achieved by adding `open_ml` to the existing picks log — no separate ledger, patching pipeline, or seeding script needed.
  • 4 new code locations introduced when 1 change suffices.

Implementation Realist — REVISE
  • `compute_clv()` is called but never defined. No moneyline-to-probability conversion specified (+138 vs -138). Gets this wrong -> silently corrupts every CLV value.
  • `is_fade` flag stored but CLV sign inversion for fade bets never addressed — all fade-system CLV values will be systematically wrong.

Risk Analyst — REVISE
  • `system_clv_ledger.json` single-file JSON: concurrent read-modify-write across 3 pipeline phases with no locking. Silent data corruption under normal sequential use is possible.
  • Seed script has no deduplication — re-run after failure silently doubles all historical CLV.

Senior Dev Realist — REVISE
  • Raw dict with string keys used throughout where a `CLVEntry` dataclass would enforce required fields at write time.
  • No unit test path exists — ledger is both written and read by the same function, blocking isolation.

Consensus: All roles recommend revision.

━━━━━━━━━━━━━━━ SYNTHESIS ━━━━━━━━━━━━━━━

High-confidence (2+ reviewers):
- Single JSON ledger lacks atomicity — concurrent read/modify/write can corrupt the file
- Normalization runs after seeding, creating key mismatch between old and new entries

Other findings:
- compute_clv() undefined — corrupts all CLV values if wrong
- is_fade sign inversion missing — all fade bets get inverted CLV

Specificity: 14/14 findings concrete (100%) -> HIGH
Cost: $0.0051
```

What changed after this review: dropped the separate ledger entirely (Simplicity Challenger was right), defined `compute_clv()` with explicit US odds conversion, added `is_fade` sign inversion, replaced JSON rewrite with append-only writes + atomic rename.

---

## Quick start

**Via pip:**
```bash
pip install git+https://github.com/nhouseholder/deepseek-council.git
echo 'DEEPSEEK_API_KEY=your_key_here' >> ~/.plan-council/.env
deepseek-council --plan PLAN.md --council
```

**Via clone:**
```bash
git clone https://github.com/nhouseholder/deepseek-council
cd deepseek-council
bash setup.sh

# Add your DeepSeek API key (get one at platform.deepseek.com):
echo 'DEEPSEEK_API_KEY=your_key_here' >> ~/.plan-council/.env

# Check estimated cost:
python3 review.py --plan PLAN.md --discover

# Single review (1 model, up to 3 rounds):
python3 review.py --plan PLAN.md

# Full council (4 roles in parallel + synthesis, ~$0.003–$0.007):
python3 review.py --plan PLAN.md --council

# Include a git diff for additional context:
python3 review.py --plan PLAN.md --council --diff origin/main
```

---

## Provider support

Supported providers:

| Provider | Key env var | Input / Output (per 1M tokens) | Notes |
|----------|-------------|-------------------------------|-------|
| DeepSeek V4 Pro | `DEEPSEEK_API_KEY` | $0.27 / $1.10 | Default — cheapest quality-tier |
| Gemini 3 Flash (thinking) | `GEMINI_API_KEY` | free | Free tier, `thinking_level: high`; used for diversity roles |
| Gemini Flash | `GEMINI_API_KEY` | $0.15 / $0.60 | Auto-discovers latest model version |
| Gemini Pro | `GEMINI_API_KEY` | $1.25 / $10.00 | Best Gemini quality |
| GPT-4o Mini | `OPENAI_API_KEY` | $0.15 / $0.60 | OpenAI compatible |
| Claude Haiku | `ANTHROPIC_API_KEY` | $0.80 / $4.00 | Anthropic native |

Add any OpenAI-compatible API by adding an entry to `providers.json` with `"format": "openai"`.

---

## API key setup

**Option A — environment variable (easiest):**
```bash
export DEEPSEEK_API_KEY=your_key_here
python3 review.py --plan PLAN.md --council
```

**Option B — `~/.plan-council/.env`:**
```
DEEPSEEK_API_KEY=your_key_here
```
Run `bash setup.sh` to create this file with a template.

**Option C — Claude Code users:**
Already works if you have `~/.claude/credentials/master.env` with your key set there.

Priority order: environment variable > `~/.plan-council/.env` > `~/.claude/credentials/master.env`.

---

## Claude Code skill

deepseek-council ships with `contribution_report.py` — a `PreToolUse ExitPlanMode` hook that wires into Claude Code's native hook system.

**What it does:** When Claude Code tries to exit plan mode, the hook intercepts and requires you to choose:

```
Plan ready — how to proceed?
1. Exit & implement now
2. DeepSeek Council review (~$0.004–$0.007)
3. Ultra web review
```

If you pick option 2, the council runs, appends findings to `PLAN-REVIEW-LOG.md`, and clears the gate automatically. If the plan was revised after the review, that delta is reported before coding begins.

**Wire it in `~/.claude/settings.json`:**

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

Copy `SKILL.md` from the repo to `~/.claude/skills/deepseek-council.md` so the council is invokable as `/deepseek-council` from any Claude Code session.

**Or, invoke the council manually from `CLAUDE.md`:**

```
python3 /path/to/deepseek-council/review.py --plan PLAN.md --council
```

The `--json-output` flag emits hook-compatible JSON for programmatic use:

```json
{"continue": true, "agent_message": "council: REVISE — compute_clv() undefined (~$0.0051). Report -> PLAN-REVIEW-LOG.md"}
```

---

## GitHub Actions

Gate PRs that touch plan files behind a council review. Copy `docs/plan-review-action.yml` to `.github/workflows/plan-review.yml` in your repo.

Required secrets: `DEEPSEEK_API_KEY` + `GEMINI_API_KEY` (Gemini free tier works).

The action:
- Triggers on PRs that change `PLAN.md` or `plans/**.md`
- Runs `--council --diff origin/<base>` on each changed plan file
- Posts a verdict comment on the PR
- Fails the PR on MAJOR_REVISE

---

## `--diff` flag

Pass a git ref to include a redacted diff as reviewer context:

```bash
python3 review.py --plan PLAN.md --council --diff origin/main
```

The diff is appended to the plan excerpt (≤8000 chars). Common secret patterns (API keys, tokens, hex strings >32 chars) are redacted before sending to any external API.

---

## Output files

- `PLAN-REVIEW-LOG.md` — full council output, appended to the plan's directory after each run
- `~/.plan-council/reviewer-metrics.jsonl` — per-run metrics (cost, verdict, quality scores)

---

## Requirements

- Python 3.9+
- `curl`
- No other dependencies

---

## License

MIT

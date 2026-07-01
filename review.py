#!/usr/bin/env python3
"""
Provider-agnostic adversarial plan reviewer.
Supports DeepSeek, Gemini, OpenAI-compatible, and Anthropic API formats.
Auto-selects the cheapest enabled provider unless --provider is specified.

Usage:
  python3 review.py --plan PLAN.md
  python3 review.py --plan PLAN.md --council          # 4-role parallel council
  python3 review.py --plan PLAN.md --provider deepseek-v4-pro --rounds 2
  python3 review.py --plan PLAN.md --json-output      # hook-compatible JSON
  python3 review.py --plan PLAN.md --discover         # print provider/cost, exit
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date
from pathlib import Path

SKILL_DIR = Path(__file__).parent
PLAN_COUNCIL_DIR = Path.home() / ".plan-council"
PLAN_COUNCIL_ENV = PLAN_COUNCIL_DIR / ".env"
MASTER_ENV = Path.home() / ".claude" / "credentials" / "master.env"  # Claude Code compat
METRICS_LOG = PLAN_COUNCIL_DIR / "reviewer-metrics.jsonl"

REVIEW_PROMPT_TEMPLATE = """\
You are an adversarial plan reviewer. Find what breaks BEFORE implementation begins.

Round {round} review.

{stakes_header}
═══════════════ PLAN ═══════════════
{plan_content}
════════════════════════════════════

Rules:
- Be specific. "Consider edge cases" is useless — name the specific edge case.
- Focus on: data loss, security holes, race conditions, broken invariants, wrong assumptions, impossible-to-reverse mistakes, undefined behavior under load.
- Do not be generous. A plan that looks solid probably is — say so briefly and approve.

Return ONLY this structure:

## Critical Issues
[Specific, concrete flaws that must be fixed before coding. If none: "None identified."]

## Minor Issues
[Lower-priority gaps. If none: "None identified."]

## What's Solid
[One or two honest lines on what the plan gets right.]

VERDICT: APPROVED
— or —
VERDICT: REVISE
REASON: [single most important issue, one sentence]"""

COUNCIL_ROLES = [
    {
        "name": "Risk Analyst",
        "focus": "Find everything that can break, fail, or cause data loss. Irreversible steps, missing rollbacks, security gaps, race conditions, undefined behavior under load.",
        "verdict_scale": "APPROVED | REVISE | MAJOR_REVISE",
    },
    {
        "name": "Implementation Realist",
        "focus": "Identify what is ambiguous, unclear, or missing for someone executing this plan. Unclear step ordering, missing prerequisites, undefined success criteria, steps that reference unspecified files or functions.",
        "verdict_scale": "APPROVED | REVISE",
    },
    {
        "name": "Simplicity Challenger",
        "focus": "Challenge the complexity. Is this over-engineered? Is there a simpler path to the same outcome? Unnecessary abstractions, files, or steps? Scope creep that wasn't requested?",
        "verdict_scale": "APPROVED | REVISE",
    },
    {
        "name": "Senior Dev Realist",
        "focus": (
            "Evaluate whether this plan would produce production-quality, maintainable code "
            "if executed literally. Ask: Is this idiomatic for the target language/framework? "
            "Are multiple steps reinventing stdlib or existing utilities? Is the result "
            "testable — or does the plan create coupling that blocks unit testing? Would a "
            "code reviewer approve a PR that implements this exactly? Does this introduce "
            "tech debt (magic values, raw dicts where a dataclass fits, ad-hoc parsing)?"
        ),
        "verdict_scale": "APPROVED | REVISE",
    },
]

ROLE_PROMPT_TEMPLATE = """\
Project context:
{ctx_header}

═══════════════ PLAN ═══════════════
{plan_content}
════════════════════════════════════

You are the {role_name}. Your ONLY job: {role_focus}

Do NOT write a general review. Stay strictly in your lane.

Return ONLY this structure:

## {role_name} Findings
[3-5 bullet points. Each must be specific and concrete.
 If nothing to flag in your area: write "Nothing to flag in this area."]

VERDICT: {verdict_scale}
REASON: [your single most critical finding, one sentence. If APPROVED: "Plan looks solid from this perspective."]"""

SYNTHESIS_PROMPT_TEMPLATE = """\
You are the synthesis agent. Four specialist reviewers have independently analyzed this plan.
Consolidate their findings into a single actionable verdict.

══════════ ROLE REVIEWS ══════════
{role_outputs}
══════════════════════════

Consolidation rules:
- Issues in 2+ reviews = HIGH CONFIDENCE → must appear in final output
- Single-reviewer issues: include if specific and concrete, skip if vague
- VERDICT: APPROVED if no MAJOR_REVISE votes and ≤1 minor REVISE
            REVISE if 1+ REVISE votes with concrete actionable issues
            MAJOR_REVISE if any MAJOR_REVISE vote from any reviewer

Return ONLY this structure:

## Council Verdict
**High-confidence issues (2+ reviewers):** [bulleted list, or "None"]
**Other findings:** [top 2 concrete items, or "None"]
**What's solid:** [one honest line]

VERDICT: APPROVED | REVISE | MAJOR_REVISE
REASON: [most critical issue, or "Plan passes multi-perspective review" if APPROVED]"""

HEDGE_STARTERS = (
    "consider ", "maybe ", "perhaps ", "could be ", "might want",
    "it would be", "it's worth", "you should think about",
    "ensure that ", "make sure to ",
)


def append_metric(entry):
    try:
        PLAN_COUNCIL_DIR.mkdir(parents=True, exist_ok=True)
        with open(METRICS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def compute_plan_sha(plan_path):
    try:
        content = Path(plan_path).read_bytes()
        return hashlib.sha256(content).hexdigest()[:7]
    except OSError:
        return "unknown"


def load_env_var(key):
    # Env vars take priority — set them in your shell or CI environment
    val = os.environ.get(key)
    if val:
        return val
    # ~/.plan-council/.env (recommended for local setup)
    if PLAN_COUNCIL_ENV.is_file():
        for line in PLAN_COUNCIL_ENV.read_text().splitlines():
            if line.startswith(f"{key}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    # ~/.claude/credentials/master.env (Claude Code users)
    if MASTER_ENV.is_file():
        for line in MASTER_ENV.read_text().splitlines():
            if line.startswith(f"{key}="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    return None


def load_providers():
    config_path = SKILL_DIR / "providers.json"
    return json.loads(config_path.read_text())


def resolve_model_id(provider_name, cfg):
    model_id = cfg.get("model_id", "auto")
    if model_id != "auto":
        return model_id
    try:
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "discover.py"), "--pattern", cfg["model_pattern"]],
            capture_output=True, text=True, timeout=20
        )
        resolved = result.stdout.strip()
        if resolved and resolved != "No match":
            return resolved
    except Exception as e:
        print(f"[review] discover failed for {provider_name}: {e}", file=sys.stderr)
    return None


def get_api_key(cfg):
    key = load_env_var(cfg["api_key_env"])
    if not key and cfg.get("fallback_api_key_env"):
        key = load_env_var(cfg["fallback_api_key_env"])
    return key


def estimate_cost(plan_chars, cfg):
    est_input = plan_chars / 4
    est_output = 1500
    return (est_input / 1_000_000) * cfg["input_cost_per_1m"] + \
           (est_output / 1_000_000) * cfg["output_cost_per_1m"]


def select_provider(config, plan_chars, override=None):
    providers = config["providers"]
    candidates = []
    for name, cfg in providers.items():
        if not cfg.get("enabled", False):
            continue
        if override and name != override:
            continue
        key = get_api_key(cfg)
        if not key:
            if override:
                print(f"[review] No API key for {name} (check {cfg['api_key_env']})", file=sys.stderr)
            continue
        model_id = resolve_model_id(name, cfg)
        if not model_id:
            print(f"[review] Could not resolve model ID for {name}", file=sys.stderr)
            continue
        cost = estimate_cost(plan_chars, cfg)
        candidates.append((name, model_id, key, cfg, cost))
    if not candidates:
        return None, None, None, None, None
    mode = config.get("selection", {}).get("mode", "cheapest")
    if mode == "cheapest":
        candidates.sort(key=lambda x: x[4])
    name, model_id, key, cfg, cost = candidates[0]
    return name, model_id, key, cfg, cost


def build_payload(fmt, model_id, prompt, cfg=None):
    # ponytail: per-provider max_output_tokens caps runaway billing; thinking models need a higher value in providers.json
    max_tok = cfg.get("max_output_tokens", 4096) if cfg else 4096
    if fmt == "gemini":
        gen_config: dict = {"temperature": 0.3, "maxOutputTokens": max_tok}
        if cfg and cfg.get("thinking_level"):
            gen_config["thinkingConfig"] = {"thinkingLevel": cfg["thinking_level"]}
        return json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_config})
    elif fmt == "openai":
        return json.dumps({"model": model_id, "max_tokens": max_tok, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3})
    elif fmt == "anthropic":
        return json.dumps({"model": model_id, "max_tokens": max_tok, "messages": [{"role": "user", "content": prompt}]})
    raise ValueError(f"Unknown format: {fmt}")


def call_api(fmt, api_key, model_id, prompt, cfg):
    payload = build_payload(fmt, model_id, prompt)
    if fmt == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
        headers = ["-H", "Content-Type: application/json"]
    elif fmt == "openai":
        url = f"{cfg['api_base']}/chat/completions"
        headers = ["-H", "Content-Type: application/json", "-H", f"Authorization: Bearer {api_key}"]
    elif fmt == "anthropic":
        url = f"{cfg['api_base']}/messages"
        headers = [
            "-H", "Content-Type: application/json",
            "-H", f"x-api-key: {api_key}",
            "-H", "anthropic-version: 2023-06-01",
        ]
    else:
        return None, f"Unknown format: {fmt}"

    result = subprocess.run(
        ["curl", "-s", "--max-time", "30", "-X", "POST", url] + headers + ["-d", payload],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, f"curl error (exit {result.returncode})"
    try:
        data = json.loads(result.stdout)
    except Exception:
        return None, f"JSON parse error: {result.stdout[:200]}"
    if "error" in data:
        err = data["error"]
        return None, f"API error {err.get('code') or err.get('type') or ''}: {err.get('message') or str(err)}"
    try:
        if fmt == "gemini":
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        elif fmt == "openai":
            text = data["choices"][0]["message"]["content"]
        elif fmt == "anthropic":
            # DeepSeek v4-pro prepends a thinking block; find the first text block
            text_block = next((b for b in data["content"] if b.get("type") == "text"), None)
            if text_block is None:
                raise KeyError("no text-type content block in response")
            text = text_block["text"]
        else:
            return None, f"Unknown format for response parsing: {fmt}"
        return text, None
    except (KeyError, IndexError) as e:
        return None, f"Unexpected response structure: {e} — {result.stdout[:300]}"


def parse_verdict(text):
    reason = ""
    for line in text.splitlines():
        if line.strip().upper().startswith("REASON:"):
            reason = line.strip()[7:].strip()
            break
    for line in text.splitlines():
        u = line.upper()
        if "VERDICT" not in u:
            continue
        if "MAJOR_REVISE" in u:
            return "MAJOR_REVISE", reason
        if "REVISE" in u:
            return "REVISE", reason
        if "APPROVED" in u:
            return "APPROVED", ""
    return "UNKNOWN", ""


def run_review(plan_path, provider_override=None, max_rounds=3, json_output=False, discover_only=False, silent=False):
    plan_file = Path(plan_path)
    if not plan_file.is_file():
        msg = f"Plan file not found: {plan_path}"
        if json_output:
            print(json.dumps({"continue": True, "agent_message": f"⚠️ plan-review: {msg}"}))
        else:
            print(msg, file=sys.stderr)
        return False

    plan_content = plan_file.read_text(encoding="utf-8")
    plan_excerpt = plan_content[:12000] + ("\n\n[...truncated...]" if len(plan_content) > 12000 else "")

    config = load_providers()
    pname, model_id, api_key, pcfg, cost = select_provider(config, len(plan_content), provider_override)

    if not pname:
        msg = "No available provider (check API keys and providers.json)"
        if json_output:
            print(json.dumps({"continue": True, "agent_message": f"⚠️ plan-review: {msg}"}))
        else:
            print(msg, file=sys.stderr)
        return False

    assert pcfg is not None  # guaranteed when pname is set

    if not silent:
        print(f"[plan-review] Selected: {pname} / {model_id} — estimated cost: ${cost:.4f}")
    if discover_only:
        return True

    HIGH_STAKES_PATTERNS = [
        (r"auth(?:entication)?|oauth|jwt|session\s+token|password|credential", "auth/credentials"),
        (r"schema|migration|alter\s+table|create\s+table|drop\s+table|database", "schema/migration"),
        (r"payment|stripe|billing|subscription|checkout|webhook", "payments"),
        (r"security|permission|rbac|acl|role.based|access.control", "security/permissions"),
        (r"breaking[\s-]change|backward.compat|deprecat", "breaking change"),
        (r"production\s+deploy|deploy\s+to\s+prod|rollout|release", "production deploy"),
        (r"multi.?file\s+refactor|cross.?service|distributed", "multi-system change"),
    ]
    content_lower = plan_content.lower()
    matched = [label for pat, label in HIGH_STAKES_PATTERNS if re.search(pat, content_lower)]
    stakes_header = f"High-stakes categories detected: {', '.join(matched)}" if matched else ""

    log_path = plan_file.parent / "PLAN-REVIEW-LOG.md"
    if not log_path.is_file():
        log_path.write_text("# Plan Review Log\n\n")
    else:
        with open(log_path, "a") as f:
            f.write("\n---\n")

    last_verdict = "UNKNOWN"
    last_reason = ""

    for round_n in range(1, max_rounds + 1):
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            round=round_n, stakes_header=stakes_header, plan_content=plan_excerpt
        )
        if not silent:
            print(f"[plan-review] Round {round_n}/{max_rounds}...", end=" ", flush=True)
        text, err = call_api(pcfg["format"], api_key, model_id, prompt, pcfg)
        if err:
            msg = f"API call failed: {err}"
            if json_output:
                print(json.dumps({"continue": True, "agent_message": f"⚠️ plan-review: {msg}"}))
            else:
                print(f"\n[plan-review] Error: {msg}", file=sys.stderr)
            return False

        verdict, reason = parse_verdict(text)
        last_verdict, last_reason = verdict, reason
        if not silent:
            print(f"{verdict}" + (f" — {reason}" if reason else ""))

        log_entry = f"\n## Round {round_n} — {pname}/{model_id} (${cost:.4f})\n**Verdict:** {verdict}\n"
        if reason:
            log_entry += f"**Reason:** {reason}\n"
        log_entry += f"\n{text}\n"
        with open(log_path, "a") as f:
            f.write(log_entry)

        if verdict == "APPROVED":
            break
        if round_n < max_rounds and verdict == "REVISE":
            if not silent:
                print(f"[plan-review] Revising for round {round_n + 1}...")

    if json_output:
        categories_str = ", ".join(matched) if matched else "general"
        if last_verdict == "APPROVED":
            msg = f"✅ plan-review: APPROVED ({pname}/{model_id}, ${cost:.4f}) — {categories_str}. Full review -> PLAN-REVIEW-LOG.md"
        elif last_verdict == "REVISE":
            msg = f"⚠️ plan-review: REVISE — {last_reason or categories_str} ({pname}/{model_id}). Full review -> PLAN-REVIEW-LOG.md"
        else:
            msg = f"⚠️ plan-review: no clear verdict after {max_rounds} round(s). Check PLAN-REVIEW-LOG.md"
        print(json.dumps({"continue": True, "agent_message": msg}))
    else:
        if not silent:
            status = "✓ APPROVED" if last_verdict == "APPROVED" else "⚠ REVISE"
            print(f"\n{status} — {pname}/{model_id} — ${cost:.4f} — PLAN-REVIEW-LOG.md updated")

    return last_verdict == "APPROVED"


def get_project_context(plan_path):
    plan_dir = plan_path.parent
    cwd = Path.cwd()
    home = Path.home()

    _generic = {"plans", ".plans", ".claude", "claude", "commands", "skills"}
    project_name = cwd.name if cwd.name not in _generic else plan_path.stem
    lines = [
        f"Project: {project_name}  (cwd: {cwd}  plan: {plan_path.name})\n",
        "Context for council: you are reviewing an implementation plan for the project above.\n",
    ]

    def _read_capped(path, cap, label):
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
        suffix = "\n[...truncated...]" if len(content) > cap else ""
        return f"# {label}\n{content[:cap]}{suffix}\n"

    # 1. Global rules — check multiple locations in priority order, use first found
    global_candidates = [
        (home / ".claude" / "CLAUDE.md", 5000, "Global rules (~/.claude/CLAUDE.md)"),
        (home / "CLAUDE.md", 2000, "Global rules (~/CLAUDE.md)"),
        (home / "AGENTS.md", 2000, "Global rules (~/AGENTS.md)"),
        (home / ".cursorrules", 2000, "Global rules (~/.cursorrules)"),
    ]
    for path, cap, label in global_candidates:
        block = _read_capped(path, cap, label)
        if block:
            lines.append(block)
            break

    # 2. Project CLAUDE.md / AGENTS.md
    seen = set()
    for candidate in [plan_dir / "CLAUDE.md", cwd / "CLAUDE.md", plan_dir / "AGENTS.md", cwd / "AGENTS.md"]:
        if candidate.is_file() and candidate.resolve() not in seen:
            seen.add(candidate.resolve())
            block = _read_capped(candidate, 2000, f"Project rules ({candidate.name})")
            if block:
                lines.append(block)
            break

    # 3. Project KIMI.md — operational rules (project-level only; global ~/KIMI.md is too large)
    for candidate in [plan_dir / "KIMI.md", cwd / "KIMI.md"]:
        if candidate.is_file():
            block = _read_capped(candidate, 1200, f"Project ops ({candidate.name})")
            if block:
                lines.append(block)
            break

    # 4. BETTING_SYSTEM_RULES.md — sports qualification gates (auto-detect, skipped if absent)
    for candidate in [
        plan_dir / "BETTING_SYSTEM_RULES.md",
        cwd / "BETTING_SYSTEM_RULES.md",
        home / ".kimi" / "BETTING_SYSTEM_RULES.md",
    ]:
        if candidate.is_file():
            block = _read_capped(candidate, 2000, "Betting system rules (BETTING_SYSTEM_RULES.md)")
            if block:
                lines.append(block)
            break

    # 5. Newest handoff — recent session decisions and context
    handoffs_dir = cwd / "handoffs"
    if handoffs_dir.is_dir():
        handoff_files = sorted(
            [f for f in handoffs_dir.iterdir() if f.is_file() and f.suffix == ".md"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if handoff_files:
            block = _read_capped(handoff_files[0], 1500, f"Latest handoff ({handoff_files[0].name})")
            if block:
                lines.append(block)

    # 6. tasks/todo.md — current work state
    block = _read_capped(cwd / "tasks" / "todo.md", 500, "Current tasks (tasks/todo.md)")
    if block:
        lines.append(block)

    # 7. Recent commits
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            capture_output=True, text=True, cwd=str(cwd), timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines.append(f"Recent commits:\n{result.stdout.strip()}\n")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 8. Files referenced in the plan — inline first 60 lines so council sees what exists
    plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    referenced = re.findall(r'`([^`]+\.(py|ts|js|json|sh|md|yaml|yml|toml|cfg|env))`', plan_text)
    seen_files = set()
    for fname, _ in referenced:
        if fname in seen_files:
            continue
        seen_files.add(fname)
        for search_root in [plan_dir, cwd]:
            candidate = search_root / fname
            if candidate.is_file():
                snippet_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()[:60]
                lines.append(
                    f"# {fname} (first {len(snippet_lines)} lines — exists in project)\n"
                    + "\n".join(snippet_lines)
                    + "\n"
                )
                break

    return "\n".join(lines)


def _extract_findings(text):
    findings = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "• ")):
            content = line[2:].strip()
            if content and "Nothing to flag" not in content:
                findings.append(content)
    return findings


def _is_specific(finding):
    f = finding.lower()
    if re.search(r'\w+\.(py|js|ts|json|md|sh|yaml|yml|env|cfg|toml)', f):
        return True
    if re.search(r'`[^`]+`|step \d+|line \d+', f):
        return True
    if any(f.startswith(h) for h in HEDGE_STARTERS):
        return False
    return len(finding.split()) >= 12


def format_debate_report(role_results):
    bar = "━" * 15
    lines = ["", f"{bar} COUNCIL DEBATE {bar}", ""]
    all_findings = {name: _extract_findings(text) for name, text in role_results.items()}
    all_verdicts = {name: parse_verdict(text) for name, text in role_results.items()}

    for name in role_results:
        verdict, _ = all_verdicts[name]
        top_two = all_findings[name][:2]
        lines.append(f"{name} — {verdict}")
        if top_two:
            for f in top_two:
                lines.append(f"  • {f[:140]}")
        else:
            lines.append("  • (no findings extracted)")
        lines.append("")

    check_terms = [
        "rollback", "race condition", "timeout",
        "authentication", "authorization",
        "over-engineer", "complex", "prerequisite", "undefined",
    ]
    role_names = list(all_findings.keys())
    shared = []
    for i in range(len(role_names)):
        for j in range(i + 1, len(role_names)):
            t1 = " ".join(all_findings[role_names[i]]).lower()
            t2 = " ".join(all_findings[role_names[j]]).lower()
            shared.extend(t for t in check_terms if t in t1 and t in t2)

    if shared:
        lines.append(f"Agreement: {', '.join(dict.fromkeys(shared))}")

    approved = [n for n in role_names if all_verdicts[n][0] == "APPROVED"]
    revising = [n for n in role_names if all_verdicts[n][0] != "APPROVED"]
    if approved and revising:
        lines.append(f"Divergence: {', '.join(approved)} approved; {', '.join(revising)} want revision.")
    elif not approved:
        lines.append("Consensus: All roles recommend revision.")
    else:
        lines.append("Consensus: All roles approved.")

    lines += ["", f"{bar} SYNTHESIS {bar}"]
    return "\n".join(lines)


def run_meta_judge(role_results, pcfg, api_key, model_id):
    all_text = "\n\n".join(f"=== {name} ===\n{text}" for name, text in role_results.items())
    prompt = (
        "You are evaluating the quality of adversarial plan review findings.\n\n"
        "Rate each bullet-point finding on a 1-3 scale:\n"
        "1 = Boilerplate: generic concern with no plan-specific detail "
        '(e.g., "missing error handling" without naming where)\n'
        "2 = Specific but expected: concrete, a careful developer would likely catch it\n"
        "3 = Non-obvious insight: would realistically slip through without adversarial review\n\n"
        f"Findings to rate:\n{all_text}\n\n"
        "Return ONLY a valid JSON array — no prose before or after:\n"
        '[{"finding": "<first 60 chars of finding>", "score": 1, "reason": "<one sentence>"}]'
    )
    text, err = call_api(pcfg["format"], api_key, model_id, prompt, pcfg)
    if err or not text:
        return None
    # greedy match so the full array is captured even when findings contain "]"
    match = re.search(r'\[[\s\S]*\]', text)
    if not match:
        return None
    try:
        scores = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not scores:
        return None
    total = len(scores)
    avg = sum(s.get("score", 1) for s in scores) / total
    pct_high = sum(1 for s in scores if s.get("score") == 3) / total * 100
    return {
        "scores": scores,
        "avg": round(avg, 2),
        "pct_high": round(pct_high),
        "n": total,
        "findings": [s.get("finding", "") for s in scores],
    }


def generate_council_report(role_results, synthesis_verdict, synthesis_reason, total_cost, role_providers=None):
    all_findings = {name: _extract_findings(text) for name, text in role_results.items()}
    all_verdicts = {name: parse_verdict(text) for name, text in role_results.items()}

    total_f = sum(len(f) for f in all_findings.values())
    specific_f = sum(1 for findings in all_findings.values() for f in findings if _is_specific(f))
    specificity_pct = (specific_f / total_f * 100) if total_f else 0
    quality = "HIGH" if specificity_pct >= 60 else ("MEDIUM" if specificity_pct >= 30 else "LOW")

    role_names = list(all_findings.keys())
    agreement_hints = []
    check_terms = [
        "rollback", "error handling", "ambiguous", "unclear", "missing",
        "race condition", "timeout", "authentication", "authorization",
        "over-engineer", "complex", "prerequisite", "undefined",
    ]
    for i in range(len(role_names)):
        for j in range(i + 1, len(role_names)):
            t1 = " ".join(all_findings[role_names[i]]).lower()
            t2 = " ".join(all_findings[role_names[j]]).lower()
            shared = [t for t in check_terms if t in t1 and t in t2]
            if shared:
                agreement_hints.append(f"{role_names[i][:4]}+{role_names[j][:4]}: {', '.join(shared[:2])}")

    votes = [v for v, _ in all_verdicts.values()]
    lines = [
        "## Council Performance Report", "",
        "### Role Verdicts",
        "| Role | Model | Verdict | Findings | Key Concern |",
        "|------|-------|---------|----------|-------------|",
    ]
    for name in role_results:
        v, r = all_verdicts[name]
        n = len(all_findings[name])
        # Prefer REASON; fall back to top finding so the column is never blank
        top_finding = all_findings[name][0] if all_findings[name] else ""
        key = (r or top_finding or "—")[:75]
        model_tag = (role_providers or {}).get(name, "—")
        lines.append(f"| {name} | {model_tag} | {v} | {n} | {key} |")

    # Per-role key contributions (top 2 findings per role, below the table)
    lines += ["", "**Key contributions per role:**"]
    for name in role_results:
        top = all_findings[name][:2]
        if top:
            lines.append(f"- **{name}:** {top[0][:120]}")
            if len(top) > 1:
                lines.append(f"  — {top[1][:120]}")

    lines += [
        "",
        "### Quality Signals",
        f"- **Specificity:** {specific_f}/{total_f} findings are concrete ({specificity_pct:.0f}%) -> **{quality}**",
        f"- **Votes:** {votes.count('MAJOR_REVISE')} MAJOR_REVISE · {votes.count('REVISE')} REVISE · {votes.count('APPROVED')} APPROVED",
    ]
    if agreement_hints:
        lines.append(f"- **Cross-role agreement:** {'; '.join(agreement_hints)}")
    else:
        lines.append("- **Cross-role agreement:** None detected — reviewers raised independent concerns")

    lines += [
        f"- **Synthesis:** {synthesis_verdict}" + (f" — {synthesis_reason}" if synthesis_reason else ""),
        f"- **Cost:** ${total_cost:.4f}",
        "",
    ]

    if synthesis_verdict == "APPROVED":
        guidance = "Plan passes all perspectives. Proceed to implementation."
    elif synthesis_verdict == "MAJOR_REVISE":
        guidance = "Serious issues — revise plan before coding."
    elif synthesis_verdict == "REVISE" and quality == "HIGH":
        guidance = "Concrete issues found. Address them before implementing."
    elif synthesis_verdict == "REVISE" and quality == "MEDIUM":
        guidance = "Mixed quality. Review findings in PLAN-REVIEW-LOG.md and judge each one."
    elif synthesis_verdict == "REVISE":
        guidance = "REVISE verdict but findings are mostly vague. Model may be over-cautious — use judgment."
    else:
        guidance = "Inconclusive. Review full output in PLAN-REVIEW-LOG.md."

    lines.append(f"### Recommendation\n**{guidance}**")

    # Enumerate all unique findings with checkboxes for the implementing agent to triage
    lines += [
        "",
        "## Claude Assessment (Required)",
        "For each finding below: **Accept** (address in plan) or **Reject** (with one-line rationale).",
        "Apply accepted findings to the plan; mark rejected ones `<!-- rejected: reason -->`.",
        "",
    ]
    seen_f = set()
    for name in role_results:
        role_label_printed = False
        for f in all_findings[name]:
            dedup_key = f[:60].lower()
            if dedup_key in seen_f:
                continue
            seen_f.add(dedup_key)
            if not role_label_printed:
                lines.append(f"**{name}:**")
                role_label_printed = True
            lines.append(f"- [ ] {f[:120]}")
        if role_label_printed:
            lines.append("")

    return "\n".join(lines)


def prepend_council_summary_to_plan(plan_file, role_results, synthesis_verdict, synthesis_reason, pname, model_id, total_cost, role_providers=None):
    """Prepend a compact council verdict table to the top of the plan file."""
    all_verdicts = {name: parse_verdict(text) for name, text in role_results.items()}
    all_findings = {name: _extract_findings(text) for name, text in role_results.items()}

    today = str(date.today())
    lines = [
        f"<!-- council-review: {today} | {pname}/{model_id} | ~${total_cost:.4f} -->",
        "",
        f"## Council Review — {today}",
        "",
        "| Role | Verdict | Key Finding |",
        "|------|---------|-------------|",
    ]
    for name in role_results:
        v, r = all_verdicts[name]
        top = all_findings[name][0] if all_findings[name] else "—"
        key = (r or top)[:80]
        model_tag = (role_providers or {}).get(name, "")
        name_cell = f"{name} [{model_tag}]" if model_tag else name
        lines.append(f"| {name_cell} | {v} | {key} |")

    synthesis_key = (synthesis_reason or "Plan passes multi-perspective review")[:80]
    lines.append(f"| **Synthesis** | **{synthesis_verdict}** | {synthesis_key} |")
    lines += ["", "---", ""]

    header = "\n".join(lines) + "\n"
    try:
        existing = plan_file.read_text(encoding="utf-8")
        plan_file.write_text(header + existing, encoding="utf-8")
    except OSError as e:
        print(f"[council] Could not prepend summary to plan: {e}", file=sys.stderr)


def run_council(plan_path, provider_override=None, json_output=False, silent=False, discover_only=False):
    """4-role parallel council review + synthesis. ~3x cost of a single review."""
    plan_file = Path(plan_path)
    if not plan_file.is_file():
        msg = f"Plan file not found: {plan_path}"
        if json_output:
            print(json.dumps({"continue": True, "agent_message": f"⚠️ council: {msg}"}))
        else:
            print(msg, file=sys.stderr)
        return False

    plan_content = plan_file.read_text(encoding="utf-8")
    plan_excerpt = plan_content[:12000] + ("\n\n[...truncated...]" if len(plan_content) > 12000 else "")
    ctx_header = get_project_context(plan_file)

    config = load_providers()
    pname, model_id, api_key, pcfg, unit_cost = select_provider(config, len(plan_content), provider_override)

    if not pname:
        msg = "No available provider (check API keys and providers.json)"
        if json_output:
            print(json.dumps({"continue": True, "agent_message": f"⚠️ council: {msg}"}))
        else:
            print(msg, file=sys.stderr)
        return False

    assert pcfg is not None and unit_cost is not None  # guaranteed when pname is set

    # Pre-resolve all role providers serially to warm cache and avoid concurrent auto-discovery
    role_provider_map = config.get("council_role_providers", {})
    resolved_role_providers: dict = {}
    for _role in COUNCIL_ROLES:
        _role_pname = role_provider_map.get(_role["name"])
        if _role_pname:
            _role_pcfg = config["providers"].get(_role_pname)
            if _role_pcfg:
                _role_key = get_api_key(_role_pcfg)
                _role_model = resolve_model_id(_role_pname, _role_pcfg)
                if _role_key and _role_model:
                    resolved_role_providers[_role["name"]] = (_role_pname, _role_model, _role_key, _role_pcfg)
                else:
                    print(
                        f"[council] WARNING: Role '{_role['name']}' provider '{_role_pname}' unavailable"
                        f" — falling back to default '{pname}'",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"[council] WARNING: Role '{_role['name']}' provider '{_role_pname}' not found"
                    f" — falling back to default '{pname}'",
                    file=sys.stderr,
                )

    if not silent:
        est_council = unit_cost * 3.0  # 4 roles + synthesis + meta-judge (rough estimate)
        distinct = list(dict.fromkeys(
            resolved_role_providers.get(r["name"], (pname,))[0] for r in COUNCIL_ROLES
        ))
        provider_str = "/".join(distinct) if distinct else pname
        print(f"[council] {provider_str} — {len(COUNCIL_ROLES)} roles in parallel... est. cost: ~${est_council:.4f}")
    if discover_only:
        return True

    # Advisory budget cap — estimate uses 1500 output tokens; true worst-case at max_output_tokens=4096 is ~2.7× higher
    cap = config.get("council_budget_usd", 1.0)
    if est_council > cap:
        msg = f"council skipped — est ${est_council:.4f} exceeds cap ${cap:.2f} (raise council_budget_usd in providers.json to override)"
        if json_output:
            print(json.dumps({"continue": True, "agent_message": f"⚠️ {msg}"}))
        else:
            print(f"[council] ⚠️ {msg}", file=sys.stderr)
        return False

    plan_entities = _extract_plan_entities(plan_excerpt)


    def _call_role(role):
        r_pname, r_model, r_key, r_pcfg = resolved_role_providers.get(
            role["name"], (pname, model_id, api_key, pcfg)
        )
        prompt = ROLE_PROMPT_TEMPLATE.format(
            ctx_header=ctx_header,
            plan_content=plan_excerpt,
            role_name=role["name"],
            role_focus=role["focus"],
            verdict_scale=role["verdict_scale"],
        )
        text, err = call_api(r_pcfg["format"], r_key, r_model, prompt, r_pcfg)
        if err:
            print(f"[council] {role['name']}: retry after error — {err}", file=sys.stderr)
            text, err = call_api(r_pcfg["format"], r_key, r_model, prompt, r_pcfg)
        return role["name"], r_pname, text, err

    role_results = {}
    role_providers_used = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_call_role, role): role for role in COUNCIL_ROLES}
        done, not_done = wait(futures, timeout=90)
        for future in not_done:
            future.cancel()
            role = futures[future]
            if not silent:
                print(f"[council] {role['name']}: timed out after 90s", file=sys.stderr)
            role_results[role["name"]] = (
                f"## {role['name']} Findings\n[Timeout]\n\n"
                "VERDICT: UNKNOWN\nREASON: API call timed out"
            )
            role_providers_used[role["name"]] = pname
        for future in done:
            name, r_pname, text, err = future.result()
            role_providers_used[name] = r_pname
            if err:
                if not silent:
                    print(f"[council] {name}: error — {err}", file=sys.stderr)
                role_results[name] = f"## {name} Findings\n[Error: {err}]\n\nVERDICT: UNKNOWN\nREASON: API error"
            else:
                verdict, _ = parse_verdict(text)
                if not silent:
                    provider_tag = f" [{r_pname}]" if r_pname != pname else ""
                    print(f"[council] {name}{provider_tag}: {verdict}")
                role_results[name] = text

    if not silent and not json_output:
        print(format_debate_report(role_results))

    if not silent:
        print("[council] Synthesizing...", end=" ", flush=True)

    role_outputs = "\n\n---\n\n".join(f"### {name}\n{text}" for name, text in role_results.items())
    synthesis_text, synthesis_err = call_api(
        pcfg["format"], api_key, model_id,
        SYNTHESIS_PROMPT_TEMPLATE.format(role_outputs=role_outputs),
        pcfg
    )

    if synthesis_err:
        role_verdicts = [parse_verdict(t)[0] for t in role_results.values()]
        fallback = "MAJOR_REVISE" if "MAJOR_REVISE" in role_verdicts else ("REVISE" if "REVISE" in role_verdicts else "APPROVED")
        synthesis_text = (
            f"## Council Verdict\n[Synthesis failed: {synthesis_err}]\n\n"
            f"VERDICT: {fallback}\nREASON: Synthesis error — check role findings above"
        )

    final_verdict, final_reason = parse_verdict(synthesis_text)
    if not silent:
        print(f"{final_verdict}" + (f" — {final_reason}" if final_reason else ""))

    total_cost = unit_cost * 3.0
    report = generate_council_report(role_results, final_verdict, final_reason, total_cost, role_providers=role_providers_used)

    prepend_council_summary_to_plan(
        plan_file, role_results, final_verdict, final_reason,
        pname, model_id, total_cost, role_providers=role_providers_used
    )

    meta = run_meta_judge(role_results, pcfg, api_key, model_id)
    if meta:
        meta_lines = [
            "\n### Meta-Judge Quality Scores",
            "| Finding | Score | Reason |",
            "|---------|-------|--------|",
        ]
        for s in meta["scores"]:
            meta_lines.append(f"| {s.get('finding', '')[:50]} | {s.get('score')} | {s.get('reason', '')[:80]} |")
        meta_lines.append(
            f"\n**Avg quality: {meta['avg']} / 3.0 · "
            f"High-value findings (score=3): {meta['pct_high']}% of {meta['n']}**"
        )
        report += "\n\n" + "\n".join(meta_lines)

    log_path = plan_file.parent / "PLAN-REVIEW-LOG.md"
    if not log_path.is_file():
        log_path.write_text("# Plan Review Log\n\n")
    else:
        with open(log_path, "a") as f:
            f.write("\n---\n")

    log_entry = f"\n## Council Review — {pname}/{model_id} (est. ${total_cost:.4f})\n"
    for name, text in role_results.items():
        log_entry += f"\n### {name}\n{text}\n"
    log_entry += f"\n### Synthesis\n{synthesis_text}\n\n{report}\n"
    with open(log_path, "a") as f:
        f.write(log_entry)

    if json_output:
        if final_verdict == "APPROVED":
            msg = f"✅ council: APPROVED ({pname}/{model_id}, ~${total_cost:.4f}). Report -> PLAN-REVIEW-LOG.md"
        elif final_verdict in ("REVISE", "MAJOR_REVISE"):
            msg = f"⚠️ council: {final_verdict} — {final_reason} (~${total_cost:.4f}). Report -> PLAN-REVIEW-LOG.md"
        else:
            msg = "⚠️ council: inconclusive — check PLAN-REVIEW-LOG.md"
        print(json.dumps({"continue": True, "agent_message": msg}))
    else:
        if not silent:
            print(f"\n{report}")

    # Record plan SHA for influence tracking
    plan_sha = compute_plan_sha(plan_path)
    sha_note = f"\n**Plan SHA at review:** {plan_sha}  (recorded for influence tracking)\n"
    with open(log_path, "a") as f:
        f.write(sha_note)

    today = str(date.today())
    append_metric({
        "date": today,
        "plan": Path(plan_path).name,
        "path": str(Path(plan_path).resolve()),
        "cwd": str(Path.cwd()),
        "type": "influence-pending",
        "sha_at_review": plan_sha,
    })
    if meta:
        append_metric({
            "date": today,
            "plan": Path(plan_path).name,
            "type": "quality",
            "avg": meta["avg"],
            "pct_high": meta["pct_high"],
            "n": meta["n"],
            "findings": meta["findings"],
        })

    return final_verdict == "APPROVED"


def main():
    parser = argparse.ArgumentParser(description="Provider-agnostic adversarial plan reviewer")
    parser.add_argument("--plan", required=True, help="Path to plan file")
    parser.add_argument("--provider", help="Override provider name from providers.json")
    parser.add_argument("--rounds", type=int, default=3, help="Max review rounds (default 3)")
    parser.add_argument("--json-output", action="store_true", help="Output hook-compatible JSON")
    parser.add_argument("--discover", action="store_true", help="Print selected provider/model/cost, then exit")
    parser.add_argument("--silent", action="store_true", help="Suppress progress output")
    parser.add_argument("--council", action="store_true", help="4-role parallel council review (~3x cost)")
    args = parser.parse_args()

    if args.council:
        ok = run_council(
            plan_path=args.plan, provider_override=args.provider,
            json_output=args.json_output, silent=args.silent, discover_only=args.discover,
        )
    else:
        ok = run_review(
            plan_path=args.plan, provider_override=args.provider,
            max_rounds=args.rounds, json_output=args.json_output,
            discover_only=args.discover, silent=args.silent,
        )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

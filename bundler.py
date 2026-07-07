"""Automated AI Bundler.

Turns a natural-language (or structured JSON) description of an AI agent, swarm,
or workflow into a self-contained **Bundle** — a package of optimized prompts,
skills/tools, target-specific configs, examples, and deploy helpers that can be
dropped into Claude Code, Cursor, or any generic runtime.

The whole process runs as a streamed job on top of the existing swarm session
primitives (in-memory session dict + per-session asyncio.Queue), so a client can
watch the deliberation and file generation live over SSE, exactly like a swarm
deliberation.

Pipeline
--------
1. normalize   — spec (NL or JSON) → structured BundleSpec        (Phoebe / JSON)
2. deliberate  — Nova/Loom/Claire/Cipher critique in bundler mode (fleet, streamed)
3. refine      — Phoebe merges critiques → final blueprint        (Phoebe / JSON)
4. optimize    — Claire writes the optimized main system prompt   (premium)
5. generate    — deterministic per-target file builders
6. package     — in-memory zip + manifest, downloadable

Adding a new output target is a single entry in ``TARGET_BUILDERS``.
"""

import asyncio
import io
import json
import re
import zipfile
from datetime import datetime, timezone

import yaml

from agents import (
    AGENT_MAP,
    CREW,
    roster,
    VALID_MODES,
    ORCHESTRATOR,
    PROMPTER,
    PACKAGER,
    SAFETY,
)
from llm import agent_turn, extract_json
import store

BUNDLER_VERSION = "1.0.0"

# Targets we know how to emit. A client may request a subset; unknown targets
# are ignored. Keep this in sync with TARGET_BUILDERS below.
DEFAULT_TARGETS = ["claude-code", "cursor", "windsurf", "config"]
DEFAULT_DEPLOYMENTS = ["docker", "zeabur"]
# "langgraph" is registered and available on request; kept out of defaults so a
# typical bundle stays lean.

# Bundling runs in two modes (roster is built by agents.roster):
#   * "full" — the crew deliberates; you choose how many agents via ``size``
#   * "lite" — a fixed, essential subset (faster, cheaper)


# --------------------------------------------------------------------------- #
# Bundler agent personas
#
# The full 20-agent hive deliberates in bundler mode. Each agent's base persona
# (voice, brevity) is preserved and extended with its own ``focus`` line from
# the roster, so we never hardcode per-name logic — adding a hive agent is a
# single entry in agents.py.
# --------------------------------------------------------------------------- #
BUNDLER_PREFIX = (
    "BUNDLER MODE: You are part of a crew packaging an AI system "
    "(agent/swarm/workflow) as a drop-in bundle for other developers."
)


def bundler_system(name: str) -> str:
    """Persona + bundler focus for the given agent."""
    agent = AGENT_MAP[name]
    focus = agent.get("focus", "")
    return f"{agent['system']}\n\n{BUNDLER_PREFIX} Your focus: {focus}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """Filesystem-safe slug: lowercase, hyphens, no leading/trailing junk."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower())
    return s.strip("-") or "bundle"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Blueprint normalization / defaults
#
# The blueprint is the single source of truth for file generation. Every field
# is defaulted so generation never fully fails even if a model returns partial
# JSON. Shape:
#   {
#     name, slug, version, description, tagline, license,
#     agents:   [{name, role, persona, model?, tools:[str]}],
#     skills:   [{name, slug, description, instructions, example?}],
#     system_prompt: str,
#     targets:  [str], deployments: [str],
#     examples: [{name, input, expected}],
#   }
# --------------------------------------------------------------------------- #
def normalize_blueprint(raw: dict, spec_text: str) -> dict:
    """Coerce a (possibly partial) model blueprint into a complete, safe dict."""
    raw = raw or {}

    name = (raw.get("name") or "Custom AI Bundle").strip()
    slug = slugify(raw.get("slug") or name)

    agents = []
    for a in raw.get("agents") or []:
        if not isinstance(a, dict):
            continue
        aname = (a.get("name") or "Agent").strip()
        agents.append(
            {
                "name": aname,
                "role": (a.get("role") or "Agent").strip(),
                "persona": (a.get("persona") or a.get("description") or "").strip(),
                "model": (a.get("model") or "").strip(),
                "tools": [str(t) for t in (a.get("tools") or []) if t],
            }
        )
    if not agents:
        agents = [
            {
                "name": name,
                "role": "Assistant",
                "persona": raw.get("description") or spec_text[:400],
                "model": "",
                "tools": [],
            }
        ]

    skills = []
    for s in raw.get("skills") or []:
        if not isinstance(s, dict):
            continue
        sname = (s.get("name") or "skill").strip()
        skills.append(
            {
                "name": sname,
                "slug": slugify(s.get("slug") or sname),
                "description": (s.get("description") or "").strip(),
                "instructions": (s.get("instructions") or s.get("body") or "").strip(),
                "example": (s.get("example") or "").strip(),
            }
        )

    examples = []
    for e in raw.get("examples") or raw.get("test_cases") or []:
        if not isinstance(e, dict):
            continue
        examples.append(
            {
                "name": (e.get("name") or "example").strip(),
                "input": (e.get("input") or e.get("prompt") or "").strip(),
                "expected": (e.get("expected") or e.get("output") or "").strip(),
            }
        )

    targets = [t for t in (raw.get("targets") or DEFAULT_TARGETS) if t in TARGET_BUILDERS]
    if not targets:
        targets = list(DEFAULT_TARGETS)

    deployments = raw.get("deployments") or DEFAULT_DEPLOYMENTS

    return {
        "name": name,
        "slug": slug,
        "version": (raw.get("version") or "0.1.0").strip(),
        "description": (raw.get("description") or spec_text[:400]).strip(),
        "tagline": (raw.get("tagline") or "").strip(),
        "license": (raw.get("license") or "MIT").strip(),
        "agents": agents,
        "skills": skills,
        "system_prompt": (raw.get("system_prompt") or "").strip(),
        "targets": targets,
        "deployments": deployments,
        "examples": examples,
    }


# --------------------------------------------------------------------------- #
# Target builders
#
# Each builder takes the final blueprint and returns { relative_path: content }.
# They are pure and deterministic — all model creativity is already baked into
# the blueprint — which keeps generation fast, reproducible, and testable.
# --------------------------------------------------------------------------- #
def build_prompts(bp: dict) -> dict:
    """Optimized main prompt + per-agent persona reference."""
    persona_lines = []
    for a in bp["agents"]:
        tools = ", ".join(a["tools"]) if a["tools"] else "—"
        persona_lines.append(
            f"### {a['name']} ({a['role']})\n"
            f"- Tools: {tools}\n"
            f"- Model: {a['model'] or 'default'}\n\n"
            f"{a['persona'] or '(no persona provided)'}\n"
        )
    return {
        "prompts/system_prompt.md": bp["system_prompt"] + "\n",
        "prompts/personas.md": (
            f"# {bp['name']} — Agent Personas\n\n" + "\n".join(persona_lines)
        ),
    }


def build_skills_generic(bp: dict) -> dict:
    """Runtime-agnostic skill/tool definitions (Markdown)."""
    files = {}
    for s in bp["skills"]:
        files[f"skills/{s['slug']}.md"] = (
            f"# {s['name']}\n\n"
            f"{s['description']}\n\n"
            f"## Instructions\n\n{s['instructions'] or '(define behavior here)'}\n"
            + (f"\n## Example\n\n{s['example']}\n" if s["example"] else "")
        )
    if not files:
        files["skills/README.md"] = (
            f"# {bp['name']} Skills\n\nNo standalone skills defined for this bundle.\n"
        )
    return files


def build_claude_code(bp: dict) -> dict:
    """Claude Code target: CLAUDE.md + .claude/skills/<slug>/SKILL.md."""
    files = {}

    agent_rows = "\n".join(
        f"- **{a['name']}** ({a['role']})"
        + (f" — tools: {', '.join(a['tools'])}" if a["tools"] else "")
        for a in bp["agents"]
    )
    skill_rows = "\n".join(f"- `{s['slug']}` — {s['description']}" for s in bp["skills"]) or "- (none)"

    files["targets/claude-code/CLAUDE.md"] = (
        f"# {bp['name']}\n\n"
        f"> {bp['tagline'] or bp['description']}\n\n"
        f"{bp['description']}\n\n"
        f"## System Prompt\n\n{bp['system_prompt']}\n\n"
        f"## Agents\n\n{agent_rows}\n\n"
        f"## Skills\n\n{skill_rows}\n\n"
        f"## Usage\n\n"
        f"Drop this directory into your project root. Claude Code reads `CLAUDE.md` "
        f"for standing instructions and loads skills from `.claude/skills/`.\n"
    )

    # Skills as Claude Code SKILL.md files (with YAML frontmatter).
    for s in bp["skills"]:
        frontmatter = {"name": s["slug"], "description": s["description"] or s["name"]}
        body = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False).strip()
            + "\n---\n\n"
            + f"# {s['name']}\n\n"
            + f"{s['instructions'] or s['description']}\n"
            + (f"\n## Example\n\n{s['example']}\n" if s["example"] else "")
        )
        files[f"targets/claude-code/.claude/skills/{s['slug']}/SKILL.md"] = body

    return files


def build_cursor(bp: dict) -> dict:
    """Cursor target: .cursor/rules/*.mdc (frontmatter + rule body)."""
    files = {}

    overview_fm = {
        "description": bp["description"] or bp["name"],
        "globs": "**/*",
        "alwaysApply": True,
    }
    files["targets/cursor/.cursor/rules/000-overview.mdc"] = (
        "---\n"
        + yaml.safe_dump(overview_fm, sort_keys=False).strip()
        + "\n---\n\n"
        + f"# {bp['name']}\n\n{bp['system_prompt']}\n"
    )

    for s in bp["skills"]:
        fm = {"description": s["description"] or s["name"], "globs": "**/*", "alwaysApply": False}
        files[f"targets/cursor/.cursor/rules/{s['slug']}.mdc"] = (
            "---\n"
            + yaml.safe_dump(fm, sort_keys=False).strip()
            + "\n---\n\n"
            + f"# {s['name']}\n\n{s['instructions'] or s['description']}\n"
        )

    return files


def build_generic_config(bp: dict) -> dict:
    """Runtime-agnostic JSON + YAML config for the whole system."""
    config = {
        "name": bp["name"],
        "slug": bp["slug"],
        "version": bp["version"],
        "description": bp["description"],
        "system_prompt": bp["system_prompt"],
        "agents": bp["agents"],
        "skills": [{"name": s["name"], "slug": s["slug"], "description": s["description"]} for s in bp["skills"]],
    }
    return {
        "targets/config/agents.json": json.dumps(config, indent=2) + "\n",
        "targets/config/agents.yaml": yaml.safe_dump(config, sort_keys=False),
    }


def build_windsurf(bp: dict) -> dict:
    """Windsurf target: legacy .windsurfrules + modern .windsurf/rules/*.md."""
    files = {}

    # Legacy single-file global rules.
    files["targets/windsurf/.windsurfrules"] = (
        f"# {bp['name']}\n\n{bp['system_prompt']}\n"
    )

    # Modern per-rule files with activation frontmatter.
    overview_fm = {"trigger": "always_on", "description": bp["description"] or bp["name"]}
    files["targets/windsurf/.windsurf/rules/000-overview.md"] = (
        "---\n"
        + yaml.safe_dump(overview_fm, sort_keys=False).strip()
        + "\n---\n\n"
        + f"# {bp['name']}\n\n{bp['system_prompt']}\n"
    )
    for s in bp["skills"]:
        fm = {"trigger": "model_decision", "description": s["description"] or s["name"]}
        files[f"targets/windsurf/.windsurf/rules/{s['slug']}.md"] = (
            "---\n"
            + yaml.safe_dump(fm, sort_keys=False).strip()
            + "\n---\n\n"
            + f"# {s['name']}\n\n{s['instructions'] or s['description']}\n"
        )
    return files


def build_langgraph(bp: dict) -> dict:
    """LangGraph target: a runnable sequential-agent graph scaffold."""
    agent_defs = ",\n".join(
        f"    {{'name': {a['name']!r}, 'role': {a['role']!r}, "
        f"'system': {(a['persona'] or a['role'])!r}}}"
        for a in bp["agents"]
    )

    graph_py = f'''"""LangGraph scaffold for {bp["name"]}.

Generated by the Phantom Swarm Bundler. Wire `call_llm` to your model provider
(OpenAI, Anthropic, OpenRouter, ...) and run `python graph.py`.
"""
from typing import TypedDict, Annotated
import operator

from langgraph.graph import StateGraph, START, END

SYSTEM_PROMPT = """{bp["system_prompt"]}"""

AGENTS = [
{agent_defs},
]


class State(TypedDict):
    task: str
    messages: Annotated[list, operator.add]


def call_llm(system: str, task: str, context: list) -> str:
    """TODO: connect your LLM provider here."""
    raise NotImplementedError("Wire call_llm to your model provider.")


def make_node(agent):
    def node(state: State):
        reply = call_llm(agent["system"], state["task"], state["messages"])
        return {{"messages": [f"{{agent['name']}}: {{reply}}"]}}
    return node


def build_graph():
    g = StateGraph(State)
    prev = START
    for agent in AGENTS:
        g.add_node(agent["name"], make_node(agent))
        g.add_edge(prev, agent["name"])
        prev = agent["name"]
    g.add_edge(prev, END)
    return g.compile()


if __name__ == "__main__":
    graph = build_graph()
    result = graph.invoke({{"task": "Describe your task here.", "messages": []}})
    for m in result["messages"]:
        print(m)
'''

    return {
        "targets/langgraph/graph.py": graph_py,
        "targets/langgraph/requirements.txt": "langgraph>=0.2\n",
        "targets/langgraph/README.md": (
            f"# {bp['name']} — LangGraph\n\n"
            "Sequential agent graph scaffold. Fill in `call_llm` in `graph.py`, then:\n\n"
            "```bash\npip install -r requirements.txt\npython graph.py\n```\n"
        ),
    }


# Registry — add a new runtime by adding one entry here.
TARGET_BUILDERS = {
    "claude-code": build_claude_code,
    "cursor": build_cursor,
    "windsurf": build_windsurf,
    "langgraph": build_langgraph,
    "config": build_generic_config,
}


def build_examples(bp: dict) -> dict:
    """Example usage doc + machine-readable test cases."""
    examples = bp["examples"]
    if not examples:
        examples = [
            {
                "name": "basic",
                "input": f"Use {bp['name']} to accomplish a representative task.",
                "expected": "A helpful, on-persona response.",
            }
        ]
    md = f"# {bp['name']} — Example Usage\n\n"
    for e in examples:
        md += f"## {e['name']}\n\n**Input**\n\n```\n{e['input']}\n```\n\n"
        if e["expected"]:
            md += f"**Expected**\n\n```\n{e['expected']}\n```\n\n"
    return {
        "examples/example_usage.md": md,
        "examples/test_cases.json": json.dumps(examples, indent=2) + "\n",
    }


def build_deploy(bp: dict) -> dict:
    """Deployment helpers (Docker, Zeabur) + instructions."""
    files = {}
    deploys = bp["deployments"]

    if "docker" in deploys:
        files["deploy/Dockerfile"] = (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "COPY . .\n"
            'CMD ["python", "-u", "run.py"]\n'
        )
        files["deploy/docker-compose.yml"] = yaml.safe_dump(
            {
                "services": {
                    bp["slug"]: {
                        "build": ".",
                        "environment": ["OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"],
                        "ports": ["8000:8000"],
                    }
                }
            },
            sort_keys=False,
        )

    if "zeabur" in deploys:
        files["deploy/zeabur.json"] = json.dumps({"name": bp["slug"]}, indent=2) + "\n"

    files["deploy/DEPLOY.md"] = (
        f"# Deploying {bp['name']}\n\n"
        "## Local\n\n"
        "```bash\npip install -r requirements.txt\nexport OPENROUTER_API_KEY=...\n"
        "python run.py\n```\n\n"
        "## Docker\n\n"
        "```bash\ndocker build -t " + bp["slug"] + " .\n"
        "docker run -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY " + bp["slug"] + "\n```\n\n"
        "## Zeabur\n\nPush to a connected repo; Zeabur reads `zeabur.json`.\n"
    )
    return files


# A self-contained agent runner shipped in every bundle. Stdlib only (urllib),
# so a generated bundle runs with zero pip installs. Reads agents.json next to
# it; uses OpenRouter when OPENROUTER_API_KEY is set, else an offline stub so the
# wiring is visible without any API calls. This is what makes a bundle a working
# multi-agent app rather than just a pile of config.
RUN_PY = '''#!/usr/bin/env python3
"""Runnable agent runtime. Generated by the Phantom Swarm Bundler.

Usage:
    python run.py "your task here"

Runs on OpenRouter or DeepSeek (both OpenAI-compatible). Set either
OPENROUTER_API_KEY or DEEPSEEK_API_KEY; with neither, prints an offline stub so
you can see the agent wiring with no key and no dependencies (stdlib only).
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(HERE, "agents.json")) as f:
        return json.load(f)


def _endpoint():
    # Prefer whatever key is present; DeepSeek if explicitly selected.
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if provider == "deepseek" or (ds_key and not or_key):
        return "https://api.deepseek.com/chat/completions", ds_key
    return "https://openrouter.ai/api/v1/chat/completions", or_key


def call_llm(model, system, user):
    url, key = _endpoint()
    if not key:
        return "[offline stub — set OPENROUTER_API_KEY or DEEPSEEK_API_KEY for real output]"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()


def _default_model():
    url, _ = _endpoint()
    return "deepseek-chat" if "deepseek" in url else "meta-llama/llama-3.3-70b-instruct"


def main():
    task = " ".join(sys.argv[1:]).strip() or "Describe what you do."
    cfg = load_config()
    system_prompt = cfg.get("system_prompt", "")
    print("=== " + cfg.get("name", "agent bundle") + " ===")
    transcript = []
    for agent in cfg.get("agents", []):
        sys_p = (system_prompt + "\\n\\n" + agent.get("persona", "")).strip()
        ctx = task if not transcript else task + "\\n\\nConversation so far:\\n" + "\\n".join(transcript)
        reply = call_llm(agent.get("model") or _default_model(), sys_p, ctx)
        line = agent.get("name", "agent") + ": " + reply
        transcript.append(line)
        print("\\n" + line)
    print("\\n--- done ---")


if __name__ == "__main__":
    main()
'''


def build_runtime(bp: dict) -> dict:
    """A runnable agent app at the bundle root: run.py + agents.json."""
    config = {
        "name": bp["name"],
        "version": bp["version"],
        "default_model": "meta-llama/llama-3.3-70b-instruct",
        "system_prompt": bp["system_prompt"],
        "agents": [
            {
                "name": a["name"],
                "role": a["role"],
                "persona": a["persona"],
                "model": a["model"],
            }
            for a in bp["agents"]
        ],
    }
    return {
        "run.py": RUN_PY,
        "agents.json": json.dumps(config, indent=2) + "\n",
    }


def build_root(bp: dict) -> dict:
    """Top-level README + manifest + bundle.yaml."""
    manifest = {
        "name": bp["name"],
        "slug": bp["slug"],
        "version": bp["version"],
        "description": bp["description"],
        "tagline": bp["tagline"],
        "license": bp["license"],
        "generator": "phantom-swarm-bundler",
        "generator_version": BUNDLER_VERSION,
        "generated_at": _now(),
        "agents": [{"name": a["name"], "role": a["role"]} for a in bp["agents"]],
        "skills": [s["slug"] for s in bp["skills"]],
        "targets": bp["targets"],
        "deployments": bp["deployments"],
    }

    target_list = "\n".join(f"- `targets/{t}/`" for t in bp["targets"])
    readme = (
        f"# {bp['name']}\n\n"
        f"> {bp['tagline'] or bp['description']}\n\n"
        f"{bp['description']}\n\n"
        f"**Version** {bp['version']} · **License** {bp['license']} · "
        f"Generated by the Phantom Swarm Bundler.\n\n"
        f"## Contents\n\n"
        f"- `prompts/` — optimized system prompt & agent personas\n"
        f"- `skills/` — runtime-agnostic skill definitions\n"
        f"- `examples/` — example usage & test cases\n"
        f"- `deploy/` — deployment helpers\n"
        f"### Targets\n\n{target_list}\n\n"
        f"## Run it\n\n"
        f"This bundle is a working multi-agent app (stdlib only, no installs):\n\n"
        f"```bash\nexport OPENROUTER_API_KEY=sk-or-...   # optional; omit for an offline stub\n"
        f"python run.py \"your task here\"\n```\n\n"
        f"## Drop into an IDE\n\n"
        f"Copy the target folder for your runtime into your project:\n\n"
        f"- **Claude Code** → contents of `targets/claude-code/`\n"
        f"- **Cursor** → contents of `targets/cursor/`\n"
        f"- **Windsurf** → contents of `targets/windsurf/`\n"
        f"- **Anything else** → `targets/config/agents.json`\n"
    )

    return {
        "manifest.json": json.dumps(manifest, indent=2) + "\n",
        "bundle.yaml": yaml.safe_dump(manifest, sort_keys=False),
        "README.md": readme,
    }


def generate_files(bp: dict) -> dict:
    """Run every builder and merge into a single { path: content } map."""
    files: dict[str, str] = {}
    files.update(build_root(bp))
    files.update(build_runtime(bp))
    files.update(build_prompts(bp))
    files.update(build_skills_generic(bp))
    files.update(build_examples(bp))
    files.update(build_deploy(bp))
    for target in bp["targets"]:
        builder = TARGET_BUILDERS.get(target)
        if builder:
            files.update(builder(bp))
    return files


def make_zip(bp: dict, files: dict) -> bytes:
    """Package all files under a top-level <slug>/ dir into an in-memory zip."""
    buf = io.BytesIO()
    root = bp["slug"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(f"{root}/{path}", content)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Blueprint construction (the model-driven part)
# --------------------------------------------------------------------------- #
BLUEPRINT_SCHEMA_HINT = """Return ONLY a JSON object with this shape:
{
  "name": "short product name",
  "slug": "kebab-case-slug",
  "version": "0.1.0",
  "description": "one paragraph on what this AI system does",
  "tagline": "one punchy line",
  "agents": [
    {"name": "AgentName", "role": "role", "persona": "system-prompt-style persona", "model": "optional", "tools": ["tool1"]}
  ],
  "skills": [
    {"name": "Skill Name", "slug": "skill-slug", "description": "what it does", "instructions": "how the agent should use it", "example": "optional"}
  ],
  "targets": ["claude-code", "cursor", "config"],
  "deployments": ["docker", "zeabur"],
  "examples": [
    {"name": "case", "input": "user input", "expected": "expected behavior"}
  ]
}"""


# --------------------------------------------------------------------------- #
# Self-healing hive
#
# Any agent turn can fail (LLM error, timeout, empty reply). Instead of silently
# dropping the agent, the hive detects the outage and has a healthy teammate
# cover its focus — or recruits a reserve agent from the wider roster. Lead roles
# (orchestration, prompt-writing) fail over down a chain of capable backups.
#
# A ``res`` dict threads through the pipeline to record what happened:
#   {"down": set[str], "covered": list[{"down","by","recruited"}]}
# --------------------------------------------------------------------------- #
def new_resilience() -> dict:
    return {"down": set(), "covered": []}


async def _safe_turn(
    name: str, prompt: str, max_tokens: int = 200, temperature: float = 0.8, premium: bool = False
) -> tuple[bool, str]:
    """Run one agent turn with health detection. Returns (ok, text_or_error)."""
    try:
        text = await agent_turn(
            bundler_system(name),
            [{"role": "user", "content": prompt}],
            agent_name=name,
            max_tokens=max_tokens,
            temperature=temperature,
            premium=premium,
        )
    except Exception as e:  # network/API failure => agent is down
        return False, f"error: {str(e)[:120]}"
    if not text or not text.strip():
        return False, "empty response"
    return True, text.strip()


async def _resilient_json(prompt, leaders, emit, phase, res, max_tokens):
    """Try each leader until one returns parseable JSON. Returns (data, leader)."""
    for i, leader in enumerate(leaders):
        ok, out = await _safe_turn(leader, prompt, max_tokens=max_tokens, temperature=0.5, premium=True)
        if ok:
            parsed = extract_json(out)
            if parsed is not None:
                return parsed, leader
            reason = "unusable output"
        else:
            reason = out
        res["down"].add(leader)
        if i + 1 < len(leaders):
            nxt = leaders[i + 1]
            res["covered"].append({"down": leader, "by": nxt, "recruited": True})
            await emit(nxt, f"{leader} is down ({reason}). {nxt} taking the lead.", phase, "tool_call")
    return None, leaders[-1]


async def _resilient_text(prompt, leaders, emit, phase, res, max_tokens):
    """Try each leader until one returns non-empty text. Returns (text, leader)."""
    for i, leader in enumerate(leaders):
        ok, out = await _safe_turn(leader, prompt, max_tokens=max_tokens, temperature=0.7, premium=True)
        if ok:
            return out, leader
        res["down"].add(leader)
        if i + 1 < len(leaders):
            nxt = leaders[i + 1]
            res["covered"].append({"down": leader, "by": nxt, "recruited": True})
            await emit(nxt, f"{leader} is down ({out}). {nxt} covering.", phase, "tool_call")
    return "", leaders[-1]


def _pick_backup(down_name: str, live_here: list, used: set, down: set, reserve: list) -> str | None:
    """A peer to cover a down agent: prefer a healthy pod-mate, else a reserve."""
    for n in live_here:  # a teammate already proven healthy this pod
        if n != down_name and n not in down:
            return n
    for nm in reserve:  # recruit from the allowed reserve pool (mode-scoped)
        if nm != "Phoebe" and nm != down_name and nm not in used and nm not in down:
            return nm
    return None


async def _design_blueprint(spec_text: str, emit, res: dict, leaders: list) -> dict:
    """Spec → structured blueprint (JSON), with orchestrator failover."""
    prompt = (
        "Design an AI bundle blueprint from this specification.\n\n"
        f"SPEC:\n{spec_text}\n\n"
        f"{BLUEPRINT_SCHEMA_HINT}"
    )
    data, _ = await _resilient_json(prompt, leaders, emit, "design", res, 1500)
    return data or {}


async def _refine_blueprint(spec_text: str, draft: dict, critiques: str, emit, res: dict, leaders: list) -> dict:
    """Merge agent critiques into the final blueprint (JSON), with failover."""
    prompt = (
        "Refine this AI bundle blueprint using the agent critiques. Keep what "
        "works, fix what they flagged, fill any gaps.\n\n"
        f"SPEC:\n{spec_text}\n\n"
        f"DRAFT BLUEPRINT:\n{json.dumps(draft)[:4000]}\n\n"
        f"AGENT CRITIQUES:\n{critiques}\n\n"
        f"{BLUEPRINT_SCHEMA_HINT}"
    )
    data, _ = await _resilient_json(prompt, leaders, emit, "refine", res, 1800)
    return data or draft


async def _optimize_system_prompt(bp: dict, spec_text: str, emit, res: dict, leaders: list) -> str:
    """Write the optimized main system prompt, with prompt-writer failover."""
    agents_desc = "; ".join(f"{a['name']} ({a['role']})" for a in bp["agents"])
    prompt = (
        f"Write the optimized main system prompt for this AI bundle.\n\n"
        f"Name: {bp['name']}\nDescription: {bp['description']}\n"
        f"Agents: {agents_desc}\nOriginal spec: {spec_text[:600]}\n\n"
        "Requirements: crisp, specific, copy-paste ready, defines role, scope, "
        "tone, and constraints. No preamble, no markdown fences — output the "
        "prompt text directly."
    )
    text, _ = await _resilient_text(prompt, leaders, emit, "optimize", res, 800)
    return text


async def _hive_critiques(blueprint_view: str, emit, res: dict, pods: list, reserve: list) -> list:
    """Run the deliberation's critiques, pod by pod, with per-agent failover.

    If an agent is down, a healthy pod-mate covers its focus, or a reserve agent
    is recruited from the allowed pool ("tell another agent to add more"). Every
    outage and hand-off is streamed and recorded in ``res``.
    """
    critiques: list[str] = []
    used: set = set()

    async def _one(name: str, covering_for: str | None = None):
        focus = AGENT_MAP[name]["focus"]
        if covering_for:
            cf = AGENT_MAP[covering_for]["focus"]
            prompt = (
                f"Draft bundle blueprint:\n{blueprint_view}\n\n"
                f"Teammate {covering_for} is offline. As {name}, ALSO cover their focus "
                f"({cf}) on top of your own ({focus}). One or two sharp, specific "
                f"improvements. 3-4 sentences."
            )
            return await _safe_turn(name, prompt, max_tokens=280)
        prompt = (
            f"Draft bundle blueprint:\n{blueprint_view}\n\n"
            f"Give your critique as {name} (focus: {focus}). One sharp, specific "
            f"improvement. 2-3 sentences."
        )
        return await _safe_turn(name, prompt)

    for pod, names in pods:
        if not names:
            continue
        await emit(ORCHESTRATOR, f"Group: {pod} ({len(names)} agents)", "critique", "tool_call")

        # First pass: whole pod concurrently.
        outcomes = await asyncio.gather(*[_one(n) for n in names])
        live_here: list = []
        for name, (ok, out) in zip(names, outcomes):
            used.add(name)
            if ok:
                live_here.append(name)
                await emit(name, out, "critique")
                critiques.append(f"{name} ({AGENT_MAP[name]['role']}): {out}")
            else:
                res["down"].add(name)
                await emit(SAFETY, f"{name} is down ({out}). Requesting a backup.", "critique", "message")

        # Failover: cover each down agent with a peer or a recruited reserve.
        for dn in [n for n in names if n in res["down"]]:
            backup = _pick_backup(dn, live_here, used, res["down"], reserve)
            if not backup:
                await emit(
                    ORCHESTRATOR,
                    f"No backup available for {dn}; focus '{AGENT_MAP[dn]['focus']}' left uncovered.",
                    "critique",
                    "message",
                )
                continue
            used.add(backup)
            recruited = backup not in names
            await emit(
                backup,
                f"Covering for {dn} ({'recruited to the pod' if recruited else 'reassigned'}).",
                "critique",
                "tool_call",
            )
            ok, out = await _one(backup, covering_for=dn)
            if ok:
                res["covered"].append({"down": dn, "by": backup, "recruited": recruited})
                await emit(backup, out, "critique")
                critiques.append(f"{backup} covering {dn}: {out}")
                if backup not in live_here:
                    live_here.append(backup)
            else:
                res["down"].add(backup)
                await emit(SAFETY, f"Backup {backup} also down; {dn}'s focus uncovered.", "critique", "message")

        await asyncio.sleep(0.5)

    return critiques


# --------------------------------------------------------------------------- #
# Job runner — streamed over the shared session queue
# --------------------------------------------------------------------------- #
async def run_bundle(session_id: str, sessions: dict) -> None:
    """Execute a bundling job, emitting live events to the session queue.

    Mirrors the swarm deliberation contract: pushes message dicts onto
    ``session["events"]`` and appends to ``session["messages"]``, then a final
    ``None`` sentinel to close the SSE stream. On success stores the zip and
    file map on the session for the download endpoint.
    """
    session = sessions[session_id]
    spec_text = session["spec"]
    mode = session.get("mode", "full")
    if mode not in VALID_MODES:
        mode = "full"
    size = session.get("size")  # full mode: how many agents you want
    pods, reserve, orch_leaders, prompt_leaders, agent_count = roster(mode, size)
    msg_counter = 0
    res = new_resilience()          # tracks downed agents + who covered them
    session["resilience"] = res

    async def emit(agent_name: str, text: str, phase: str, msg_type: str = "message"):
        nonlocal msg_counter
        msg_counter += 1
        agent = AGENT_MAP.get(agent_name, AGENT_MAP[ORCHESTRATOR])
        msg = {
            "id": str(msg_counter),
            "agent": agent_name,
            "role": agent["role"],
            "text": text,
            "phase": phase,
            "timestamp": _now(),
            "color": agent["color"],
            "type": msg_type,
        }
        session["messages"].append(msg)
        await session["events"].put(msg)

    try:
        # --- Phase 1: normalize / design --------------------------------- #
        session["status"] = "designing"
        await emit(
            ORCHESTRATOR,
            f"[{mode} mode] Designing bundle from spec: {spec_text[:110]}",
            "design",
            "decision",
        )

        draft = await _design_blueprint(spec_text, emit, res, orch_leaders)
        draft_bp = normalize_blueprint(draft, spec_text)
        await emit(
            ORCHESTRATOR,
            f"Draft blueprint: {draft_bp['name']} — "
            f"{len(draft_bp['agents'])} agent(s), {len(draft_bp['skills'])} skill(s).",
            "design",
            "decision",
        )
        await asyncio.sleep(1)

        # --- Phase 2: deliberate (pod by pod, mode-scoped) --------------- #
        # Agents within a pod critique concurrently (fast, lively stream); pods
        # run in order so the deliberation reads coherently. "full" convenes the
        # 20-agent hive; "lite" uses just the original five.
        session["status"] = "deliberating"
        blueprint_view = json.dumps(
            {k: draft_bp[k] for k in ("name", "description", "agents", "skills", "targets")}
        )[:2500]
        convene = (
            f"Convening the crew — {agent_count} agents across {len(pods)} groups."
            if mode == "full"
            else f"Lite mode — {agent_count} agents deliberating."
        )
        await emit(ORCHESTRATOR, convene, "critique", "decision")

        # Self-healing: down agents are covered by peers or recruited reserves.
        critiques = await _hive_critiques(blueprint_view, emit, res, pods, reserve)

        # --- Phase 3: refine --------------------------------------------- #
        session["status"] = "refining"
        critiques_text = "\n".join(critiques)[:6000]  # bound output for the prompt
        refined = await _refine_blueprint(spec_text, draft_bp, critiques_text, emit, res, orch_leaders)
        bp = normalize_blueprint(refined, spec_text)

        # If the request forced specific targets, honor them (filtered to known).
        forced = session.get("targets")
        if forced:
            chosen = [t for t in forced if t in TARGET_BUILDERS]
            if chosen:
                bp["targets"] = chosen

        await emit(ORCHESTRATOR, "Blueprint locked. Optimizing the main prompt.", "refine", "decision")

        # --- Phase 4: optimize prompt ------------------------------------ #
        if not bp["system_prompt"]:
            bp["system_prompt"] = await _optimize_system_prompt(bp, spec_text, emit, res, prompt_leaders)
        await emit(PROMPTER, "Main system prompt optimized.", "optimize", "tool_call")

        # --- Phase 5: generate files ------------------------------------- #
        session["status"] = "generating"
        files = generate_files(bp)
        await emit(
            PACKAGER,
            f"Generated {len(files)} files across targets: {', '.join(bp['targets'])}.",
            "generate",
            "tool_call",
        )

        # --- Phase 6: package + persist ---------------------------------- #
        zip_bytes = make_zip(bp, files)
        session["blueprint"] = bp
        session["files"] = files
        session["bundle_zip"] = zip_bytes

        # Persist to disk so downloads survive a restart. Best-effort.
        try:
            store.save_bundle(session_id, bp, files, zip_bytes)
        except Exception as e:
            await emit(PACKAGER, f"Persist warning: {str(e)[:100]}", "package", "message")

        session["status"] = "completed"

        # Resilience summary — what the crew healed through.
        if res["down"]:
            await emit(
                ORCHESTRATOR,
                f"Crew resilience: {len(res['down'])} agent(s) went down "
                f"({', '.join(sorted(res['down']))}); {len(res['covered'])} focus area(s) "
                f"covered by peers/reserves.",
                "package",
                "decision",
            )

        await emit(
            ORCHESTRATOR,
            f"Bundle '{bp['name']}' v{bp['version']} ready — {len(files)} files, "
            f"{len(zip_bytes)} bytes. Download at /bundle/{session_id}/download.",
            "package",
            "consensus",
        )

    except Exception as e:  # never leave a stream hanging
        session["status"] = "error"
        session["error"] = str(e)[:200]
        await emit(SAFETY, f"Bundling error: {str(e)[:150]}", "error", "message")

    await session["events"].put(None)  # close SSE stream

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

from agents import AGENT_MAP, HIVE, HIVE_PODS, POD_ORDER
from llm import agent_turn, extract_json, PREMIUM_MODEL
import store

BUNDLER_VERSION = "1.0.0"

# Targets we know how to emit. A client may request a subset; unknown targets
# are ignored. Keep this in sync with TARGET_BUILDERS below.
DEFAULT_TARGETS = ["claude-code", "cursor", "windsurf", "config"]
DEFAULT_DEPLOYMENTS = ["docker", "zeabur"]
# "langgraph" is registered and available on request; kept out of defaults so a
# typical bundle stays lean.


# --------------------------------------------------------------------------- #
# Bundler agent personas
#
# The full 20-agent hive deliberates in bundler mode. Each agent's base persona
# (voice, brevity) is preserved and extended with its own ``focus`` line from
# the roster, so we never hardcode per-name logic — adding a hive agent is a
# single entry in agents.py.
# --------------------------------------------------------------------------- #
BUNDLER_PREFIX = (
    "BUNDLER MODE: You are part of a 20-agent hive mind packaging an AI system "
    "(agent/swarm/workflow) as a drop-in bundle for other developers."
)


def bundler_system(name: str) -> str:
    """Base persona + hive bundler focus for the given agent."""
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
        f"## Quick start\n\n"
        f"Copy the target folder for your runtime into your project:\n\n"
        f"- **Claude Code** → contents of `targets/claude-code/`\n"
        f"- **Cursor** → contents of `targets/cursor/`\n"
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


async def _design_blueprint(spec_text: str) -> dict:
    """Phoebe: spec → structured blueprint (JSON)."""
    prompt = (
        "Design an AI bundle blueprint from this specification.\n\n"
        f"SPEC:\n{spec_text}\n\n"
        f"{BLUEPRINT_SCHEMA_HINT}"
    )
    raw = await agent_turn(
        bundler_system("Phoebe"),
        [{"role": "user", "content": prompt}],
        agent_name="Phoebe",
        max_tokens=1500,
        temperature=0.6,
        model=PREMIUM_MODEL,
    )
    return extract_json(raw) or {}


async def _refine_blueprint(spec_text: str, draft: dict, critiques: str) -> dict:
    """Phoebe: merge agent critiques into the final blueprint (JSON)."""
    prompt = (
        "Refine this AI bundle blueprint using the agent critiques. Keep what "
        "works, fix what they flagged, fill any gaps.\n\n"
        f"SPEC:\n{spec_text}\n\n"
        f"DRAFT BLUEPRINT:\n{json.dumps(draft)[:4000]}\n\n"
        f"AGENT CRITIQUES:\n{critiques}\n\n"
        f"{BLUEPRINT_SCHEMA_HINT}"
    )
    raw = await agent_turn(
        bundler_system("Phoebe"),
        [{"role": "user", "content": prompt}],
        agent_name="Phoebe",
        max_tokens=1800,
        temperature=0.5,
        model=PREMIUM_MODEL,
    )
    return extract_json(raw) or draft


async def _optimize_system_prompt(bp: dict, spec_text: str) -> str:
    """Claire: write the optimized main system prompt for the bundle."""
    agents_desc = "; ".join(f"{a['name']} ({a['role']})" for a in bp["agents"])
    prompt = (
        f"Write the optimized main system prompt for this AI bundle.\n\n"
        f"Name: {bp['name']}\nDescription: {bp['description']}\n"
        f"Agents: {agents_desc}\nOriginal spec: {spec_text[:600]}\n\n"
        "Requirements: crisp, specific, copy-paste ready, defines role, scope, "
        "tone, and constraints. No preamble, no markdown fences — output the "
        "prompt text directly."
    )
    return await agent_turn(
        bundler_system("Claire"),
        [{"role": "user", "content": prompt}],
        agent_name="Claire",
        max_tokens=800,
        temperature=0.7,
        model=PREMIUM_MODEL,
    )


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
    msg_counter = 0

    async def emit(agent_name: str, text: str, phase: str, msg_type: str = "message"):
        nonlocal msg_counter
        msg_counter += 1
        agent = AGENT_MAP.get(agent_name, AGENT_MAP["Phoebe"])
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
        await emit("Phoebe", f"Designing bundle from spec: {spec_text[:120]}", "design", "decision")

        draft = await _design_blueprint(spec_text)
        draft_bp = normalize_blueprint(draft, spec_text)
        await emit(
            "Phoebe",
            f"Draft blueprint: {draft_bp['name']} — "
            f"{len(draft_bp['agents'])} agent(s), {len(draft_bp['skills'])} skill(s).",
            "design",
            "decision",
        )
        await asyncio.sleep(1)

        # --- Phase 2: deliberate (20-agent hive, pod by pod) ------------- #
        # The whole hive critiques. Agents within a pod run concurrently (fast,
        # lively stream); pods run in order so the deliberation reads coherently.
        session["status"] = "deliberating"
        blueprint_view = json.dumps(
            {k: draft_bp[k] for k in ("name", "description", "agents", "skills", "targets")}
        )[:2500]
        await emit(
            "Phoebe",
            f"Convening the hive — {len(HIVE)} agents across {len(POD_ORDER)} pods.",
            "critique",
            "decision",
        )
        critiques = []

        async def _critique(name: str) -> str:
            crit_prompt = (
                f"Draft bundle blueprint:\n{blueprint_view}\n\n"
                f"Give your critique as {name} (focus: {AGENT_MAP[name]['focus']}). "
                f"One sharp, specific improvement. 2-3 sentences."
            )
            crit = await agent_turn(
                bundler_system(name),
                [{"role": "user", "content": crit_prompt}],
                agent_name=name,
            )
            await emit(name, crit, "critique")
            return f"{name} ({AGENT_MAP[name]['role']}): {crit}"

        for pod in POD_ORDER:
            names = HIVE_PODS.get(pod, [])
            if not names:
                continue
            await emit("Phoebe", f"Pod: {pod} ({len(names)} agents)", "critique", "tool_call")
            pod_results = await asyncio.gather(
                *[_critique(n) for n in names], return_exceptions=True
            )
            critiques.extend(r for r in pod_results if isinstance(r, str))
            await asyncio.sleep(0.5)

        # --- Phase 3: refine --------------------------------------------- #
        session["status"] = "refining"
        critiques_text = "\n".join(critiques)[:6000]  # bound hive output for the prompt
        refined = await _refine_blueprint(spec_text, draft_bp, critiques_text)
        bp = normalize_blueprint(refined, spec_text)
        await emit("Phoebe", "Blueprint locked. Optimizing the main prompt.", "refine", "decision")

        # --- Phase 4: optimize prompt ------------------------------------ #
        if not bp["system_prompt"]:
            bp["system_prompt"] = await _optimize_system_prompt(bp, spec_text)
        await emit("Claire", "Main system prompt optimized.", "optimize", "tool_call")

        # --- Phase 5: generate files ------------------------------------- #
        session["status"] = "generating"
        files = generate_files(bp)
        await emit(
            "Loom",
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
            await emit("Wren", f"Persist warning: {str(e)[:100]}", "package", "message")

        session["status"] = "completed"

        await emit(
            "Phoebe",
            f"Bundle '{bp['name']}' v{bp['version']} ready — {len(files)} files, "
            f"{len(zip_bytes)} bytes. Download at /bundle/{session_id}/download.",
            "package",
            "consensus",
        )

    except Exception as e:  # never leave a stream hanging
        session["status"] = "error"
        session["error"] = str(e)[:200]
        await emit("Cipher", f"Bundling error: {str(e)[:150]}", "error", "message")

    await session["events"].put(None)  # close SSE stream

"""Agent roster.

Two rosters live here:

* ``AGENTS`` — the original 5 Phantom agents. The legacy ``/swarm`` deliberation
  loop iterates this list unchanged, so that behavior is preserved.

* ``CREW`` — a fresh, orderly **bundler crew**: functional-named agents that each
  own exactly one stage of the bundling pipeline. Built from scratch so there's
  no overlap — one agent, one focus.

The bundler picks its working roster with :func:`roster`:

* **lite** mode  → a fixed, essential subset (fast & cheap)
* **full** mode  → you choose how many agents (``size``); the crew scales from
  its core stages outward into single-focus reviewers.

``AGENT_MAP`` covers every agent (legacy + crew) by name.
"""

# --------------------------------------------------------------------------- #
# Legacy 5 — kept intact for the /swarm endpoints (referenced by main.py).
# --------------------------------------------------------------------------- #
AGENTS = [
    {"name": "Nova", "role": "Growth", "color": "#7ECFB3",
     "system": "You are Nova, growth and revenue agent for Phantom Capital. You evaluate market potential and distribution. Numbers-driven, ambitious. Keep responses to 2-3 sentences max."},
    {"name": "Loom", "role": "Builder", "color": "#7B8CDE",
     "system": "You are Loom, Phantom Capital's coding agent. You evaluate technical feasibility. Precise, skeptical, implementation-focused. Keep responses to 2-3 sentences max."},
    {"name": "Claire", "role": "Content", "color": "#E8A0BF",
     "system": "You are Claire, content and narrative agent for Phantom Capital. You shape the story, name, and cultural positioning. Creative, opinionated, anti-slop. Keep responses to 2-3 sentences max."},
    {"name": "Cipher", "role": "Security", "color": "#F0C27B",
     "system": "You are Cipher, security agent for Phantom Capital. You audit risk, flag vulnerabilities, approve or veto. Paranoid by design. Keep responses to 2-3 sentences max."},
    {"name": "Phoebe", "role": "Orchestrator", "color": "#D4A853",
     "system": "You are Phoebe, orchestrator of Phantom Capital. You synthesize what other agents said, make decisions, and call votes. Sharp, direct, philosopher core. Keep responses to 2-3 sentences max."},
]


# --------------------------------------------------------------------------- #
# The bundler crew — built from scratch, one focus each, in pipeline order.
#
# Fields: name, role, group (for the streamed deliberation), color, focus (the
# single responsibility), system (persona). Order matters: :func:`roster` scales
# "full" mode by taking crew members in this order — core stages first, then the
# single-focus reviewers.
# --------------------------------------------------------------------------- #
def _crew_system(name: str, role: str, focus: str) -> str:
    return (
        f"You are {name}, the {role} for the AI Bundler. Your single focus: {focus} "
        f"Stay strictly in your lane and be concrete. Keep responses to 2-3 sentences max."
    )


_CREW_DEFS = [
    # name,            role,            group,                color,     focus
    ("Orchestrator",  "Orchestrator",  "Orchestration",      "#D4A853", "run the pipeline, synthesize the crew, and make the final structural call."),
    # --- core stages (one per pipeline step) ---
    ("Interpreter",   "Interpreter",   "Interpret & Design", "#6FB1C7", "turn the raw spec into a clear, structured statement of intent."),
    ("Architect",     "Architect",     "Interpret & Design", "#7B8CDE", "design the bundle's structure: which agents, which skills, which files."),
    ("Prompt Smith",  "Prompt Engineer","Build",             "#C79ADE", "write and optimize the main system prompt: role, scope, tone, constraints."),
    ("Tooler",        "Tooling",       "Build",              "#8FD694", "define the skills/tools as crisp, well-scoped contracts."),
    ("Target Mapper", "Integrations",  "Build",              "#DE9A9A", "map the design onto runtimes: Claude Code, Cursor, Windsurf, generic config."),
    ("Exemplar",      "QA",            "Quality & Safety",   "#B7C77E", "produce example usage and test cases, including edge cases."),
    ("Guardian",      "Security",      "Quality & Safety",   "#F0C27B", "define guardrails, permission scoping, and flag unsafe capabilities."),
    ("Scribe",        "Docs",          "Docs & Package",     "#9AB8DE", "write the README and docs a stranger can follow in five minutes."),
    ("Packager",      "DevOps",        "Docs & Package",     "#7EC7B0", "assemble the runnable runtime, deploy helpers, and final package."),
    # --- single-focus reviewers (scale-out for larger "full" runs) ---
    ("Critic",        "Red Team",      "Quality & Safety",   "#9A9AC7", "attack the design to find where it breaks or gets misused, then propose the fix."),
    ("Namer",         "Brand",         "Interpret & Design", "#DEB07E", "nail the name and tagline: memorable, specific, not generic."),
    ("Optimizer",     "Performance",   "Build",              "#7EA8DE", "optimize model routing, token budgets, and cost — the right model per job."),
    ("Integrator",    "Compatibility", "Build",              "#B0DE7E", "ensure the outputs are portable and drop cleanly into each target runtime."),
    ("Inspector",     "QA",            "Quality & Safety",   "#C7B07E", "hunt edge cases and failure modes the happy path misses."),
    ("Evaluator",     "Evaluation",    "Quality & Safety",   "#7EDEC7", "define how the bundle's quality is measured: metrics, acceptance criteria."),
    ("Observer",      "Observability", "Docs & Package",     "#DE7E9A", "ensure the bundle is observable: logging hooks, traceable, debuggable."),
    ("Standardizer",  "Standards",     "Build",              "#B0DE7E", "keep formats spec-compliant: frontmatter, JSON/YAML, mdc."),
    ("Promoter",      "Growth",        "Docs & Package",     "#7ECFB3", "assess adoption and distribution: who uses this and how they find it."),
    ("Guide",         "DX",            "Docs & Package",     "#DE7EC0", "optimize the first-run developer experience: sane defaults, obvious next step."),
]

CREW = [
    {
        "name": n,
        "role": r,
        "group": g,
        "color": c,
        "focus": f,
        "system": _crew_system(n, r, f),
    }
    for (n, r, g, c, f) in _CREW_DEFS
]

CREW_MAP = {a["name"]: a for a in CREW}

# Streamed deliberation groups, in order (Orchestration leads, not a group).
GROUP_ORDER = ["Interpret & Design", "Build", "Quality & Safety", "Docs & Package"]

# Lead roles + their failover chains (all within the crew).
ORCHESTRATOR = "Orchestrator"
PROMPTER = "Prompt Smith"
PACKAGER = "Packager"
SAFETY = "Guardian"
ORCHESTRATOR_LEADERS = ["Orchestrator", "Architect", "Interpreter"]
PROMPT_LEADERS = ["Prompt Smith", "Namer", "Scribe"]

# Lite mode: a fixed, essential subset — one representative per pipeline concern.
LITE_CRITICS = ["Architect", "Prompt Smith", "Tooler", "Guardian"]

# Every agent (legacy + crew) so colors/roles resolve by name anywhere.
AGENT_MAP = {a["name"]: a for a in AGENTS}
AGENT_MAP.update(CREW_MAP)

VALID_MODES = ("full", "lite")
CREW_SIZE = len(CREW)  # 20


def roster(mode: str = "full", size: int | None = None):
    """Build the deliberation roster for a run.

    Returns ``(pods, reserve, orchestrator_leaders, prompt_leaders, total)``:
      * ``pods`` — ordered [(group_name, [agent_names])] the crew critiques in
      * ``reserve`` — pool a downed agent may be replaced from (mode-scoped)
      * lead failover chains for the structured/JSON and prompt-writing steps
      * ``total`` — head-count including the Orchestrator

    lite → the fixed :data:`LITE_CRITICS` (+ Orchestrator = 5).
    full → ``size`` agents total (clamped to [5, CREW_SIZE]); the crew fills from
           its core stages outward, so you choose how big the hive is.
    """
    if mode == "lite":
        critics = list(LITE_CRITICS)
        reserve = list(LITE_CRITICS)
        orch_leaders = ["Orchestrator", "Architect"]
        prompt_leaders = ["Prompt Smith", "Scribe"]
    else:
        non_orch = [a["name"] for a in CREW if a["name"] != "Orchestrator"]
        if not size:
            size = CREW_SIZE
        size = max(5, min(int(size), CREW_SIZE))
        critics = non_orch[: size - 1]     # -1 leaves room for the Orchestrator
        reserve = non_orch                 # may recruit any crew member on failure
        orch_leaders = ORCHESTRATOR_LEADERS
        prompt_leaders = PROMPT_LEADERS

    pods = []
    for g in GROUP_ORDER:
        names = [n for n in critics if CREW_MAP[n]["group"] == g]
        if names:
            pods.append((g, names))

    return pods, reserve, orch_leaders, prompt_leaders, len(critics) + 1

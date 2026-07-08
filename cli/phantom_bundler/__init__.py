"""phantom-bundler — a clean CLI for the Phantom Automated AI Bundler.

Talk to a hosted phantom-swarm-engine (or an auto-detected local one), create
bundles from a natural-language spec, follow the live hive stream, download the
zip, and run the generated multi-agent app — all from the terminal.

    $ phantom-bundle create "A 3-agent code-review swarm"
    $ phantom-bundle stream <session_id>
    $ phantom-bundle run ./my-bundle "review this diff"
"""

__version__ = "1.0.0"

# Kept in sync with the engine's bundler.DEFAULT_TARGETS so `targets` works
# offline (no server round-trip required).
DEFAULT_TARGETS = ["claude-code", "cursor", "windsurf", "config"]
ALL_TARGETS = ["claude-code", "cursor", "windsurf", "langgraph", "config"]

__all__ = ["__version__", "DEFAULT_TARGETS", "ALL_TARGETS"]

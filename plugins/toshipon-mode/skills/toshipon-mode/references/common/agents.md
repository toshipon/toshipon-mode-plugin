# Agent Orchestration

## Available Agents

Only `architect` ships with this plugin, at `${CLAUDE_PLUGIN_ROOT}/agents/architect.md`. The other rows are optional agents a user may have configured under `~/.claude/agents/`; they are not bundled here.

| Agent | Purpose | When to Use |
|-------|---------|-------------|
| planner | Implementation planning | Complex features, refactoring |
| architect | System design | Architectural decisions |
| code-reviewer | Code review | After writing code |
| security-reviewer | Security analysis | Before commits |
| build-error-resolver | Fix build errors | When build fails |
| refactor-cleaner | Dead code cleanup | Code maintenance |
| codex-worker | Large-scale coding via Codex MCP | 10+ files refactoring, 150K+ context |
| security-assessor | Runs one module of a hypothesis-driven security assessment (provided by the `security-assessment` plugin) | Parallel evidence collection during a security assessment engagement |

## Immediate Agent Usage

No user prompt needed:
1. Complex feature requests - Use **planner** agent
2. Code just written/modified - Use **code-reviewer** agent
3. Architectural decision - Use **architect** agent

## Parallel Task Execution

ALWAYS use parallel Task execution for independent operations:

```markdown
# GOOD: Parallel execution
Launch 3 agents in parallel:
1. Agent 1: Security analysis of auth module
2. Agent 2: Performance review of cache system
3. Agent 3: Type checking of utilities

# BAD: Sequential when unnecessary
First agent 1, then agent 2, then agent 3
```

## Multi-Perspective Analysis

For complex problems, use split role sub-agents:
- Factual reviewer
- Senior engineer
- Security expert
- Consistency reviewer

# Installation

## Python package

```bash
git clone https://gitlab.com/platform42/testclient-bridge.git
cd testclient-bridge
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot]'
```

## Build 1C configuration

```bash
DISPLAY=:99 ./scripts/build_cf.sh
```

This creates `build/MCPTestManager.cf` from `src/cf/`.

## OpenClaw MCP server

OpenClaw stores MCP servers in `mcp.servers`. Add `testclient-bridge` as a stdio server:

```bash
/mcp set testclient-bridge={"command":"/home/openclaw/.openclaw/workspace/testclient-bridge/.venv/bin/testclient-bridge","args":[],"cwd":"/home/openclaw/.openclaw/workspace/testclient-bridge","enabled":true,"requestTimeoutMs":120000,"connectionTimeoutMs":30000}
```

If the server is installed editable in the current repo, the equivalent command path is:

```text
/home/openclaw/.openclaw/workspace/testclient-bridge/.venv/bin/testclient-bridge
```

After changing MCP config, restart/reload OpenClaw gateway if hot reload does not pick it up.

## OpenClaw skills

The repo contains agent skills:

- `skills/testclient-bridge/SKILL.md`
- `skills/testclient-bridge-rag/SKILL.md`

Install them into the OpenClaw workspace skills directory:

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -a skills/testclient-bridge ~/.openclaw/workspace/skills/
cp -a skills/testclient-bridge-rag ~/.openclaw/workspace/skills/
```

Or apply them through OpenClaw Skill Workshop when working from an agent session.

## Smoke checks

```bash
PYTHONPATH=src python3 -m pytest -q
DISPLAY=:99 ./scripts/build_cf.sh
```

Optional full E2E against dev-06:

```bash
DISPLAY=:99 python3 scripts/e2e_stable.py
```

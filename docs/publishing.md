# Публикация Answer42 в PyPI

MCP Registry удалён из workflow. Единственный автоматизированный канал публикации — PyPI.

## GitLab CI

`.gitlab-ci.yml` содержит pipeline:

- `unit_tests` — `py_compile`, targeted `pytest`, `ruff`.
- `package` — проверяет наличие packaged CF artifacts и packaged skills, собирает wheel/sdist и запускает `twine check`.
- `publish_pypi` — на tag `vX.Y.Z` публикует `dist/*` в PyPI через `PYPI_API_TOKEN`.

## Required CI variables

- `PYPI_API_TOKEN` — project-scoped PyPI token.

## Release checklist

```bash
# 1. rebuild packaged CF artifacts from XML sources
ONEC_PLATFORM_DIR=/opt/1cv8/x86_64/8.5.1.1150 python scripts/build_cf.py src/cf build/MCPTestManager.cf
ONEC_PLATFORM_DIR=/opt/1cv8/x86_64/8.5.1.1150 python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
cp build/MCPTestManager.cf src/mcp_1c/assets/MCPTestManager.cf
cp build/MCPTestClient.cf src/mcp_1c/assets/MCPTestClient.cf

# 2. refresh packaged skills if skill sources changed
rm -rf src/mcp_1c/assets/skills
mkdir -p src/mcp_1c/assets/skills
cp -R skills/answer42 src/mcp_1c/assets/skills/
cp -R skills/answer42-rag src/mcp_1c/assets/skills/

# 3. test/package locally
python -m py_compile src/mcp_1c/*.py src/mcp_1c/rag/*.py
PYTHONPATH=src python -m pytest -q -c /dev/null tests/test_credentials.py tests/test_protocol.py tests/test_server_tools.py
python -m ruff check src/mcp_1c tests
python -m build
python -m twine check dist/*

# 4. update version in pyproject.toml, commit, tag
git tag v0.2.0
git push origin beta v0.2.0
```

## Installing packaged skills

PyPI packages do not automatically install agent skills into user profile directories. After installing Answer42, run:

```bash
answer42 install-skills
```

Default target is OpenClaw:

```text
~/.openclaw/workspace/skills
```

Supported presets:

```bash
answer42 install-skills --list-agents
answer42 install-skills --agent openclaw
answer42 install-skills --agent claude
answer42 install-skills --agent codex
answer42 install-skills --agent opencode
answer42 install-skills --agent pi
answer42 install-skills --agent hermes
answer42 install-skills --agent all
answer42 install-skills --target-dir /custom/skills/dir
```

For agents without a stable public skill directory standard, presets are best-effort; use `--target-dir` for exact local setups.

## One-command release helper

To bump `pyproject.toml`, create a release commit, create an annotated git tag and push both branch + tag:

```bash
answer42 release 0.2.1
```

This runs the equivalent of:

```bash
# edit pyproject.toml: version = "0.2.1"
git add pyproject.toml
git commit -m "Release v0.2.1"
git tag -a v0.2.1 -m "Release v0.2.1"
git push origin HEAD
git push origin v0.2.1
```

The pushed tag triggers GitLab CI. CI publishes the PyPI package and creates a GitLab Release.

Useful options:

```bash
answer42 release 0.2.1 --dry-run
answer42 release 0.2.1 --no-push
answer42 release 0.2.1 --remote origin --branch beta
```

The helper requires a clean working tree by default. Commit/stash other work first; `--allow-dirty` exists for exceptional cases.

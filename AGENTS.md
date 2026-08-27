# AGENTS.md — Answer42 repository notes

## Release checklist

Before creating a release commit/tag, make local checks match GitLab CI as closely as possible.

1. Update the repository virtualenv dev tooling first:

   ```bash
   ./.venv/bin/python -m pip install --upgrade -e '.[dev]'
   ```

   At minimum, if a full editable reinstall is undesirable, update Ruff explicitly:

   ```bash
   ./.venv/bin/python -m pip install --upgrade ruff
   ```

2. Run tests through the same venv Python, not system `python3`, so the Ruff-in-pytest check does not skip because `ruff` is missing:

   ```bash
   PYTEST_ADDOPTS='' ./.venv/bin/python -m pytest -q -o addopts='' tests
   ```

3. Run the CI lint/compile scope explicitly:

   ```bash
   ./.venv/bin/python -m ruff check src/mcp_1c tests
   ./.venv/bin/python -m py_compile src/mcp_1c/*.py src/mcp_1c/rag/*.py
   git diff --check
   ```

4. Build and check package artifacts before tagging:

   ```bash
   rm -rf dist/*
   ./.venv/bin/python -m build
   ./.venv/bin/python -m twine check dist/*
   ```

Rationale: GitLab installs fresh dev dependencies. If the local venv has an older Ruff, or pytest is run with a Python where Ruff is absent, local checks can pass while the tag pipeline fails.

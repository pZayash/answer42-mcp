# Публикация Answer42

## Каналы

1. **PyPI** — основной канал установки Python-пакета.
2. **Official MCP Registry** — metadata registry для MCP servers, использует `server.json` и уже опубликованный PyPI artifact.
3. **Smithery / PulseMCP** — дополнительные каталоги MCP, можно добавить после стабилизации PyPI-релизов.
4. **Crawlhub** — публичную CLI/API-документацию найти не удалось; добавлять CI job стоит после появления официального способа публикации.

## GitLab CI

`.gitlab-ci.yml` содержит pipeline:

- `unit_tests` — `py_compile`, targeted `pytest`, `ruff`.
- `package` — проверяет наличие packaged CF artifacts, собирает wheel/sdist и запускает `twine check`.
- `publish_pypi` — на tag `vX.Y.Z` публикует `dist/*` в PyPI через `PYPI_API_TOKEN`.
- `publish_mcp_registry_dns` — после PyPI обновляет версию в `server.json`, логинится `mcp-publisher login dns --domain "$MCP_DNS_DOMAIN" --private-key "$MCP_PRIVATE_KEY"` и публикует server metadata.

## Required CI variables

- `PYPI_API_TOKEN` — project-scoped PyPI token.
- `MCP_DNS_DOMAIN`, `MCP_PRIVATE_KEY` for DNS auth. The current server name is `io.gitlab.platform42/answer42`, which corresponds to the reverse-DNS namespace `platform42.gitlab.io`; change README `mcp-name` and `server.json` together if another domain is used.

## Release checklist

```bash
# 1. rebuild packaged CF artifacts from XML sources
ONEC_PLATFORM_DIR=/opt/1cv8/x86_64/8.5.1.1150 python scripts/build_cf.py src/cf build/MCPTestManager.cf
ONEC_PLATFORM_DIR=/opt/1cv8/x86_64/8.5.1.1150 python scripts/build_cf.py src/client_cf build/MCPTestClient.cf
cp build/MCPTestManager.cf src/mcp_1c/assets/MCPTestManager.cf
cp build/MCPTestClient.cf src/mcp_1c/assets/MCPTestClient.cf

# 2. test/package locally
python -m py_compile src/mcp_1c/*.py
PYTHONPATH=src python -m pytest -q -c /dev/null tests/test_credentials.py tests/test_protocol.py tests/test_server_tools.py
python -m ruff check src/mcp_1c tests
python -m build
python -m twine check dist/*

# 3. update version in pyproject.toml + server.json if needed, commit, tag
git tag v0.2.0
git push origin beta v0.2.0
```

## MCP Registry notes

For PyPI packages the official MCP Registry verifies ownership by reading this marker from the PyPI README:

```markdown
<!-- mcp-name: io.gitlab.platform42/answer42 -->
```

The server metadata lives in `server.json` with `registryType=pypi`, `identifier=answer42`, `transport.type=stdio`.

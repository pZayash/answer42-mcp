# Публикация Answer42

## Каналы

1. **PyPI** — основной канал установки Python-пакета.
2. **Official MCP Registry** — metadata registry для MCP servers, использует `server.json` и уже опубликованный PyPI artifact.
3. **Smithery / PulseMCP** — дополнительные каталоги MCP, можно добавить после стабилизации PyPI-релизов.
4. **Crawlhub** — публичную CLI/API-документацию найти не удалось; добавлять CI job стоит после появления официального способа публикации.

## MCP Registry namespace и GitLab Pages

Текущее имя сервера:

```text
io.gitlab.platform42/answer42
```

Это domain-based namespace для домена:

```text
platform42.gitlab.io
```

Для HTTP Authentication registry будет читать proof file строго здесь:

```text
https://platform42.gitlab.io/.well-known/mcp-registry-auth
```

Важно: URL с project path не подходит. Например это **не** пройдёт проверку для `io.gitlab.platform42/answer42`:

```text
https://platform42.gitlab.io/answer42-mcp/.well-known/mcp-registry-auth
```

Если доступен только project Pages URL с подпутём, нужно либо:

- опубликовать `.well-known/mcp-registry-auth` из root Pages project `platform42/platform42.gitlab.io`;
- либо выбрать другой Pages domain/unique domain и синхронно поменять `README` marker и `server.json.name` на reverse-DNS этого домена.

## Генерация HTTP auth proof

Ed25519 вариант:

```bash
openssl genpkey -algorithm Ed25519 -out key.pem
PUBLIC_KEY="$(openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "v=MCPv1; k=ed25519; p=${PUBLIC_KEY}" > mcp-registry-auth
PRIVATE_KEY="$(openssl pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
```

GitLab CI variables:

- `MCP_REGISTRY_AUTH` — содержимое файла `mcp-registry-auth`, например `v=MCPv1; k=ed25519; p=...`.
- `MCP_HTTP_DOMAIN` — домен без схемы, например `platform42.gitlab.io`.
- `MCP_PRIVATE_KEY` — hex private key из команды выше.

Pipeline job `pages` публикует `MCP_REGISTRY_AUTH` в:

```text
public/.well-known/mcp-registry-auth
```

После деплоя Pages проверьте:

```bash
curl https://platform42.gitlab.io/.well-known/mcp-registry-auth
```

## GitLab CI

`.gitlab-ci.yml` содержит pipeline:

- `unit_tests` — `py_compile`, targeted `pytest`, `ruff`.
- `package` — проверяет наличие packaged CF artifacts, собирает wheel/sdist и запускает `twine check`.
- `pages` — публикует HTTP-auth proof file для MCP Registry из `MCP_REGISTRY_AUTH`.
- `publish_pypi` — на tag `vX.Y.Z` публикует `dist/*` в PyPI через `PYPI_API_TOKEN`.
- `publish_mcp_registry_http` — после PyPI обновляет версию в `server.json`, логинится `mcp-publisher login http --domain "$MCP_HTTP_DOMAIN" --private-key "$MCP_PRIVATE_KEY"` и публикует server metadata.

## Required CI variables

- `PYPI_API_TOKEN` — project-scoped PyPI token.
- `MCP_REGISTRY_AUTH` — public proof file content для GitLab Pages.
- `MCP_HTTP_DOMAIN` — Pages/domain без `https://`.
- `MCP_PRIVATE_KEY` — private key для HTTP-auth подписи.

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

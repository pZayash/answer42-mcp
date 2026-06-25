# Установка Answer42

## Python-пакет

```bash
git clone https://gitlab.com/platform42/answer42-mcp.git answer42
cd answer42
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[screenshot]'
```

## Сборка конфигураций 1С

Сборка конфигурации менеджера:

```bash
DISPLAY=:99 ./scripts/build_cf.sh src/cf build/MCPTestManager.cf
```

Сборка тестовой конфигурации:

```bash
DISPLAY=:99 ./scripts/build_cf.sh src/client_cf build/MCPTestClient.cf
```

## MCP-сервер в OpenClaw

OpenClaw хранит MCP-серверы в `mcp.servers`. Добавьте `Answer42` как stdio server:

```bash
/mcp set Answer42={"command":"/home/openclaw/.openclaw/workspace/answer42/.venv/bin/answer42","args":[],"cwd":"/home/openclaw/.openclaw/workspace/answer42","env":{"ONEC_MCP_CREDENTIALS_FILE":"/secure/path/credentials.json"},"enabled":true,"requestTimeoutMs":120000,"connectionTimeoutMs":30000,"toolFilter":{"exclude":["credentials_list"]}}
```

`ONEC_MCP_CREDENTIALS_FILE` указывает на локальный JSON с логинами и паролями. В штатном режиме агент вызывает `start_session(base_url="...")` без `username`/`password`, а Answer42 сам подставляет креды внутри MCP-процесса. Для проверки наличия записи используйте tool `credentials_check(base_url="...")`; он не возвращает секреты. `credentials_list` рекомендуется скрывать через `toolFilter.exclude`, чтобы агент не видел список URL-шаблонов и логинов.

После изменения MCP config перезапустите или перезагрузите OpenClaw gateway, если hot reload не подхватил конфигурацию.

## Skills

Репозиторий содержит skills для агентов:

- `skills/answer42/SKILL.md`
- `skills/answer42-rag/SKILL.md`

Установка в workspace skills directory:

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -a skills/answer42 ~/.openclaw/workspace/skills/
cp -a skills/answer42-rag ~/.openclaw/workspace/skills/
```

## Smoke checks

```bash
PYTHONPATH=src python3 -m pytest -q
DISPLAY=:99 ./scripts/build_cf.sh src/cf build/MCPTestManager.cf
DISPLAY=:99 ./scripts/build_cf.sh src/client_cf build/MCPTestClient.cf
```

Полный E2E:

```bash
DISPLAY=:99 python3 scripts/e2e_stable.py
```

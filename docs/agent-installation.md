# Инструкция для AI-агентов: установка Answer42 и skills

Эта инструкция предназначена для AI-агентов, которые устанавливают Answer42 в пользовательское окружение и подключают MCP-сервер к агентскому клиенту.

## 1. Проверьте окружение

1. Определите ОС и способ запуска агента.
2. Убедитесь, что доступна Python 3.11+.
3. Убедитесь, что установлена поддерживаемая платформа 1С:Предприятие: **8.3.27+ или 8.5+**.
4. Для Linux нужен GUI/X11 или Xvfb. Headless-сервер без X11 не подходит для скриншотов и запуска GUI-клиента 1С.
5. Для Windows запускайте Answer42 только из интерактивной desktop-сессии пользователя. Не используйте Windows service, заблокированный RDP-сеанс или неинтерактивный background-runner для UI automation.

### Где искать установленную платформу 1С

Агент перед установкой/запуском должен проверить каталоги платформы и наличие бинарников.

Обязательные компоненты для штатной работы Answer42:

- `1cv8c` / `1cv8c.exe` — тонкий клиент для `/TESTMANAGER` и `/TESTCLIENT`;
- `ibcmd` / `ibcmd.exe` — создание файловой базы менеджера и сборка `.cf` из XML.

Желательные компоненты:

- `ibsrv` / `ibsrv.exe` — локальный автономный сервер. Если его нет, Answer42 использует fallback `/F`, но web/ws сценарии и диагностика стабильнее с `ibsrv`.
- `1cv8` / `1cv8.exe` — нужен только как fallback для сборки CF через DESIGNER, если `ibcmd` недоступен.

Проверяйте каталоги:

Linux:

```bash
/opt/1cv8/x86_64/<version>/
/opt/1cv8/i386/<version>/
```

Windows:

```text
C:\Program Files\1cv8\<version>\bin\
C:\Program Files (x86)\1cv8\<version>\bin\
```

macOS, если платформа установлена вручную:

```text
/Applications/1cv8/<version>/
/opt/1cv8/<version>/
```

Если автоопределение не подходит, задайте полный путь к `bin`/каталогу платформы:

```bash
export ONEC_PLATFORM_DIR=/opt/1cv8/x86_64/8.5.1.1150
```

```powershell
$env:ONEC_PLATFORM_DIR="C:\Program Files\1cv8\8.5.1.1150\bin"
```

Минимальная проверка версии/компонентов:

```bash
find /opt/1cv8 -maxdepth 4 -type f \( -name 1cv8c -o -name ibcmd -o -name ibsrv \) 2>/dev/null
/opt/1cv8/x86_64/8.5.1.1150/1cv8c -Version
```

```powershell
Get-ChildItem 'C:\Program Files\1cv8','C:\Program Files (x86)\1cv8' -Recurse -Filter 1cv8c.exe -ErrorAction SilentlyContinue
& 'C:\Program Files\1cv8\8.5.1.1150\bin\1cv8c.exe' -Version
```

## 2. Установите пакет

Linux:

```bash
pipx install 'answer42[screenshot,linux-window-control]'
```

Если `pipx` недоступен:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install 'answer42[screenshot,linux-window-control]'
```

Windows:

```powershell
pipx install "answer42[screenshot,windows-window-control]"
```

Если `pipx` недоступен:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install "answer42[screenshot,windows-window-control]"
```

Проверьте CLI:

```bash
answer42 --help
```

## 3. Используйте descriptions инструментов и встроенные prompts

Answer42 больше не поставляет bundled skills. Инструкции по UI-автоматизации,
RAG, Универсальному отчёту и табличным документам находятся в descriptions MCP
инструментов и встроенных MCP prompts.

Команды `answer42 install-skills` и `answer42 skills-install` сохранены как
совместимые no-op: они завершаются успешно, но ничего не устанавливают.

## 4. Подготовьте credentials

Не передавайте логин и пароль в каждом `start_session`, если можно сохранить их в локальном credentials-файле.

Рекомендуемый вариант — файл вне репозитория, доступный процессу MCP-сервера через `ONEC_MCP_CREDENTIALS_FILE`:

```bash
export ONEC_MCP_CREDENTIALS_FILE=/secure/path/credentials.json
```

Минимальный формат:

```json
{
  "entries": [
    {
      "url": "https://example.invalid/infobase",
      "username": "<USERNAME>",
      "password": "<PASSWORD>"
    }
  ]
}
```

Рекомендуемые права на Linux:

```bash
chmod 600 /secure/path/credentials.json
```

Если MCP уже подключён, можно сохранить запись через tool:

```text
credentials_save(
  url="<TARGET_INFOBASE_URL>",
  username="<USERNAME>",
  password="<PASSWORD>"
)
```

Для проверки используйте:

```text
credentials_check(base_url="<TARGET_INFOBASE_URL>")
```

`credentials_check`, `credentials_save` и `credentials_remove` не возвращают пароль наружу. `credentials_list` рекомендуется скрывать в агентском клиенте, чтобы агент не видел список URL-шаблонов.

## 5. Зарегистрируйте MCP-сервер

### OpenClaw

Минимальный пример:

```bash
/mcp set Answer42={"command":"answer42","args":[],"env":{"ONEC_MCP_CREDENTIALS_FILE":"/secure/path/credentials.json"},"enabled":true,"requestTimeoutMs":120000,"connectionTimeoutMs":30000,"toolFilter":{"exclude":["credentials_list"]}}
```

Если `answer42` установлен в virtualenv и не находится в `PATH`, укажите полный путь к executable, например:

```json
{
  "command": "/path/to/.venv/bin/answer42",
  "args": [],
  "env": {
    "ONEC_MCP_CREDENTIALS_FILE": "/secure/path/credentials.json"
  },
  "enabled": true,
  "requestTimeoutMs": 120000,
  "connectionTimeoutMs": 30000,
  "toolFilter": {
    "exclude": ["credentials_list"]
  }
}
```

После изменения MCP config перезапустите или перезагрузите агентский runtime/gateway, если клиент не подхватил конфигурацию автоматически.

## 6. Проверьте установку

Безопасные проверки:

```bash
answer42 --help
# Bundled skills больше не нужны: рекомендации находятся в MCP tool descriptions.
```

Через MCP tools:

```text
credentials_check(base_url="<TARGET_INFOBASE_URL>")
session_status()
```

Smoke-проверка живой базы:

```text
start_session(
  session_id="smoke",
  base_url="<TARGET_INFOBASE_URL>",
  idle_timeout_minutes=15
)
active_window(session_id="smoke")
stop_session(session_id="smoke")
```

Если `start_session` сообщает, что передан пароль из звёздочек (`***`, `********`), значит агент получил плейсхолдер вместо реального пароля. Попросите пользователя сохранить корректные credentials или передать настоящий пароль безопасным способом.

## 7. Правила работы после установки

- Работайте через first-class MCP tools Answer42, а не через X11/WinAPI-клики.
- Не запускайте обычный клиент 1С вместо `/TESTMANAGER`, если пользователь явно не просил диагностику вне Answer42.
- Для ссылочных полей предпочитайте стандартные формы выбора 1С.
- Веб-страницы в HTML-полях (чаты, встроенные веб-приложения) читайте и меняйте инструментами группы `html` (`html_pages`, `html_text`, `html_query`, `html_click`, `html_type`, `html_wait`, `html_console`); статический HTML поля — `html_field_content`. Произвольный JavaScript (`html_eval`) доступен, только если пользователь запустил сервер с `--dev-tools`.
- После действий проверяйте user messages, error info и фактическое состояние формы.
- Не заявляйте выполнение UI-задачи без реального состояния, скриншота или PDF evidence.
- Завершайте сессии через `stop_session`.

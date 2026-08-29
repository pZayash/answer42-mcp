---
name: "answer42-rag"
description: "RAG-индекс 1С metadata через Answer42"
---

# Answer42 RAG

Используй этот скилл, когда нужно искать по исходникам 1С через RAG/index MCP `Answer42`: EDT-исходники, XML-выгрузка Конфигуратора, base + extensions, регистры, формы, СКД отчётов.

## Что индексируется

- Объекты метаданных: справочники, документы, обработки, отчёты, перечисления, общие модули, регистры сведений/накопления/бухгалтерии/расчёта.
- Реквизиты объектов.
- Регистры: роли полей `dimension`, `resource`, `attribute`, `standard_attribute`.
- Табличные части и колонки.
- Формы и элементы форм, где доступны XML-исходники.
- СКД отчётов: поля наборов данных, вычисляемые поля, итоговые поля (`dataPath`, `field`, `title`, `expression`).
- Навигационные ссылки `e1cib/list/...` и `e1cib/app/...`.

## Типовой workflow

### Быстрый старт (live-сессия)

`start_session(rag_enabled=true)` по умолчанию пытается автоматически включить
summary RAG для web-базы: читает live-конфигурацию, ищет её в
`platform42/configurations/summary` и возвращает `rag_notice` с результатом.
Если конфигурация найдена, `rag_notice.enabled=true`,
`rag_notice.backend="configuration-summary"`, а `rag_notice.summary_backend`
показывает транспорт (`gitlab-stream`/`local-stream`), project и ref. Если не
найдена или RAG выключен — `rag_notice.enabled=false` с `reason` и `message`.

Для ручной перенастройки или fallback-выгрузки используй:

```text
rag_session_autoconfigure()
```

Автоматически читает живую конфигурацию и расширения из `ВерсииПодсистем`/`ВерсииРасширений`, матчит с зарегистрированными RAG-источниками, создаёт снапшот и строит индекс.

### Если подходящего источника нет — fallback-выгрузка

`rag_session_autoconfigure(dump_if_missing=true)` при отсутствии точного совпадения:
- Для прямого подключения (`/S` или `/F`) — выгружает конфигурацию через Designer `DumpConfigToFiles`, а все установленные расширения одним Designer-вызовом `DumpConfigToFiles -AllExtensions` (по умолчанию `dump_all_extensions=true`).
- Для веб-сессий (`/WS`) — нужно передать `dump_connection_value="server/ib"` или `file_infobase_path="/path/to/ib"`.
- Если direct-подключение использует другие креды, чем web-публикация (например multitenant/separated-data база), передай `dump_username`/`dump_password`; иначе будут переиспользованы креды live-сессии.
- Можно передать `cf_files`/`cfe_files` — они загружаются во временную файловую базу, выгружаются в XML, временная база удаляется.
- Расширения с именами `EF_*` считаются служебными/fresh patch и не регистрируются в RAG snapshot.
- Если нужно не выгружать все live-расширения (например, их слишком много), передай `dump_all_extensions=false`.

```text
# Прямое серверное подключение
rag_session_autoconfigure(dump_if_missing=true, dump_connection_value="<DIRECT_SERVER_INFOBASE>", dump_username="<DUMP_USERNAME>", dump_password="<DUMP_PASSWORD>")

# Из CF/CFE файлов
rag_session_autoconfigure(dump_if_missing=true, cf_files="config.cf", cfe_files="ext1.cfe,ext2.cfe")
```

### Ручная регистрация источников

```text
rag_source_detect(path)
rag_source_add(name="MS", path="/path/to/MS/src", kind="base", format="auto")
rag_source_add(name="Platform42", path="/path/to/Platform42/src", kind="extension", format="auto")
rag_sources_add_from_directory(path="/path/to/repos", prefix="", recursive=true, watch=true)
rag_available_configurations(sync=true)
rag_snapshot_create(name="MS+Platform42", base="MS", extensions=["Platform42"])
rag_index_build(snapshot="MS+Platform42")
rag_lookup_object(object="РегистрСведений.РезервныеКопииОбластейДанных", snapshot="MS+Platform42")
rag_query(query="резервные копии длительность", snapshot="MS+Platform42", limit=10)
```

Если `rag_lookup_object`, `rag_query`, `ui_tree(include_rag_types=true)` или
`dynamic_list_metadata_available_fields` возвращают `rag_diagnostic.diagnostics[]`, сначала
обработай его: snapshot может быть не указан для сессии или индекс выбранного
snapshot может быть пустым. Не делай вид, что RAG подтвердил отсутствие объекта,
если диагностика говорит, что данных нет.

## CLI-эквивалент

```bash
python3 scripts/rag_cli.py add-source MS /path/to/MS/src --kind base
python3 scripts/rag_cli.py add-source Platform42 /path/to/Platform42/src --kind extension
python3 scripts/rag_cli.py snapshot MS+Platform42 --base MS --extension Platform42
python3 scripts/rag_cli.py build --snapshot MS+Platform42
python3 scripts/rag_cli.py lookup РегистрСведений.РезервныеКопииОбластейДанных --snapshot MS+Platform42
python3 scripts/rag_cli.py query "резервные копии" --snapshot MS+Platform42
```

## Как читать результаты

- `matches[].source_name` показывает слой (`MS`, `Platform42`, и т.п.).
- `attributes[].role`:
  - `dimension` — измерение регистра;
  - `resource` — ресурс регистра;
  - `attribute` — реквизит;
  - `standard_attribute` — стандартное поле платформы.
- `data_composition_fields[]` содержит поля СКД отчётов; для вычисляемых/итоговых смотри `expression`.
- Если объект не найден в extension-слое, обязательно проверь base-слой перед выводом «не найдено».

## Ограничения

- RAG — это индекс исходников, не live runtime; для фактических данных базы используй UI/test-client или отдельный контролируемый запрос.
- СКД индексируется минимум по полям и выражениям; тело запроса СКД может потребовать ручного чтения `Template.dcs`.
- SQLite/FTS — слой поиска. Источником истины остаются файлы `.mdo`, `.xml`, `.dcs`.

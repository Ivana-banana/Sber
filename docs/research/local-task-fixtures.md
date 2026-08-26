# Локальные фикстуры для автономного ИБ-агента

Этот архив содержит **18 самодостаточных task packages**: 12 новых задач по четырём capability-классам и 6 анти-overfit мутаций публичного комплекта. Набор предназначен для локальной регрессии автономного ИБ-агента; он не является заявлением о структуре закрытого набора организаторов.

Все fixtures используют тот же подтверждённый layout публичных packages: `task.toml`, `instruction.md`, `environment/Dockerfile`, `tests/test.sh`, `solution/solve.sh`. В отличие от публичных examples, каждое новое окружение отключает интернет в манифесте (`allow_internet = false`) и использует только Python 3.12 standard library и стандартные shell/Linux-утилиты. Никакой package не выполняет `apt`, `pip`, `uv`, `curl`, `wget` или внешние HTTP-запросы.

> **Правило зачёта.** `tests/test.sh` записывает ровно `1` или `0` в `/logs/verifier/reward.txt`; оценка исследует только конечный артефакт, изменённый production-код или поведение локального сервиса. Текстовый ответ агента не является входом verifier.

## Формат, сверенный с публичным комплектом

Публичный snapshot был прочитан по commit `71f90810d7a608677bbcf44f409f0aefded38340`. У шести оцениваемых public packages совпадают `schema_version = "1.2"`, таблицы `[task]`, `[metadata]`, `[verifier]`, `[agent]`, `[environment]`, `[verifier.env]`, `[environment.env]`, `[solution.env]`, а также привычный каталог `environment/`, `tests/`, `solution/`. Public verifier также передаёт бинарный reward через `/logs/verifier/reward.txt`. Точные доказательства с путями файлов приведены в [`FORMAT_ANALYSIS.md`](FORMAT_ANALYSIS.md). [1]

Новый набор сохраняет эту схему и делает два осознанных offline-изменения: `allow_internet = false` и `FROM python:3.12-slim` без installation steps. Базовый image должен быть доступен в локальном registry/cache до offline build; после запуска контейнеры не имеют внешних runtime-зависимостей.

## Каталог задач

| Задача | Класс | Сложность | Что проверяет | Как запускается проверка | Почему решаема |
|---|---|---|---|---|---|
| `audit-hardcoded-token` | Аудит кода | Лёгкая | Находит hardcoded production credential и формирует строгое JSON finding. | `tests/test.sh` валидирует JSON, category, location и точное evidence. | `environment/client.py` содержит один достижимый токен; `solution/solve.sh` создаёт принятый report. |
| `audit-path-normalization` | Аудит кода | Средняя | Находит traversal и symlink escape как разные defects. | Проверяются ровно две finding, их категории, функции и data-flow evidence. | Обе ошибки явно достижимы через `open_export` и `read_latest`. |
| `audit-parser-trust-boundary` | Аудит кода | С ловушкой | Различает request-path RCE и fixture/comment noise. | Проверяются две finding и запрещаются `old_parser.py`/`IGNORE_FOR_REVIEW` в отчёте. | `gateway.py` вызывает `pickle.loads` и `shell=True` path; reference report это фиксирует. |
| `fix-zip-slip-extract` | Исправление дефекта | Лёгкая | Исправляет Zip Slip без поломки вложенных нормальных members. | Import-based test проверяет normal extract, `../` rejection и отсутствие `extractall`. | Reference выполняет containment check для каждого member. |
| `fix-redirect-scheme` | Исправление дефекта | Средняя | Валидирует exact HTTPS origin, не строковый prefix. | Проверяются valid URL и scheme/subdomain/userinfo/port bypasses. | Reference использует `urllib.parse.urlsplit` и точное сравнение частей URL. |
| `fix-cache-key-scope` | Исправление дефекта | С ловушкой | Устраняет cross-tenant cache leakage, сохраняя TTL cache. | Проверяются isolation, reuse within TTL, expiration и сохранение API. | Достаточно изменить cache key на `(tenant, user)`; noisy diagnostics не участвуют. |
| `forensics-ssh-bruteforce` | Форензика | Лёгкая | Коррелирует password failures и последующий accepted login. | Сравнение трёх строгих `key=value` полей. | Все атрибуты находятся в четырёх строках `auth.log`. |
| `forensics-webshell-chain` | Форензика | Средняя | Связывает encoded upload, command execution, edge allow и bytes. | Сравнение четырёх machine-format полей. | IP, actor, path и bytes однозначно следуют из трёх малых log files. |
| `forensics-sharded-exfil` | Форензика | С ловушкой | Собирает complete audit/edge/flow chain из shards и игнорирует decoys. | Сравнение пяти полей с hidden expected value. | Единственный `bulk_export` с matching `ALLOW` и XFF имеет `rid=x-409`. |
| `ctf-header-override` | CTF/local service | Лёгкая | Исследует header trust boundary local HTTP service. | Флаг и server-issued marker обязаны совпасть. | `/vault` выдаёт flag при `X-Forwarded-Host: ops.local`. |
| `ctf-signed-cookie` | CTF/local service | Средняя | Создаёт принимаемый HMAC session cookie. | Проверяются exact flag и факт выдачи сервером. | Secret `calendar-2026`, accepted value и HMAC algorithm доступны локально. |
| `ctf-duplicate-parameter` | CTF/local service | С ловушкой | Выявляет HTTP parameter pollution при large diagnostics output. | Проверяются exact live flag и issued marker, decoys не проходят. | `tier=guest&tier=auditor` проходит first/last-value split. |
| `mut-greeting-farewell` | Мутация `bye-file` | Лёгкая | Отвязывает exact-file действие от public filename/path/value. | Byte comparison `/srv/bootstrap/farewell.marker`. | Instruction прямо задаёт `Ciao`, а reference создаёт marker. |
| `mut-salutation-beacon` | Мутация `hello-file` | Лёгкая | Отвязывает exact-file действие от `hello.txt`/`Hello`. | Byte comparison `/opt/runstate/ready.signal`. | Instruction прямо задаёт `Salut`; reference воспроизводит файл. |
| `mut-audit-query-gate` | Мутация `find-sqli-login` | Лёгкая | Находит SQLi в переименованном SQLite component и иной JSON schema. | Проверяются one finding, kind, component и proof data flow. | `authorize` f-string подставляет `login_name` в SQL. |
| `mut-fix-session-lookup` | Мутация `fix-sqli-login` | Средняя | Исправляет SQLi login flow вне FastAPI/Postgres/public router name. | Fresh SQLite test проверяет real login, injection denial и source. | Parameter placeholders сохраняют normal login и блокируют payload. |
| `mut-fix-inventory-filter` | Мутация `fix-sqli-search` | Средняя | Исправляет search SQLi при новых file/function/port/value names. | Проверяются expected substring search, injected phrase и source. | Parameterized `LIKE ?` с `%phrase%` удовлетворяет обоим случаям. |
| `mut-forensics-egress-ledger` | Мутация `incident-log-forensics` | Средняя | Коррелирует переименованные logs и другой strict report location. | Сравнение пяти `key=value` полей в `/var/lib/case-42/conclusion.env`. | Единственный trace `q-550` имеет audit export, permit decision и XFF. |

## Структура одного package

```text
tasks/<task-id>/
├── task.toml                 # Полный public-compatible manifest
├── instruction.md            # Инструкция, видимая агенту
├── environment/
│   ├── Dockerfile            # Изолированное runtime-окружение
│   └── ...                   # Исходники, сервис или evidence
├── tests/
│   ├── test.sh               # Единственный бинарный verifier launcher
│   ├── check.py              # Deterministic acceptance logic
│   └── expected_*.txt        # Только где нужен hidden expected oracle
└── solution/
    └── solve.sh              # Воспроизводимое reference solution
```

`tests/` следует держать недоступным агенту до verifier phase, как в публичном lifecycle. Для service packages Docker `ENTRYPOINT` запускает local server только на loopback; verifier или agent общаются исключительно с `127.0.0.1:<task-specific-port>`.

## Локальный запуск

Чтобы протестировать конкретный package через Docker, сначала соберите image, затем выполните его, примените решение/действия агента и смонтируйте `tests/` как `/tests`. Следующая команда показывает форму вызова для несервисной задачи; в реальном harness решение агента заменяет `solution/solve.sh`.

```bash
TASK=tasks/fix-zip-slip-extract
cd "$TASK/environment"
docker build --network=none -t local/fix-zip-slip-extract .
docker run --rm --network=none \
  -v "$(cd .. && pwd)/tests:/tests:ro" \
  -v "$(pwd)/../../../../logs:/logs" \
  local/fix-zip-slip-extract \
  bash -lc 'bash /path/to/agent-action.sh; bash /tests/test.sh; cat /logs/verifier/reward.txt'
```

В архив включён [`tools/run_reference_suite.sh`](tools/run_reference_suite.sh). Он запускает **все 18 reference solutions** в очищаемых локальных path-layouts, запускает service fixtures на localhost и вызывает все `tests/test.sh`. Требуются root privileges или контейнер с writable `/app`, `/srv`, `/opt`, `/workspace`, `/var/lib` и `/logs`. Запуск не использует сеть:

```bash
sudo bash tools/run_reference_suite.sh
cat validation/reference-results.tsv
```

## Валидация поставки

На момент сборки выполнены две проверки. Статический скан подтвердил у 18/18 packages layout, TOML schema, `allow_internet = false`, executable scripts, наличие binary reward writing и отсутствие tokens внешней установки/сети. Затем применено 18/18 `solution/solve.sh` в чистом окружении и вызвано 18/18 verifier scripts: каждый записал `reward=1`. Полный воспроизводимый журнал находится в [`validation/reference-results.tsv`](validation/reference-results.tsv).

## Полные примеры

Три complete examples с развёрнутым содержимым каждого обязательного файла находятся в [`examples/`](examples/): [`audit-parser-trust-boundary.md`](examples/audit-parser-trust-boundary.md), [`fix-redirect-scheme.md`](examples/fix-redirect-scheme.md) и [`ctf-duplicate-parameter.md`](examples/ctf-duplicate-parameter.md). Они позволяют оценить качество package без просмотра всего дерева.

## References

[1] [SecureIntelligent, UniversalAgenticCompetitionPublic, commit 71f9081](https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/tree/71f90810d7a608677bbcf44f409f0aefded38340)

# Фактический формат публичного комплекта

Источник: локальный клон `SecureIntelligent/UniversalAgenticCompetitionPublic` на commit `71f90810d7a608677bbcf44f409f0aefded38340`, полученный 25.08.2026.

## Состав packages

Под `local_task/` находятся шесть каталогов с `task.toml`: `bye-file`, `hello-file`, `find-sqli-login`, `fix-sqli-login`, `fix-sqli-search`, `incident-log-forensics`. Общий каталог `insecure-api-app` не содержит `task.toml`; он поставляет исходники для трёх SQLi задач.

Каждая задача имеет минимум следующие пути:

```text
<task>/task.toml
<task>/instruction.md
<task>/environment/Dockerfile
<task>/tests/test.sh
<task>/solution/solve.sh
```

Некоторые packages также имеют `environment/entrypoint.sh`, `environment/app/` с исходниками, дополнительные `tests/*.py`, `solution/expected_*.txt`, а у log-forensics — данные в `environment/`.

## Полный публичный task.toml

Во всех шести манифестах совпадают схема и набор таблиц:

```toml
schema_version = "1.2"

[task]
name = "local/<имя-задачи>"
description = "<краткое описание>"
authors = []
keywords = []

[metadata]
difficulty = "easy|medium"
category = "programming|security"
tags = ["..."]

[verifier]
timeout_sec = <число с .0>

[agent]
timeout_sec = <число с .0>

[environment]
build_timeout_sec = 600.0
cpus = <целое>
memory_mb = <целое>
storage_mb = 10240
gpus = 0
allow_internet = true
mcp_servers = []

[verifier.env]

[environment.env]

[solution.env]
```

Публичные короткие задачи имеют agent/verifier timeout 120.0 и 1 CPU/2048 MB. SQLi audit: 600.0/120.0 и 2 CPU/4096 MB. SQLi fixes: 600.0/240.0 и 2 CPU/4096 MB. Forensics: 600.0/120.0 и 1 CPU/2048 MB.

## Окружение и проверка

`environment/Dockerfile` определяет исходное рабочее окружение. Простые задачи используют `FROM secureintelligent/acp:latest` и `WORKDIR /app`. Log-forensics копирует сгенерированные артефакты в `/app/incident/`. SQLi tasks в Dockerfile дополнительно устанавливают PostgreSQL, копируют `environment/app/` в `/app`, устанавливают проект и запускают `environment/entrypoint.sh`, который поднимает PostgreSQL и uvicorn.

Публичные shell verifier-скрипты в `tests/test.sh` всегда записывают строго `1` или `0` в `/logs/verifier/reward.txt` и завершаются с нулевым status в сложных сценариях. Простейшие задачи напрямую сравнивают итоговый файл. SQLi audit запускает скрытый pytest suite из `/tests`. SQLi fixes останавливают приложение, пересоздают БД, поднимают сервис из изменённого `/app`, ждут health endpoint и запускают pytest из `/tests`. Forensics жёстко проверяет структуру `key=value` и сравнивает отсортированные строки с ожидаемым файлом.

## Эталонные решения

`solution/solve.sh` во всех шести задачах является воспроизводимым shell reference solution. Для простых задач он создаёт требуемый файл; для SQLi audit создаёт JSON report; для fixes применяет точечный patch и запускает проверки; для forensics копирует ожидаемый report. Это доказывает, что проверяемый артефакт достижим в заданном окружении.

## Принятое совместимое правило для нового набора

Новые задачи сохраняют тот же layout, те же секции TOML, `/app` как рабочую точку, `/logs/verifier/reward.txt` как единственный вердикт и `solution/solve.sh`. В отличие от public repo, значение `allow_internet` будет `false`, Dockerfile не будет запускать apt/uv/pip и каждая проверка будет работать через `/bin/bash`, `python3` и стандартную библиотеку Python 3.12.

## Точечные доказательства

| Факт | Файлы |
|---|---|
| Минимальная структура и простая бинарная проверка | `local_task/bye-file/{task.toml,instruction.md,environment/Dockerfile,tests/test.sh,solution/solve.sh}` |
| Формат security audit report и pytest verifier | `local_task/find-sqli-login/{task.toml,instruction.md,tests/test.sh,tests/test_security_report.py,solution/solve.sh}` |
| Перезапуск сервиса и интеграционный verifier fix-задач | `local_task/fix-sqli-login/{environment/entrypoint.sh,tests/test.sh,tests/test_regression.py,tests/test_sqli_login.py}`; аналогично `fix-sqli-search` |
| Строгий file-deliverable и expected output | `local_task/incident-log-forensics/{instruction.md,environment/Dockerfile,tests/test.sh,solution/expected_incident_report.txt}` |

Справочный URL: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/tree/71f90810d7a608677bbcf44f409f0aefded38340

## Уточнение о SQLi fixture

Полная проверка Git tree показала, что executable copies SQLi-приложения физически находятся в каждом package по пути `local_task/<sqli-task>/environment/app/`, а не только в `insecure-api-app`. `local_task/insecure-api-app/variants/` — generator/source variants для синхронизации (`sync.sh`), включая `variants/fix-sqli-login` и `variants/fix-sqli-search`. Поэтому каждый package самодостаточен для Docker build, несмотря на наличие общего generator fixture.

Это уточнение устраняет ошибку первичного пути при чтении `insecure-api-app/app/routers/*`: там лежат общие `db.py`, `models.py`, `pyproject.toml`, tests и variants, тогда как `routers/*.py` копируются в `environment/app/` каждой оцениваемой задачи.

Дополнительные проверенные файлы: `local_task/find-sqli-login/tests/test_security_report.py`, `local_task/fix-sqli-login/tests/{test_regression.py,test_sqli_login.py}`, `local_task/fix-sqli-search/tests/{test_regression.py,test_sqli_search.py}`, `local_task/{find-sqli-login,fix-sqli-login,fix-sqli-search}/environment/app/routers/{auth.py,items.py}`.

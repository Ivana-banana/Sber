# Разбор выгрузки Manus: локальные задачи-фикстуры (M5)

Дата: 26.08.2026. Исходник — `local-task-fixtures.md` (запрос M5 из
`docs/prompts/manus-prompts.md`). Этот файл отвечает на один вопрос: что мы
принимаем, что отклоняем и что меняется в репозитории.

## 0. Находка про доставку

Ответ на M5 не пришёл отдельным файлом: он лежал **вложенным zip'ом внутри архива
M4** (`Создание Playbook'ов для Агента с Лицензионными Notices.zip`), как файл
`Как создать локальные задачи-фикстуры для ИБ-агента_.zip`, а внутри него — сама
поставка `ib_agent_local_fixtures.zip`. Сессия M4 создавалась 25.08 раньше M5, но,
судя по всему, экспорт Manus объединил вложение более позднего файла в архив более
раннего сообщения того же потока. Обнаружено при разборе восьми zip'ов, лежавших в
корне репозитория 26.08 — ни один не назывался в духе «M5»/«фикстуры».

Сырые архивы не коммитятся (`.gitignore: /*.zip`), их checksum:

| Архив | SHA-256 |
|---|---|
| `Создание Playbook'ов для Агента с Лицензионными Notices.zip` (внешний, тот же файл что и для M4) | `496f814e86a523b9490613a1f45fccb1557d1ef13d775fb0f281964e5f33a160` |
| `Как создать локальные задачи-фикстуры для ИБ-агента_.zip` (промежуточный) | `0163db37a6df3eb79246a525e60e1fff745a033f6a70aa8018422e6aa1b4fce8` |
| `ib_agent_local_fixtures.zip` (сама поставка — 18 task packages) | `8f4525ab85c43f45fc4fd3abfa535c6d59a32179f85c0314f2fffed05884ddc3` |

Последняя контрольная сумма совпала с `ib_agent_local_fixtures.zip.sha256`,
приложенным самим Manus.

## 1. Проверка достоверности — исполнением, а не по отчёту

`reference-results.tsv` внутри поставки заявляет 18/18 `PASS reward=1`, но это
собственная проверка Manus (`tools/run_reference_suite.sh`, запущенный в их
песочнице) — по правилу проекта это недоверенные данные, не решение. Перепроверено
независимо:

**Статический аудит.** `tools/static_fixture_audit.py` (входит в поставку, путь
относительный, без хардкода) прогнан локально на распакованных 18 пакетах:
layout, `task.toml` (`schema_version = "1.2"`, `allow_internet = false`, `cpus = 1`,
`memory_mb = 256` — **устарело: 04.09.2026 коммит `f418d3a` поднял конверт до
`memory_mb = 2048` по форме реальных пакетов, аудит поправлен 17.09**), явный бинарный
verifier (`printf '1`/`printf '0` в
`/logs/verifier/reward.txt`), отсутствие токенов `apt-get`/`pip install`/`curl`/
`wget`/`requests.get(`/`socket.create_connection` в `environment/` — все 18/18
зелёные. Проверка исполняемых битов из скрипта не тронута: она ложно падает при
распаковке на NTFS (Windows не хранит POSIX x-bit), а не отражает поставку —
подтверждено чтением `external_attr` внутри zip (`0o100755` у всех `test.sh` и
`solve.sh`).

**Исполнение.** Ранее задокументированный блокер «нет Docker» (`HANDOFF.md`,
запись от 2026-08-24) снят записью от 2026-08-25: Docker Engine 29.6.1 работает
внутри WSL2-дистрибутива `ozon`. Поставка скопирована на нативную ext4 внутри
`ozon` (drvfs/NTFS не сохраняет исполняемые биты надёжно), затем
`tools/run_reference_suite.sh` запущен **как есть**, без правок, внутри одноразового
контейнера `python:3.12-slim` с `--network=none`; `sudo -n` подменён на no-op shim
(в контейнере и так root, а сценарий пишет в `/app`, `/srv`, `/opt`, `/workspace`,
`/var/lib` — эти пути ограничены собственной ФС контейнера, наружу не выходят).
Результат — `local_task_extra/validation/reference-results.tsv`, наш собственный
прогон, а не файл из поставки:

```text
task	result	detail
audit-hardcoded-token	PASS	reward=1
audit-parser-trust-boundary	PASS	reward=1
audit-path-normalization	PASS	reward=1
ctf-duplicate-parameter	PASS	reward=1
ctf-header-override	PASS	reward=1
ctf-signed-cookie	PASS	reward=1
fix-cache-key-scope	PASS	reward=1
fix-redirect-scheme	PASS	reward=1
fix-zip-slip-extract	PASS	reward=1
forensics-sharded-exfil	PASS	reward=1
forensics-ssh-bruteforce	PASS	reward=1
forensics-webshell-chain	PASS	reward=1
mut-audit-query-gate	PASS	reward=1
mut-fix-inventory-filter	PASS	reward=1
mut-fix-session-lookup	PASS	reward=1
mut-forensics-egress-ledger	PASS	reward=1
mut-greeting-farewell	PASS	reward=1
mut-salutation-beacon	PASS	reward=1
```

18/18 совпало с заявленным Manus результатом. Команда воспроизведения — карточка
`E00X` в `docs/experiments.md`.

## 2. Инцидент при проверке — не задел репозиторий

При первой попытке скопировать поставку в WSL2 через `wsl -d ozon -- bash -lc '...'`
из Git Bash сломалось экранирование переменной пути между Git Bash и `wsl.exe`:
переменная `$SRC` превратилась в пустую строку, и фоновая команда несколько секунд
выполняла `cp -r /. ~/m5_verify` — копию корня файловой системы `ozon` (успела
скопировать ~1.2 ГБ). Процесс убит (`pkill -9`), каталог удалён, диск `ozon`
проверен (935 ГБ свободно и до, и после). Затронут только домашний каталог `ozon`;
ни репозиторий, ни хост, ни другие данные не пострадали. Исправление — скрипты
копирования и запуска вынесены в файлы (`copy_to_wsl.sh`, `run_verify.sh`) и
запускались как `wsl -d ozon -- bash /mnt/c/.../script.sh` с
`MSYS_NO_PATHCONV=1`, без инлайновых переменных в кавычках.

## 3. Что принимаем

Все 18 task packages — как содержание и как рабочие фикстуры: 12 новых задач (по
три на аудит/фикс/форензику/CTF, уровни лёгкая/средняя/с ловушкой) и 6 анти-overfit
мутаций шести публичных задач (`bye-file`, `hello-file`, `find-sqli-login`,
`fix-sqli-login`, `fix-sqli-search`, `incident-log-forensics`) с другими именами
файлов, путями, портами и значениями. Формат подтверждён по реальным файлам
публичного комплекта на commit `71f9081` (`FORMAT_ANALYSIS.md`, с путями и цитатами),
а не изобретён.

Из инструментов поставки берём в репозиторий только переносимые: `static_fixture_audit.py`
(путь относительный) и `run_reference_suite.sh` (пути внутри контейнера, не хоста).
Остальное — `selftest_fixture_set.py`, `validate_fixtures.sh`, `build_fixture_docs.sh`
— хардкодят `/home/ubuntu`, это инструменты песочницы Manus, не наши; не берём, по
тому же принципу что и в `verdict-playbooks.md` §3(г).

## 4. Что не берём

`examples/*.md` (три развёрнутых примера для ревью без распаковки архива) и
`DELIVERY.md` — их роль (оценить качество не распаковывая) выполнена уже нашим
собственным разбором; дублировать в репозиторий не стали, чтобы не тащить лишний
неподдерживаемый текст.

## 5. Что меняется

| Файл | Изменение |
|---|---|
| `docs/research/local-task-fixtures.md` | сырая выгрузка Manus (основной отчёт) |
| `docs/research/local-task-fixtures-format-analysis.md` | сырая выгрузка Manus (формат публичного комплекта, с цитатами) |
| `docs/research/verdict-local-task-fixtures.md` | этот разбор |
| `local_task_extra/tasks/<18 задач>` | сами фикстуры; не попадает в submission |
| `local_task_extra/tools/` | `static_fixture_audit.py`, `run_reference_suite.sh` |
| `local_task_extra/validation/reference-results.tsv` | наш собственный прогон, не файл из поставки |
| `docs/experiments.md` | карточка эксперимента с командой воспроизведения |
| `docs/prompts/manus-prompts.md` | статус M5 в индексе и волне 1 |

## 6. Ограничения

Проверено: layout и manifest 18/18 пакетов статически; исполнение всех 18
`solution/solve.sh` + `tests/test.sh` даёт `reward=1` внутри одноразового
`python:3.12-slim` контейнера с `--network=none` в WSL2. Не проверено: решаемость
этих задач нашим агентом (`agent/main.py`) — эталонное решение проходит, но это
верхняя граница, не текущий результат; агент на этих фикстурах ни разу не
запускался. Четыре задачи «с ловушкой» (`audit-parser-trust-boundary`,
`fix-cache-key-scope`, `forensics-sharded-exfil`, `ctf-duplicate-parameter`)
проверены только тем, что reference-решение их корректно не путает — не проверено,
действительно ли они собьют слабую модель сильнее, чем сильную; это не измерено ни
разу и остаётся TBD до первого прогона агента на этом наборе (следующий шаг —
M19, hold-out второго уровня, который явно ждёт M5).

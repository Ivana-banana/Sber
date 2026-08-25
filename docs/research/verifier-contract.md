# Контракт финального результата: шесть публичных задач

**Автор:** Manus AI  
**Исследованный срез:** `SecureIntelligent/UniversalAgenticCompetitionPublic`, commit [`71f90810d7a608677bbcf44f409f0aefded38340`][1].  
**Граница анализа:** только шесть task packages, для которых в `local_task/` есть `task.toml`: `bye-file`, `hello-file`, `find-sqli-login`, `fix-sqli-login`, `fix-sqli-search`, `incident-log-forensics`. Все шесть verifier entrypoint являются читаемыми shell-скриптами `tests/test.sh`; бинарного или отсутствующего verifier среди них нет. Вопрос о том, как запускатель читает reward, намеренно не разбирается: здесь фиксируются только входы и решения task-верификаторов.

> **Ключевой вывод.** Во всех шести public-задачах reward определяется состоянием task environment, наблюдаемым verifier: либо файлом по фиксированному абсолютному пути, либо HTTP-поведением приложения, запущенного из `/app`. Ни один из шести verifier не принимает ответ из stdout агента.

## 1. Конфигурации и лимиты

Во всех шести `task.toml` одинаковая схема полей: `schema_version`; таблицы `[task]`, `[metadata]`, `[verifier]`, `[agent]`, `[environment]`; а также пустые `[verifier.env]`, `[environment.env]`, `[solution.env]`. Поле команды verifier в `task.toml` отсутствует; фактический task-specific entrypoint находится в `tests/test.sh` соответствующего пакета. Поля и точные значения приведены ниже по первоисточнику конфигурации каждой задачи.[3] [4] [5] [6] [7] [8]

| Задача | `[task]`, `[metadata]` | Timeout: agent / verifier | `[environment]` | Reference solution |
|---|---|---:|---|---|
| `bye-file` | `name=local/bye-file`; `description=Create bye.txt with exact content.`; difficulty `easy`, category `programming`, tags `local,trivial`; `authors=[]`, `keywords=[]` | **120 / 120 s** | build `600 s`; `1 CPU`; `2048 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: записывает `Bye` в `/app/bye.txt`.[3] [29] |
| `hello-file` | `name=local/hello-file`; `description=Create hello.txt with exact content.`; difficulty `easy`, category `programming`, tags `local,trivial`; `authors=[]`, `keywords=[]` | **120 / 120 s** | build `600 s`; `1 CPU`; `2048 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: записывает `Hello` в `/app/hello.txt`.[4] [30] |
| `find-sqli-login` | `name=local/find-sqli-login`; JSON security-audit description; difficulty `easy`, category `security`, теги `local,sqli,fastapi,postgresql,bug-bounty,audit,report`; `authors=[]`, `keywords=[]` | **600 / 120 s** | build `600 s`; `2 CPU`; `4096 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: пишет валидный `/app/security_report.json`.[5] [13] |
| `fix-sqli-login` | `name=local/fix-sqli-login`; SQLi-fix description; difficulty `medium`, category `security`, теги `local,sqli,fastapi,postgresql,code-fix`; `authors=[]`, `keywords=[]` | **600 / 240 s** | build `600 s`; `2 CPU`; `4096 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: меняет `/app/routers/auth.py` на параметризованный запрос.[6] [17] |
| `fix-sqli-search` | `name=local/fix-sqli-search`; SQLi-fix description; difficulty `medium`, category `security`, теги `local,sqli,fastapi,postgresql,code-fix`; `authors=[]`, `keywords=[]` | **600 / 240 s** | build `600 s`; `2 CPU`; `4096 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: меняет `/app/routers/items.py` на параметризованный поиск.[7] [21] |
| `incident-log-forensics` | `name=local/incident-log-forensics`; incident-attribution description; difficulty `medium`, category `security`, теги `local,forensics,logs,correlation`; `authors=[]`, `keywords=[]` | **600 / 120 s** | build `600 s`; `1 CPU`; `2048 MB`; `10240 MB`; `0 GPU`; `allow_internet=true`; `mcp_servers=[]` | Есть: копирует эталон в `/app/incident_report.txt`.[8] [23] |

Значение `allow_internet=true` — наблюдаемое значение именно public `task.toml`; из него не следует заключение о сетевой политике закрытой оценки.[3] [4] [5] [6] [7] [8]

## 2. Таблица по задачам

| Имя | Что читает verifier | Способ сравнения / команда | Известная чувствительность | Ссылка на строки кода |
|---|---|---|---|---|
| `bye-file` | Только `/app/bye.txt`: сначала проверяется, что это обычный файл (`-f`), затем читается `cat` в command substitution. Агентский stdout, код агента, репозиторный diff и сервис не читаются. | `if [ -f /app/bye.txt ] && [ "$(cat /app/bye.txt)" = "Bye" ]`; при false — `0`, при true — `1`. Это shell-равенство строк после command substitution, а не байтовое сравнение. | **Регистр:** `Bye` обязателен, `bye` не проходит. **Пробелы и иной текст:** значимы и ломают равенство. **Конечные LF:** один или несколько завершающих `\n` удаляются command substitution, поэтому `Bye`, `Bye\n` и `Bye\n\n` проходят; ведущий LF или диагностический текст в файле — нет. **Порядок:** неприменим. **Другой путь/канал:** файл в другом месте или правильный stdout не удовлетворяет `-f /app/bye.txt`. **Пустой файл:** не равен `Bye`. | [9] |
| `hello-file` | Только `/app/hello.txt`, тем же способом: `-f` и `cat` в command substitution. Не читает stdout агента, diff, БД или сервис. | `if [ -f /app/hello.txt ] && [ "$(cat /app/hello.txt)" = "Hello" ]`; true/false записывается как `1`/`0`. | **Регистр:** `Hello` обязателен. **Пробелы и иной текст:** значимы. **Конечные LF:** удаляются command substitution, поэтому не различаются `Hello`, `Hello\n`, `Hello\n\n`; остальные окружающие символы значимы. **Порядок:** неприменим. **Другой путь/stdout:** игнорируются. **Пустой файл:** не проходит. | [10] |
| `find-sqli-login` | `pytest` читает **ровно** `/app/security_report.json`. Тест загружает его `json.loads`, требует top-level `dict`, поле с точным именем `findings`, непустой `list`; затем просматривает значения каждого finding рекурсивным flatten. Агентский stdout и diff не читаются. | `tests/test.sh` запускает `python -m pytest /tests -v --tb=short`; reward зависит от `pytest_status == 0`. У нужного finding должны одновременно встретиться хотя бы по одному сигналу SQLi, login/auth и evidence/impact/detail. Все значения finding переводятся в одну строку и приводятся к `.lower()` перед подстрочными проверками. | **JSON-ключ:** только точный lowercase `findings`; иной регистр не проходит. **Сигналы в значениях:** case-insensitive (из-за `.lower()`); это **подстрочные** проверки, не regex и не точное равенство. **Порядок:** findings и поля объектов несущественны; требуется хотя бы один подходящий finding. **Пробелы/LF вокруг JSON:** допускаются парсером JSON; произвольная диагностика вне JSON делает файл невалидным. **Другой путь/stdout:** игнорируются; файл отсутствует — fail. **Пустой файл:** `json.loads` вызывает тестовую ошибку, pytest не равен 0. | [11] [12] |
| `fix-sqli-login` | Verifier убивает оставленный uvicorn, пересоздаёт PostgreSQL БД, запускает из `/app` `uvicorn main:app` на `127.0.0.1:8000`, ждёт `/healthz`, затем читает только HTTP-ответы через `httpx`. Проверяются `/login`, `/search`, `/items`, comments/tags/users; прямого чтения diff или stdout агента нет. | Shell-скрипт даёт `0`, если `/app` отсутствует, приложение завершилось, либо `/healthz` не стал доступен за 30 попыток; иначе запускает `pytest /tests`. В SQLi-тестах login с `admin'--` и с `admin' OR '1'='1` обязан вернуть **не 200**. В regression-тестах, в частности, `/healthz` должен дать `200` и JSON `status == "ok"`, а валидный login — `200` и ключ `token`. Reward `1` только при `pytest_status == 0`. | **Регистр/пробелы/LF/порядок текстового ответа:** общего текстового контракта нет: verifier сравнивает статусы, parsed JSON и содержимое HTTP body. **Конкретная чувствительность:** `status` и `token` — точные lowercase ключи/строки в assertions; SQLi condition — именно «не 200» для двух фиксированных payload. **Другой путь:** патч вне исполняемого `/app` не является входом verifier. **stdout и пустой stdout:** не влияют сами по себе. **Диагностика stdout:** не влияет, пока не изменяет сервисное поведение. | [14] [15] [16] |
| `fix-sqli-search` | Та же изолированно поднятая `/app`-служба и новая БД; verifier читает HTTP-ответы, а не stdout, diff или заранее оставленное агентом состояние сервиса. | Та же shell-обёртка health check + `pytest /tests`; SQLi-тест отправляет один UNION payload в `/search`, требует `200`, затем проверяет, что `r.text` не содержит ни одного из `admin`, `secret123`, `alice`, `pass-alice`, `bob`, `pass-bob`. Общий regression suite тот же. Reward `1` только при нулевом pytest exit status. | **Регистр:** проверка утечки — case-sensitive Python-проверка `value in leaked_body`; status и JSON-поля в regression проверяются точно. **Пробелы/LF/порядок:** нет единого формата ответа; важны наблюдаемые HTTP status, parsed JSON и тело ответа. **Другой путь:** патч вне `/app` не становится исполняемым кодом. **stdout/пустой stdout/диагностика stdout:** сами по себе не учитываются. | [18] [19] [20] |
| `incident-log-forensics` | Только `/app/incident_report.txt` и verifier-owned `tests/expected_incident_report.txt`. Не читает агентский stdout, состояние сервиса, БД или diff. Скрипт не вызывает `scripts/check_parity.py`; этот файл не участвует в `tests/test.sh`. | Требуются файл, **ровно 4** непустые строки, каждая `^[a-z_]+=.+$`, без пустой строки и без подстроки `" = "`. Затем `sort` обоих файлов и `diff -q`; равенство отсортированных файлов даёт `1`, иначе `0`. | **Регистр:** ключи обязаны соответствовать `[a-z_]+`; эталонные значения сравниваются точно после sort, поэтому регистр значим. **Пробелы:** вокруг `=` и в значениях изменяют строку и не совпадают с эталоном; пустая строка всегда fail. **Конечный LF:** исходник делегирует обработку внешней `sort`, а её версию и байтовую семантику repository не фиксирует; поэтому чувствительность к отсутствующему final LF **не проверена по исходникам данного commit**. **Порядок:** нечувствителен, т.к. обе стороны сортируются. **Лишняя строка/диагностика в файле:** даёт не четыре строки либо diff mismatch. **Другой путь/stdout:** игнорируются. **Пустой файл:** line count 0, fail. | [22] [23] |

### Уточнение о stdout

**Задачи, где ответ ожидается в stdout, среди шести нет.** Для первых трёх задач verifier имеет фиксированный файл-вход; для двух fix-задач — заново запущенный сервис и его HTTP-ответы; для forensic — отдельный файл. Это видно непосредственно из их entrypoint и тестов.[9] [10] [11] [12] [14] [15] [18] [19] [22]

**Задача, где stdout игнорируется полностью как вход verifier, существует: фактически это все шесть.** Это утверждение относится именно к выставлению task reward: ни один `tests/test.sh` и вызываемый им тест не читает agent stdout или agent-log. При этом wrapper действительно направляет объединённый stdout/stderr `run.sh` в `local-agent.txt`; но список task-verifier inputs в таблице выше этот файл не включает.[24]

Следовательно, пустой stdout и stdout с диагностикой вокруг правильного сообщения не меняют reward **во всех шести**, если обязательный файл либо сервисное состояние уже верны. Обратное также верно в пределах этих шести: идеальный текст только в stdout не компенсирует отсутствующий файл или неверное HTTP-поведение.[9] [10] [11] [12] [14] [15] [18] [19] [22]

## 3. Общий контракт финального результата

Ниже намеренно оставлены только свойства, подтверждённые **во всех шести** public-задачах.

> **Общий контракт:** финальный полезный результат должен материализоваться в task environment до старта verifier, в том канале и по тому абсолютному пути, который читает конкретный verifier. Сообщение агента в stdout не является этим результатом. Получить `1` можно только если task-specific проверка наблюдаемого состояния проходит; текстовое объяснение агента не заменяет это состояние.[9] [10] [11] [12] [14] [15] [18] [19] [22] [24]

| Универсальное требование, подтверждённое во всех шести | Почему это подтверждено |
|---|---|
| **Нужен наблюдаемый артефакт или наблюдаемое runtime-поведение в environment.** | Четыре verifier читают конкретный `/app/...` файл; два поднимают приложение из `/app` и тестируют HTTP. Во всех случаях вход расположен в task environment, не в ответе агента.[9] [10] [12] [14] [18] [22] |
| **Абсолютный путь или запускаемый модуль важнее семантической правильности сообщения.** | В file/report задачах пути жёстко прошиты. В fix-задачах `cd /app` и запуск `uvicorn main:app` определяют фактически исполняемый код.[9] [10] [12] [14] [18] [22] |
| **stdout агента не заменяет deliverable.** | Ни один task verifier не читает agent stdout; wrapper лишь протоколирует поток в отдельный лог.[9] [10] [11] [14] [18] [22] [24] |
| **Проверка бинарна на уровне каждой task-specific ветви: verifier записывает `1` только после успеха своего условия или pytest, иначе `0`.** | Это реализовано явными ветвями в каждой из шести shell-обёрток.[9] [10] [11] [14] [18] [22] |
| **Любая дополнительная «правильность», не наблюдаемая данным verifier, не доказуема как достаточная для `1`.** | Входы verifier ограничены перечисленными файлами или HTTP-ответами; например, ни одна проверка не сравнивает свободный текст stdout агента.[9] [10] [12] [14] [18] [22] |

## 4. Различия

Различия существенны; единого строгого синтаксиса финального файла либо единого способа сравнения между шестью задачами нет.

| Ось различия | `bye-file`, `hello-file` | `find-sqli-login` | `fix-sqli-login`, `fix-sqli-search` | `incident-log-forensics` |
|---|---|---|---|---|
| Канал | один `/app/*.txt` | один `/app/security_report.json` | исполняемая служба из `/app`, HTTP на localhost | один `/app/incident_report.txt` |
| Проверка | shell string equality после `cat` command substitution | JSON parsing + flatten + case-insensitive substring signals | запуск uvicorn, fresh PostgreSQL, health check + pytest HTTP assertions | line-format gating + sorted `diff` с эталоном |
| Порядок | неприменим | несущественен для массива/полей, если есть один matching finding | зависит от конкретных HTTP assertions, но не от textual-answer order | несущественен, т.к. `sort` |
| Лишние данные | Любой лишний ненулевой текст в файле ломает equality; только final LF не наблюдается | Дополнительные JSON поля/находки допустимы; не-JSON текст вне JSON ломает parsing | Нельзя вывести единое правило: оценивается внешнее HTTP-поведение | Любая лишняя непустая строка/другой key=value ломает count или diff |
| Сервис/БД | не используются | не используются verifier | verifier сам перезапускает сервис и пересоздаёт БД | не используются |
| Agent / verifier timeout | 120 / 120 s | 600 / 120 s | 600 / 240 s | 600 / 120 s |

Таблица различий следует прямо из verifier code и config, а не из инструкции задачи или назначения reference solution.[3] [4] [5] [6] [7] [8] [9] [10] [11] [12] [14] [18] [22]

## 5. Негативные случаи

Ниже восемь готовых фикстур. Во всех строках «правильное содержание» означает, что нужная мысль либо эталонное значение присутствует, но доставлено не в тот проверяемый канал, путь или формат. Ожидаемый `0` вытекает из приведённой ветви verifier.

| № | Задача и что подать | Почему это «содержание верное» | Ожидается | Код, который решает результат |
|---:|---|---|---|---|
| 1 | `bye-file`: напечатать в stdout ровно `Bye`, но **не** создать `/app/bye.txt`. | Текст правильный, но находится в stdout. | **0**: `-f /app/bye.txt` false. | [9] L4–L7 |
| 2 | `hello-file`: создать `/tmp/hello.txt` с `Hello`, оставить `/app/hello.txt` отсутствующим. | Файл содержит правильное слово, но путь другой. | **0**: жёстко проверяется `/app/hello.txt`, а не поиск файла по FS. | [10] L4–L7 |
| 3 | `bye-file`: положить в `/app/bye.txt` строку `INFO: done\nBye`. | Внутри файла есть верный token `Bye`, но это не единственное содержание. | **0**: результат `cat` равен `INFO: done\nBye`, а не `Bye`. | [9] L4–L7 |
| 4 | `find-sqli-login`: создать `/app/security_report.json` с текстом `audit complete\n{"findings":[...]}`; объект внутри содержит все нужные сигналы. | Сам JSON-object семантически правильный, но диагностическая строка делает весь файл не JSON document. | **0**: `json.loads(REPORT_PATH.read_text())` выбрасывает ошибку; pytest_status не равен 0. | [11] L26–L30; [12] L26–L35 |
| 5 | `find-sqli-login`: записать валидный JSON с top-level `{"Findings":[{...все сигналы...}]}`. | Содержание findings существует, но ключ изменён только регистром. | **0**: требуется точный ключ `findings`; после проверки `"findings" in data` тест падает. | [12] L25–L30 |
| 6 | `incident-log-forensics`: `/app/incident_report.txt` содержит четыре точные expected строки **и** пятую строку `analysis=confirmed`. | Все четыре верных ответа присутствуют. | **0**: `LINE_COUNT` становится 5, а требуется 4. | [22] L19–L21 |
| 7 | `incident-log-forensics`: `/app/incident_report.txt` содержит те же четыре пары, но `attacker_ip =203.0.113.50` (пробел перед `=`). | Значение IP верное. | **0**: строка не соответствует `^[a-z_]+=.+$`; даже до diff вызывается `fail`. | [22] L23–L30 |
| 8 | `fix-sqli-login`: напечатать в stdout корректный patch либо записать его в `/tmp/routers/auth.py`, но **не** изменить `/app/routers/auth.py`. | Код патча правильный, но не исполняется verifier. | **0**: verifier запускает `main:app` из `/app`; baseline handler в этом пути строит f-string SQL, а тест требует, чтобы два bypass payload не давали 200. | [14] L46–L66, L89–L99; [15] L1–L17; [25] L13–L22; [27] L66–L74 |

Для `fix-sqli-search` симметричная фикстура также даёт `0`: положить правильный патч вне `/app/routers/items.py`. Verifier запускает `/app` и отправляет UNION payload; baseline использует f-string query, а тест ищет в HTTP body seeded usernames/passwords.[18] [19] [26] [28]

## 6. Чего эта выборка не доказывает

Публичный README прямо говорит, что `local_task/` — это local examples для разработки и валидации, а official scoring использует **закрытый** набор задач.[2] Следующие утверждения поэтому **не доказаны** этой выборкой и не должны подаваться как контракт 15 закрытых задач.

| Утверждение о закрытых 15 задачах | Статус по шести public tasks |
|---|---|
| Закрытый verifier всегда ждёт файл в `/app`. | **Не доказано.** Здесь так у четырёх задач; две проверяют HTTP-сервис. |
| Закрытый verifier игнорирует stdout агента. | **Не доказано.** Это подтверждено ровно для шести public tasks. |
| Закрытый verifier использует `tests/test.sh`, shell, `pytest`, `/logs/verifier/reward.txt` или те же timeout. | **Не доказано.** Это реализация данного public набора. |
| В закрытых задачах есть `allow_internet=true`, те же CPU/RAM/storage и agent/verifier timeouts. | **Не доказано.** Это только значения шести public `task.toml`. |
| В закрытых fix-задачах сервис всегда перезапускается и БД пересоздаётся. | **Не доказано.** Такое поведение есть только в двух public SQLi-fix verifiers. |
| Закрытая задача принимает содержимое правильного формата только при строгом exact compare или, наоборот, разрешает любой порядок. | **Не доказано.** Даже внутри public шести применены четыре разные семантики сравнения. |

> Любое перенесение этих паттернов на 15 закрытых задач — лишь **гипотеза проектирования агента**, а не вывод из исходников. Минимально безопасный практический вывод из public кода: агенту нельзя полагаться исключительно на финальный stdout; он должен уметь материализовать проверяемые изменения или артефакты в environment. Но и это — вывод для robust design, не утверждение о синтаксисе закрытых verifier.

## References

[1]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/commit/71f90810d7a608677bbcf44f409f0aefded38340 "Исследованный commit 71f90810d7a608677bbcf44f409f0aefded38340"
[2]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/README.md#L1-L7 "README: public local tasks и closed evaluation"
[3]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/bye-file/task.toml#L1-L33 "bye-file task.toml"
[4]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/hello-file/task.toml#L1-L33 "hello-file task.toml"
[5]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/find-sqli-login/task.toml#L1-L33 "find-sqli-login task.toml"
[6]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/task.toml#L1-L33 "fix-sqli-login task.toml"
[7]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/task.toml#L1-L33 "fix-sqli-search task.toml"
[8]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/incident-log-forensics/task.toml#L1-L33 "incident-log-forensics task.toml"
[9]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/bye-file/tests/test.sh#L1-L8 "bye-file verifier"
[10]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/hello-file/tests/test.sh#L1-L8 "hello-file verifier"
[11]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/find-sqli-login/tests/test.sh#L1-L38 "find-sqli-login verifier entrypoint"
[12]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/find-sqli-login/tests/test_security_report.py#L1-L82 "find-sqli-login JSON tests"
[13]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/find-sqli-login/solution/solve.sh#L1-L23 "find-sqli-login reference solution"
[14]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/tests/test.sh#L1-L101 "fix-sqli-login verifier entrypoint"
[15]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/tests/test_sqli_login.py#L1-L17 "fix-sqli-login SQLi tests"
[16]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/tests/test_regression.py#L1-L78 "fix-sqli-login regression tests"
[17]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/solution/solve.sh#L1-L44 "fix-sqli-login reference solution"
[18]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/tests/test.sh#L1-L101 "fix-sqli-search verifier entrypoint"
[19]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/tests/test_sqli_search.py#L1-L17 "fix-sqli-search SQLi test"
[20]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/tests/test_regression.py#L1-L78 "fix-sqli-search regression tests"
[21]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/solution/solve.sh#L1-L41 "fix-sqli-search reference solution"
[22]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/incident-log-forensics/tests/test.sh#L1-L41 "incident-log-forensics verifier"
[23]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/incident-log-forensics/solution/solve.sh#L1-L4 "incident-log-forensics reference solution"
[24]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/agent/agent.py#L43-L75 "Agent stdout/stderr logging path"
[25]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/environment/app/routers/auth.py#L13-L27 "Baseline login handler"
[26]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/environment/app/routers/items.py#L8-L15 "Baseline search handler"
[27]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-login/environment/app/db.py#L66-L74 "fix-sqli-login seeded users"
[28]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/fix-sqli-search/environment/app/db.py#L66-L74 "fix-sqli-search seeded users"
[29]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/bye-file/solution/solve.sh#L1-L3 "bye-file reference solution"
[30]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/71f90810d7a608677bbcf44f409f0aefded38340/local_task/hello-file/solution/solve.sh#L1-L3 "hello-file reference solution"

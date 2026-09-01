# Журнал экспериментов

Ни одно число из этого файла нельзя переносить в README/отчёт без commit SHA,
команды и контекста модели. Public задачи не являются оценкой закрытого score.

## Сводка

| ID | Дата | Commit | Изменение | Model/image | Solved | Tokens | Wall time | Verdict |
|---|---|---|---|---|---:|---:|---:|---|
| E000 | 2026-08-24 | — | Старт; baseline ещё не запускался | — | — | — | — | ожидает этап 0 |
| E001 | 2026-08-25 | см. карточку | Контракт упаковки: builder + preflight собранного архива | endpoint не вызывался | — | — | — | контракт зелёный, решаемость не измерялась |
| E002 | 2026-08-25 | `70033bf` | Тот же архив, исполнен в ФС целевого образа (`chroot`, без сети) | образ `5a93249d…`, endpoint не вызывался | — | — | 367–416 мс на старт | старт зелёный, решаемость не измерялась |
| E003 | 2026-08-25 | `ed01732` | Тот же архив под `docker run --network none` в целевом образе | образ `1324d7bb…c8a3`, amd64, endpoint не вызывался | — | — | старт в пределах секунды | старт зелёный; найден риск экстрактора |
| E004 | 2026-08-25 | `fd640a8` | Первый прогон настоящей петли (`agent.loop`, не заглушки) в контейнере: endpoint отсутствует, и endpoint сконфигурирован но недостижим | образ `1324d7bb…c8a3`, amd64; endpoint не настоящий — `127.0.0.1:1` под `--network none` | 0 | 0 | 0.6 с / 12.7 с | петля не решала задачу; проверялась только деградация без endpoint и без сети |
| E005 | 2026-08-26 | `eb598fd` | Первый прогон петли до конца при **отвечающем** endpoint: полный путь, деградация лестницы, зависший endpoint, попытка выхода из workspace | WSL2 Linux 6.6, Python 3.10.12; **модель — скриптованная заглушка**, не LLM | 1/1 сценария A (заглушка, не решаемость) | 500 / 508 / 0 / 500 | 0.25 с / 0.23 с / 332.8 с / 0.27 с | механика зелёная; `run_shell` не ограничен workspace; `finish` без verify даёт `solved=True` на неверном файле |
| E006 | 2026-08-26 | `d1f7570` | Те же четыре кейса **внутри целевого образа**, `--network none`, Python 3.12.13 | образ `1324d7bb…c8a3`, amd64; **модель — та же скриптованная заглушка**, не LLM | 1/1 сценария A (заглушка, не решаемость) | 500 / 508 / 0 / 500 | 0.90 с / 0.66 с / 331.1 с / 0.65 с | все находки E005 воспроизвелись на Python 3.12 — это свойства кода, а не WSL |
| E007 | 2026-08-26 | см. карточку | Изоляция среды между повторами: 3× кейс A + 2× кейс D, каждый повтор — свежий контейнер и свежий host-каталог монтирования | образ `1324d7bb…c8a3`, amd64; та же заглушка | 5/5 повторов без взаимного влияния | 500 на повтор (заглушка) | 0.63–0.96 с на повтор | пять разных container id, все подтверждённо удалены; `escape_shell.txt` не переживает свой повтор — изоляция доказана, а не предположена |
| E008 | 2026-08-26 | см. карточку | Независимая проверка 18 task packages из M5 (Manus): 12 новых задач + 6 анти-overfit мутаций, reference-решение против verifier'а в изолированном контейнере | `python:3.12-slim`, WSL2 `ozon`, endpoint не вызывался | — | — | секунды на пакет | 18/18 `reward=1`, совпало с заявленным Manus; агент на фикстурах не запускался |
| E009 | 2026-08-26 | см. карточку | Диалект tool call llama.cpp на боевом пути: тот же кейс A внутри целевого образа против заглушки в режиме `flat`, с фиксом `_read_tool_call` и без него | образ `1324d7bb…c8a3`, amd64, `--network none`; **модель — заглушка**, отвечающая в форме llama.cpp | 1/1 в обоих прогонах (заглушка, не решаемость) | 500 с фиксом / 378 без | 1.41 с / 1.20 с | без фикса клиент молча уходит с native tools на `json_schema` после первого запроса и остаётся там; с фиксом все четыре запроса остаются `shape=tools` |
| E010 | 2026-08-26 | см. карточку | **Первый прогон против настоящего сервера инференса, а не заглушки**: форма tool call, лестница `probe_endpoint.py`, полный `run.sh` на фикстуре | `llama-server` 0.3.0-dev build 10621 `c1d0e7a00`, Qwen2.5-0.5B-Instruct Q4_K_M, CPU, `--temp 0`; WSL2, не целевой образ | 0/1 (модель 0.5B, решаемость не измерялась) | 5157 на прогон | 15 с | llama.cpp отдаёт **канонический** OpenAI-формат, а не документированный плоский; лестница 4/4 PASS; петля не упала и остановилась сама |
| E011 | 2026-08-26 | `2b5fd26` | **Первый прогон против настоящей модели**: 18 локальных пакетов, контракт Harbor воспроизведён (cwd — каталог агента, три переменные, таймаут задачи навязан снаружи и агенту не сообщён) | `qwen2.5-coder:7b` Q4_K_M на CPU, ollama в контейнере; образы задач `python:3.12-slim` | **0/18** | 47 440 (по строкам, где агент успел отчитаться) | 2 558 с на сьют | решаемость впервые измерена: ноль. 11 задач убиты по таймауту, 6 — агент отчитался solved=True при reward=0, 1 — stuck |
| E012 | 2026-08-26 | `617b774` | Один фактор к E011: политика путей (workspace по тексту задачи + разрешение записи по названным путям). Плюс счётчик токенов на endpoint и посмертный сбор артефактов | тот же `qwen2.5-coder:7b` на CPU, те же образы задач | **3/18** | 148 021 (счётчик endpoint, все строки) | 1 881 с на сьют | +3 задачи, 0 регрессий; класс отказов сместился с `timeout` (11→3) на `reasoning` (1→8) |
| E013 | 2026-08-26 | `5aa0cea` (откачен `ffa51be`) | Один фактор к E012: детектор застревания переводит петлю в производство результата вместо остановки | тот же `qwen2.5-coder:7b` на CPU | 3/18 (**без изменений**) | 182 651 (**+23%**) | 2 546 с (**+35%**) | **отрицательный результат, изменение откачено**: 0 приобретено, 0 потеряно, дороже по обеим вторичным метрикам |

## Per-task результаты

| Experiment | Task/capability | Repeat | Result 0/1 | Input | Output | Total tokens | Seconds | Calls/steps | Failure mode |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| — | — | — | — | — | — | — | — | — | — |

Допустимые failure modes: `startup`, `packaging`, `endpoint`, `tool`, `reasoning`,
`verification`, `timeout`, `unknown`.

### E001 — контракт упаковки

- Date/time (MSK): 2026-08-25
- Commit / dirty state: коммит `pack: build and verify the submission archive`, worktree чистый
- Upstream SHA: не импортирован
- Image tag + digest: **не проверялось** — на машине нет Docker, см. блокер в `HANDOFF.md`
- Model and sampling params: endpoint не вызывался
- Hypothesis: собранный архив стартует из чистого каталога и доносит текст задачи целиком
- Command:

  ```bash
  python scripts/build_submission.py
  python scripts/check_submission.py
  python -m pytest -q
  ```

- Environment/budgets: `AGENT_TIMEOUT_S=30`; endpoint указан на `127.0.0.1:9` (discard port),
  наследуются только `PATH` и минимум системных переменных
- Raw log path + SHA-256: логи не сохранялись — вывод детерминирован и воспроизводится командой
- Submission zip SHA-256 + size: `d6b634e1155d78fd8527be8a033db1aa362ed68cb7691fb4266bf6741ff06c75`,
  13 646 байт
- Result: `pytest` 68 passed; preflight 5 PASS, 0 FAIL, 0 SKIP
- Per-task failures: задачи не запускались
- Найденный дефект: `core.autocrlf=true` без `.gitattributes` превращал бы `run.sh` в CRLF
  при следующем checkout, что на Linux даёт `/bin/sh^M: bad interpreter` и ноль по всем
  задачам. Закрыто с двух сторон: `.gitattributes` и проверка CRLF в собранном архиве
- Conclusion: шлюз A закрыт частично. Следующий эксперимент — тот же архив в
  `secureintelligent/acp` с выключенной сетью
- Limitations: **offline-работа в целевом образе не доказана.** Проверено только на
  Windows/Git Bash с Python 3.11, цель — Linux/Python 3.12. Агент пока не решает задачи:
  `agent/main.py` печатает статус и выходит, `Outcome.verified` ещё не существует

### E002 — старт в файловой системе целевого образа

- Date/time (MSK): 2026-08-25
- Commit / dirty state: `70033bf`, worktree чистый на момент отправки архива
- Upstream SHA: не импортирован
- Image tag + digest: `secureintelligent/acp`, OCI index `sha256:1324d7bb…c8a3`,
  amd64 manifest `sha256:5a93249d3dfd…6132`, config `sha256:301b78fe9962…7647`;
  семь слоёв и распакованный rootfs 1 944 379 708 Б сверены с
  `docs/research/target-image-manifest.json`
- Model and sampling params: endpoint не вызывался, переменные фиктивные
- Hypothesis: собранный архив стартует в целевой ФС без сети и доносит произвольный
  текст задачи побайтово
- Command: выполнено внешним исполнителем (запрос M12), воспроизведение у нас —
  `docker run --rm --network none` по digest, см. ступень 4 в `docs/local-verification.md`
- Environment/budgets: `chroot` в восстановленную ФС, `unshare --net`, `uid=0`,
  PATH из image config; три набора переменных: не заданы / `http://127.0.0.1:1/v1` /
  синтаксически неверные
- Raw log path + SHA-256: `docs/research/submission-execution-check.md`,
  `f07f98646ea7004b640fb24dcd2c7556b79e10c6d8d234136e684273a4453fb6`
- Submission zip SHA-256 + size: в отчёте не зафиксирован — вместо этого совпадение
  подтверждено поведением: статус-строка локального прогона `build/submission.zip`
  совпала с отчётной во всех полях, кроме `workspace`
- Result: 15 непустых инструкций — exit `0` за 367–416 мс; пустая строка и
  отсутствующий аргумент — exit `2` и `usage` в stderr; ни traceback, ни зависаний.
  Layout `archive-root` контракт принимает, `one-top-level` — нет
- Per-task failures: задачи не запускались, цикла нет
- Conclusion and next experiment: G1 закрыт, G6 сузился до контейнера. Следующий
  эксперимент — тот же архив под `docker run --network none` с проверкой `/proc`,
  `ps` и фактических лимитов
- Limitations: `chroot` + `unshare` — **не контейнер**: нет Harbor wrapper, cgroups,
  seccomp и смонтированных `/proc`, `/sys`. `nproc=6`, 25 604 468 736 Б памяти и
  длительности принадлежат хосту исполнителя, а не оценочной microVM. «Стартует» не
  означает «решает»: stdout ограничен статусной строкой

### E003 — offline-старт в контейнере целевого образа

- Date/time (MSK): 2026-08-25
- Commit / dirty state: `ed01732`, worktree чистый; архив пересобран на этом же HEAD
  и совпал байт в байт с прогнанным
- Upstream SHA: не импортирован
- Image tag + digest: `secureintelligent/acp`, OCI index
  `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`, скачан
  `docker pull` по digest (content-addressed), `Architecture=amd64/linux`, 7 слоёв,
  размер content store 633 608 824 Б против 633 598 038 Б `full_size` из Hub API —
  разница ≈10,8 КБ приходится на index, manifest и config
- Model and sampling params: endpoint не вызывался; переменные не заданы, агент
  честно печатает `endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL`
- Hypothesis: собранный архив стартует в **контейнере** целевого образа без сети,
  на том режиме, который оставил распаковщик, и доносит произвольный текст побайтово
- Command: `docker run --rm --network none -v .../submission.zip:/opt/submission.zip:ro
  --entrypoint /bin/sh <image@digest> /opt/in_container.sh`; сценарий и артефакты —
  `docs/research/g6-container-run.md`
- Environment/budgets: Docker 29.6.1 (Server, overlayfs) внутри WSL2 `ozon`;
  `--network none`, `uid=0`, PID 1 — сам сценарий, `/proc` смонтирован (65 записей),
  cgroups v2 читаются (`pids.max=7099`)
- Raw log path + SHA-256: `docs/research/g6-container-run.md`,
  `3d25901957b9790c43ed19ce82f990751f4cc5d58fbbd3c6f2398f19104d6ebb`
- Submission zip SHA-256 + size: `bf1ee9b995a908c3e3fd416d84227b5ce4174abb2a01197ae2ff56635dedb87f`,
  32 823 Б
- Result:
  - **Контракт исполнимости выполнен.** `unzip` оставляет `run.sh` как `-rwxr-xr-x`;
    `./run.sh '<текст>'` из чистого каталога, **без единого `chmod`**, даёт exit `0`.
    Это первое исполнение contract-прогона в настоящем контейнере: на Windows он
    честно давал SKIP.
  - **Текст задачи доходит побайтово.** Шесть инструкций — обычная, с кавычками, с
    переводом строки, с ведущим `--`, с `$ ` / backtick / `|` / `;` / `>` и UTF-8 —
    дали fingerprint, посчитанный независимо на Windows, до последнего символа.
  - **Аргументный контракт держится**: нет аргумента / пустая строка / пробелы —
    exit `2` и `usage` в stderr.
  - **Компоновка.** `archive-root` работает; `one-top-level` даёт exit `127`
    (`run.sh` нет в корне) — независимое подтверждение вывода E002.
  - **Среда**: Debian 12 bookworm, Python **3.12.13**, `python3` разрешается в
    `/app/.venv/bin/python3`; `/app` в голом контейнере содержит только `.venv`;
    сети нет (только `lo`, DNS не резолвит).
  - **Запуск из чужого каталога** по абсолютному пути — exit `0`.
- Per-task failures: задачи не запускались, цикла нет
- Conclusion and next experiment: **G6 закрыт.** Найден риск, который упаковкой не
  лечится, — см. Limitations. Следующий эксперимент принадлежит уже не упаковке, а
  петле: первый публичный кейс целиком через `run.sh` в этом контейнере
- Limitations:
  - **Риск экстрактора — главный результат прогона и он открыт.** Тот же архив,
    распакованный `python3 -m zipfile -e`, даёт `run.sh` как `-rw-r--r--`, и
    `./run.sh` падает с exit `126` (`Permission denied`); `sh ./run.sh` при этом
    отрабатывает на `0`. `zipfile` не восстанавливает права **ни на одной**
    платформе. То есть режим в архиве правильный, но если runner распаковывает
    питоном и зовёт `./run.sh`, мы получаем 0/15, и **со стороны архива это не
    чинится**. Требуется подтверждение способа распаковки у организаторов.
  - `nproc=8` и `memory.max=max` принадлежат хосту WSL2, а не оценочной microVM:
    настоящие лимиты по-прежнему не измерены.

### E004 — первый прогон настоящей петли в контейнере целевого образа

- Date/time (MSK): 2026-08-25, ночь AFK
- Commit / dirty state: `fd640a8`, worktree чистый. Прогонялось дважды на двух
  коммитах: сначала `91316ac` (архив `bdbf5eb9…f819`, 49 204 Б), затем, после того как
  владелец `agent/loop.py` починил дефект из Limitations прежней версии этой записи
  (`b4636c0`, разбор в `fd640a8`), пересобрано и перепрогнано на `fd640a8`
  (`ad69d86f…4e7b`, 49 609 Б) — ниже приведены финальные числа по `fd640a8`
- Upstream SHA: не импортирован
- Image tag + digest: `secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`,
  подтверждён `docker images --no-trunc` перед прогоном
- Model and sampling params: настоящий endpoint не вызывался ни разу. Два прогона:
  (a) переменные не заданы; (b) `LOCAL_AGENT_MODEL=small-local`,
  `OPENAI_BASE_URL=http://127.0.0.1:1/v1` (недостижимый порт), `OPENAI_API_KEY`
  фиктивный, `AGENT_TIMEOUT_S=90`
- Hypothesis: с этим коммитом в архиве едет не заглушка (`agent/main.py` печатал
  статус и выходил, см. E001 limitations), а настоящая `agent.loop.run`; она должна
  безопасно деградировать без сети и без зависаний в обоих случаях
- Command:

  ```bash
  python3 scripts/build_submission.py
  python3 scripts/check_submission.py build/submission.zip
  docker run --rm --network none \
    -v .../submission.zip:/tmp/submission.zip:ro \
    secureintelligent/acp@sha256:1324d7bb…c8a3 \
    bash -c 'unzip -q /tmp/submission.zip && ./run.sh "<текст>"'
  # повторено с -e AGENT_TIMEOUT_S=90 -e LOCAL_AGENT_MODEL=... -e OPENAI_BASE_URL=... -e OPENAI_API_KEY=...
  ```

- Environment/budgets: Docker 29.6.1 внутри WSL2 `ozon`; `--network none`; (a)
  `AGENT_TIMEOUT_S` не задан → дефолт `600s`/резерв `120s`/`30` шагов; (b)
  `AGENT_TIMEOUT_S=90s` → резерв `30s` (пол `VERIFY_RESERVE_MIN_S`)
- Raw log path + SHA-256: вывод команд приведён в этой записи целиком, отдельный файл
  не заводился — воспроизводится командой выше построчно
- Submission zip SHA-256 + size: `ad69d86fded33cd94a57b44f2b697d4836956d5db563bcf550730b5a5afd4e7b`,
  49 609 Б; `check_submission.py` — 5/5 PASS, включая cold-start контракт (доступен
  только на POSIX, на Windows ранее был SKIP)
- Result:
  - **(a) Без переменных**: `agent ready: workspace=/app (image workdir /app) ...
    endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL`, exit `0`, строки
    `agent done:` нет — код читает `missing` и выходит до создания `loop`/`client`,
    ни одного сетевого вызова. `0.6` с.
  - **(b) С переменными, но недостижимый endpoint**: `agent ready: ... budget=90s
    reserve=30s ... endpoint=configured`, затем `agent done: solved=False
    stop=llm-unusable stage=none deliverable=none steps=3/30 recovery=0 tokens=0`,
    exit `0`. Ровно `MAX_CONSECUTIVE_LLM_FAILURES=3` неудачных попытки, затем честная
    остановка — `12.7` с, не зависла и не досидела до `AGENT_TIMEOUT_S`. Этот сценарий
    не создаёт артефакт и не проходит через `_finalise`, поэтому он не упражняет
    дефект, найденный и закрытый ниже — числа (a)/(b) идентичны на обоих коммитах.
  - Побочно подтверждено: при `AGENT_TIMEOUT_S=20` (< `VERIFY_RESERVE_MIN_S=30`) весь
    бюджет уходит в резерв, `spendable_s()=0` с самого начала, `may_act()` сразу
    `False` — петля не делает ни одного шага (`steps=0/30`) и не пытается звонить в
    endpoint. Безопасное вырождение, а не баг, но короткий `AGENT_TIMEOUT_S` в тестах
    надо выбирать `> VERIFY_RESERVE_MIN_S`, иначе тест ничего не проверяет.
- Per-task failures: задачи не давались, только offline-деградация без/с
  недостижимым endpoint
- Conclusion and next experiment: **следующий гейт после G6 — B0.1, реальный публичный
  прогон против настоящего `LOCAL_AGENT_MODEL`/`OPENAI_BASE_URL`.** Этой ночью он не
  делался: нет гарантированного доступа к реальному endpoint без присмотра человека,
  и жечь бюджет об недостижимый endpoint хуже, чем не пытаться (см. `PLAN.md`,
  AFK-очередь). Контракт петли в контейнере теперь доказан с обеих сторон
  (нет-конфига и есть-конфига-но-недостижим); чего он не доказывает — ниже
- Limitations: **решаемость задач не измерена.** Ни одна из шести public tasks не
  запускалась через настоящий цикл; `StubLlm`-тесты (`tests/test_loop.py`,
  15 сценариев по записанной ленте) проверяют логику петли, но не поведение против
  реального ответа модели. Пороги детекторов (`ACTION_HISTORY`, `EXACT_REPEAT_STOP`,
  `ALTERNATING_STOP`, `NO_PROGRESS_STOP`, `REPEAT_FAILURE_BLOCK`) и
  `OBSERVATION_WINDOW=8` — стартовые значения, не калибровка. `chroot`-риск
  экстрактора из E003 не переоценивался. Найденный при разборе этого прогона дефект
  (`_finalise` на `steps`/`budget`/`stuck` звал `verify(..., command="")`, что давало
  `ok=True`/`stage=unverified` — то есть `Outcome.solved=True` — на любом непустом
  артефакте, даже когда модель ни разу не пыталась `finish`) закрыт в `b4636c0` тем же
  вечером: `solved=True` теперь возможен только через явный зелёный `finish`,
  `_finalise` безусловно даёт `solved=False`, а `verify()` по-прежнему зовётся и
  вердикт попадает в трассу — для разбора нуля, не как доказательство. Направление
  ошибки выбрано осознанно: недооценивать собственный `solved` безопаснее, чем
  показывать непроверенный успех (Harbor всё равно не читает `Outcome.solved`, только
  среду, так что на реальный score это не влияло — влияло бы на наш собственный
  B0.1/H0.2). Мутационное тестирование той же ночью нашло соседний дефект в защите
  `verify-failed` от повторной перезаписи вердикта — тоже закрыт, тест добавлен.
  - Нет Harbor wrapper: контейнер запущен с `--entrypoint /bin/sh`, а не так, как
    его запускает оценивающая обвязка.
  - `workspace=/app` — намеренное поведение `resolve_workspace`, но в голом
    контейнере `/app` пуст (только `.venv`); на реальной задаче туда что-то
    монтируется, и это не проверено.
  - «Стартует» не означает «решает»: цикла ещё нет, stdout ограничен статусной
    строкой.


### E005 — первый прогон петли до конца против отвечающего endpoint

- Date/time (MSK): 2026-08-26, ~12:50–12:58.
- Commit / dirty state: `eb598fd`; worktree dirty — в дереве лежали незакоммиченные
  правки другой сессии в `scripts/check_submission.py`, `tests/test_submission.py`,
  `HANDOFF.md`. **Ни один из этих файлов не участвует в прогоне**: архив собран из
  `run.sh`, `THIRD_PARTY_NOTICES.md` и `agent/`, а они не менялись. Тем не менее по
  правилу дерева результат не является контрольным baseline.
- Upstream SHA: не применимо, публичный комплект не задействован.
- Image tag + digest: **целевой образ не использовался.** Docker на машине нет.
  Прогон шёл в WSL2 (`Linux 6.6.87.2-microsoft-standard-WSL2`, Python 3.10.12), а
  цель — `secureintelligent/acp` с Python 3.12. Это Linux, но не тот Linux.
- Model and sampling params: **модели не было.** `scripts/live_run/stub_endpoint.py`
  отдаёт заранее записанные действия через настоящий HTTP-сокет. Токены в таблице —
  цифры, которые заглушка объявила в `usage`, а не расход настоящей модели.
- Hypothesis: механика петли доходит до `verified` при отвечающем endpoint, а
  границы бюджета вызова, введённые в `5565aaa`, держатся в реальном процессе, а не
  только на симулированных часах.
- Command: `scripts/live_run/README.md`, четыре вызова `run_case.sh`.
- Environment/budgets: `AGENT_TIMEOUT_S` 120 с (A, C, D) и 600 с (B); окружение
  очищено через `env -i`, задача передана `LOCAL_AGENT_WORKDIR`, `cwd` равен каталогу
  агента — как это делает Harbor.
- Raw log path + SHA-256: логи заглушки лежат в каталоге прогона
  (`/root/airi/case*.stub.log`) вне репозитория; полные тексты приведены ниже, они
  короткие и не содержат ни секретов, ни текста задачи.
- Submission zip SHA-256 + size:
  `ef7b983a6db3e95381453e4db234e43d6e04b5f251d6c2a3398e1528fe2c4b43`, 53429 байт.

**A — отвечающий endpoint, полный путь.** `solved=True stop=verified stage=command`,
4 шага, 0.25 с. Агент разрешил workspace через `LOCAL_AGENT_WORKDIR`, прочитал
каталог и файл, переписал `calc.py`, объявил `finish` с командой `python3 check.py`;
верификатор запустил её по-настоящему и получил exit 0. Файл после прогона
действительно исправлен. Инструкция длиной 143 байта с двойными кавычками,
одинарными кавычками и переводом строки дошла до агента целой (fingerprint
`18fb4f9ba7b9`).

**C — endpoint отвергает `tools` кодом 400.** Один отказ, залипающая деградация в
`json_schema`, дальше четыре вызова в новой форме, `solved=True stop=verified
stage=command`. Цена разведки — ровно один потраченный запрос, как и задумано в
`agent/llm.py`.

**B — endpoint принимает соединение и никогда не отвечает, бюджет 600 с.**
Интервалы между приходами запросов на стороне заглушки:

```
request #1 в  1.02 с
request #2 в 91.12 с   -> первый логический вызов длился 90.1 с
request #3 в 211.13 с  -> второй длился 120.0 с
конец в 332.8 с        -> третий длился 120.0 с, затем stop=llm-unusable
```

Это прямое подтверждение фикса `5565aaa` в живом процессе: до раннего чекпоинта
вызов ограничен `early_deadline_s` (90 с), после — `LLM_TIMEOUT_S` (120 с). До фикса
тот же вызов на симулированных часах занимал 364 с. Побочно подтвердилась и
подсказка про ранний дедлайн: число сообщений в запросах шло 1 → 3 → 5, то есть на
шаге 2 (t=90 с, на диске пусто) к наблюдениям добавились и ошибка, и подсказка
«запиши лучший вариант сейчас».

Агент завершился сам на 332.8 с из 600 с, `exit=0`, `solved=False
stop=llm-unusable`, ноль токенов. На диске ничего не появилось — при мёртвом
endpoint появиться и нечему.

**D — попытка выйти за пределы workspace.**

| Действие | Результат |
|---|---|
| `write_file` в `../escape_write.txt` | отказ, файл не создан |
| `run_shell` с `> /root/airi/escape_shell.txt` | **выполнен, файл создан вне workspace** |

- Result: механика петли работает от `run.sh` до зелёного верификатора; границы
  бюджета вызова держатся в реальном процессе; лестница форматов деградирует
  правильно; `write_file` ограничен workspace, `run_shell` — нет.
- Per-task failures: не применимо, публичных задач в прогоне не было.
- Conclusion and next experiment: два наблюдения требуют решения, а не правки
  по вкусу.
  1. `run_shell` не ограничен workspace. Это теперь измеренный факт, а не TBD из
     `verdict-injection-defense.md`. Ограничивать его нельзя вслепую: задачи вроде
     forensics могут законно читать пути вне каталога задачи. Нужен прогон 15
     фикстур инъекций против настоящего endpoint, чтобы понять цену ограничения.
  2. `finish` с пустым `verify` даёт `stage=unverified` и `solved=True` на файле,
     который в кейсе D остался неверным. Поведение задумано в M13 (настоящий
     верификатор читает среду, а не наш self-check), но следствие в том, что **наш
     флаг `solved` завышает результат**. Никакое число solved из наших прогонов
     нельзя сравнивать с наградой Harbor, пока `stage` не указан рядом.
- Limitations: заглушка не модель — прогон не измеряет решаемость и не заменяет
  baseline; WSL2 с Python 3.10 не является целевым образом с Python 3.12; задача
  синтетическая, из шести публичных не взята ни одна; дерево было dirty.

### E006 — те же четыре кейса внутри целевого образа

- Date/time (MSK): 2026-08-26, ~13:50–14:00.
- Commit / dirty state: `d1f7570`; в дереве незакоммиченные правки промптов (этот
  файл и `docs/prompts/manus-prompts.md`), `agent/` не менялся.
- Image tag + digest: `secureintelligent/acp@sha256:1324d7bb1404…c8a3`, amd64,
  Debian 12, Python 3.12.13, WORKDIR `/app`, uid 0. Запуск
  `docker run --rm --network none -v <rundir>:/work`.
- Model and sampling params: **модели не было**, та же заглушка
  `scripts/live_run/stub_endpoint.py`, поднятая внутри контейнера на 127.0.0.1.
  При `--network none` доступен только loopback, поэтому офлайн-режим не нарушен.
- Hypothesis: находки E005 — свойства нашего кода, а не артефакт WSL с Python 3.10.

**Поправка к E005.** В карточке E005 записано «Docker на машине нет». Это неверно:
проверялся `docker` из Git Bash под Windows, тогда как демон стоит внутри WSL
(Docker 29.6.1), и целевой образ уже был скачан локально ровно тем digest'ом, что
записан в `decisions.md`. E005 следовало ставить сразу в контейнере.

| Кейс | В контейнере | В WSL (E005) |
|---|---|---|
| A — отвечающий endpoint | `solved=True stop=verified stage=command`, 0.90 с | 0.25 с |
| C — 400 на `tools` | одна деградация в `json_schema`, решено, 0.66 с | 0.23 с |
| B — зависший endpoint | 90.12 с → 120.14 с → стоп `llm-unusable` на 331.1 с | 90.1 → 120.0 → 332.8 с |
| D — выход из workspace | `write_file` отказ, `run_shell` выполнен | то же |

`run.sh` извлечён из архива с режимом 755 и запущен без предварительного `chmod`.

- Result: механика петли, границы бюджета вызова и лестница форматов ведут себя в
  целевом образе так же, как в WSL. Обе находки E005 воспроизвелись: `run_shell` не
  ограничен workspace, `finish` с пустым `verify` даёт `solved=True` на неверном
  файле.
- Conclusion and next experiment: разрыв `run_shell` перестал быть предметом чтения
  кода и стал измеренным фактом целевой среды — из этого поднят промпт M21. Отсутствие
  endpoint стало единственным блокером вне нашего кода — из этого поднят M22.
- Limitations: те же, что у E005, минус образ. Заглушка не модель, решаемость не
  измерена, публичные задачи не задействованы, дерево dirty.

### E007 — изоляция среды между повторами

- Date/time (MSK): 2026-08-26, ~14:05–14:15.
- Commit / dirty state: см. коммит этой карточки; `agent/` не менялся.
- Image tag + digest: `secureintelligent/acp@sha256:1324d7bb1404…c8a3`, amd64,
  та же, что E006.
- Model and sampling params: та же заглушка `scripts/live_run/stub_endpoint.py`,
  без сети.
- Hypothesis: повтор одного и того же кейса N раз не даёт одному повтору повлиять
  на другой — ни через переиспользованный контейнер, ни через переиспользованный
  host-каталог монтирования.
- Command: `sh scripts/live_run/run_repeats.sh script 8741 120 caseA 3` и
  `sh scripts/live_run/run_repeats.sh escape 8742 120 caseD 2`. Новый скрипт
  (`scripts/live_run/run_repeats.sh`) на каждый повтор: `mktemp -d` под host-mount,
  `docker run -d --network none` (без `--rm`, чтобы явно проверить и потом удалить
  самому), `docker wait`, `docker logs`, `docker rm`, затем `docker ps -a --filter
  id=...` подтверждает отсутствие контейнера, и `rm -rf` каталога монтирования.
- Environment/budgets: `AGENT_TIMEOUT_S=120` на все пять повторов.
- Raw log path + SHA-256: не сохранялся — вывод короткий, детерминированный при
  той же заглушке, приведён ниже целиком.
- Submission zip SHA-256 + size: пересобран скриптом на каждый вызов
  `run_repeats.sh`, не фиксировался отдельно — байт-идентичность архива уже
  доказана `test_build_is_byte_reproducible`.
- Result:

  Кейс A (полный путь), 3 повтора — container id
  `2ad6f07be26e…`, `57b9595424486…`, `4b9e2c908789…`, каждый `confirmed removed`
  после `docker rm`; `solved=True stop=verified` во всех трёх, wall 0.964/0.688/0.901 с.

  Кейс D (выход из workspace), 2 повтора — container id `2c2245245937…`,
  `a36129b22bd06…`, оба `confirmed removed`. В обоих повторах `run_shell` создаёт
  `escape_shell.txt` в своём собственном host-каталоге; проверка «carryover» в
  начале каждого повтора (до запуска контейнера) подтверждает отсутствие файла в
  свежем каталоге — сам факт, что второй повтор начинается с чистого листа, а не
  наследование первого.

- Per-task failures: не применимо, синтетический кейс.
- Conclusion and next experiment: закрывает пункт HANDOFF про изоляцию
  task-среды. Изоляция обеспечена структурно (свежий `docker run` плюс свежий
  `mktemp -d` на каждый повтор), а не полагается на `--rm` без проверки: скрипт
  сам подтверждает удаление, а не верит флагу. `--network none` дополнительно
  снимает вопрос про порты — у каждого контейнера свой сетевой namespace, поэтому
  два повтора не могут столкнуться на одном порту даже параллельно. Следующий шаг
  вне этой задачи — то же самое, но с настоящей моделью, когда её укажет M22.
- Limitations: заглушка не модель, репрезентативность решаемости не проверяется.
  Проверено последовательное выполнение повторов, не параллельное — параллельный
  случай (два повтора одновременно) структурно защищён тем же механизмом
  (отдельные `mktemp -d` и отдельные network namespace), но отдельно не измерялся.
  Хост-процессы вне контейнера (сам `docker run`) не профилировались — только
  состояние контейнера и его файловой системы после завершения.

### E008 — независимая проверка 18 task packages из M5 (Manus)

- Date/time (MSK): 2026-08-26, ~14:20–14:40.
- Commit / dirty state: см. коммит этой карточки.
- Upstream SHA: `71f90810d7a608677bbcf44f409f0aefded38340` (формат сверен с этим
  снапшотом внутри `local-task-fixtures-format-analysis.md`).
- Image tag + digest: не задействован; проверка запускалась в одноразовом
  `python:3.12-slim` (Docker Hub `sha256:7a8b475003c4…7a17`) внутри WSL2 `ozon`, не
  в целевом `secureintelligent/acp`.
- Model and sampling params: не задействованы — эндпойнт не вызывался, проверялось
  только reference-решение задачи против её verifier'а.
- Hypothesis: `reference-results.tsv`, приложенный Manus к поставке
  (18/18 `PASS reward=1`), воспроизводится независимо, не на словах Manus.
- Command:

  ```sh
  # копирование поставки на нативную ext4 внутри WSL2 (drvfs/NTFS не хранит x-bit)
  wsl -d ozon -- bash copy_to_wsl.sh
  # реальный прогон: python:3.12-slim, --network=none, sudo -n подменён на no-op shim
  wsl -d ozon -- bash run_verify.sh
  # источник скриптов: local_task_extra/tools/run_reference_suite.sh как есть
  ```

- Environment/budgets: не применимо — не измерялся ни один LLM-бюджет.
- Raw log path + SHA-256: не сохранялся, полный TSV-результат воспроизводим и
  приведён целиком в `docs/research/verdict-local-task-fixtures.md` §1; сама
  поставка проверена по SHA-256 (см. тот же файл §0).
- Submission zip SHA-256 + size: не применимо.
- Result: 18/18 `PASS reward=1`, идентично заявленному Manus. Файл лежит в
  `local_task_extra/validation/reference-results.tsv` — это наш прогон, не файл
  из поставки.
- Per-task failures: нет — все 18 прошли.
- Conclusion and next experiment: M5 принят как рабочий набор фикстур
  (`verdict-local-task-fixtures.md`). Следующий шаг — не про фикстуры, а про
  агента: прогнать `agent/main.py` на этих 18 задачах, когда появится реальная
  модель (блокер M22), и только тогда встаёт M19 (hold-out второго уровня).
- Limitations: проверена решаемость эталонным решением, не нашим агентом — это
  верхняя граница, не текущий результат. Decoy-элементы в четырёх задачах «с
  ловушкой» проверены только тем, что reference-решение их не путает; насколько
  они собьют малую модель, не измерено. При копировании поставки в WSL2 через
  инлайновую переменную в `wsl -d ozon -- bash -lc '...'` из Git Bash сломалось
  экранирование и несколько секунд выполнялся неверный `cp -r /.` — пойман и
  остановлен, детали в `verdict-local-task-fixtures.md` §2; исправлено переносом
  команд в файлы скриптов.

### E009 — диалект tool call llama.cpp на боевом пути

- Date/time (MSK): 2026-08-26, ~18:40.
- Commit / dirty state: см. коммит этой карточки; мутационный прогон делался на
  временно откаченном `_read_tool_call` и откат отменён сразу после.
- Image tag + digest: `secureintelligent/acp@sha256:1324d7bb1404…c8a3`, amd64,
  `--network none`, Python 3.12.13.
- Model and sampling params: заглушка `scripts/live_run/stub_endpoint.py` в новом
  режиме `flat`. Она отвечает в документированной форме llama.cpp —
  `finish_reason: "tool"` и `tool_calls[].{name, arguments}` без обёртки
  `function` (`docs/function-calling.md` на коммите `c1d0e7a`). **Это не модель.**
- Hypothesis: непринятая форма tool call не убивает прогон, а тихо сбрасывает
  клиент на нижнюю ступень лестницы — и это видно только на боевом пути, не в
  юнит-тесте.
- Command: `sh /work/run_in_container.sh flat 8741 120 caseFLAT` внутри образа,
  архив собран `scripts/build_submission.py`, распакован `unzip` в контейнере.
- Environment/budgets: `AGENT_TIMEOUT_S=120`, `LOCAL_AGENT_WORKDIR=/work/task`.
- Raw log path + SHA-256: не сохранялся, вывод короткий и приведён ниже.
- Result:

  С фиксом: `agent done: solved=True stop=verified steps=4/30 tokens=500`,
  `exit=0`, `post-run check: PASS`, wall 1.406 с. Все четыре запроса в логе
  заглушки — `shape=tools`.

  Без фикса (та же заглушка, откаченный `_read_tool_call`):
  `solved=True stop=verified steps=3/30 tokens=378`, `exit=0`,
  `post-run check: PASS`, wall 1.197 с. Лог заглушки: запрос №1 `shape=tools`,
  запросы №2–4 `shape=json_schema`.

- Per-task failures: не применимо, синтетический кейс.
- Conclusion and next experiment: дефект стоил одного запроса впустую и липкого
  спуска с native tools на весь остаток задачи, а не провала прогона. Ранняя
  оценка «каждый шаг тратил бы четыре запроса и всё равно падал» была артефактом
  юнит-теста, где заглушка отдавала плоскую форму на всех ступенях; исправлено в
  `verdict-local-model-baseline.md` §2. Следующий шаг выполнен и дал
  неожиданный ответ: настоящий `llama-server` того же коммита отдаёт **не**
  плоскую форму — см. E010.
- Limitations: заглушка не модель и не сервер: она воспроизводит форму ответа из
  документации, а не поведение llama.cpp. Токены (500 против 378) сравнивать
  напрямую нельзя — из-за потерянного запроса прогоны прошли разное число шагов
  по одной и той же таблице действий. Решаемость не измерялась.

### E010 — первый контакт с настоящим сервером инференса

- Date/time (MSK): 2026-08-26, ~19:20–19:35.
- Commit / dirty state: см. коммит этой карточки; `agent/` не менялся ради прогона.
- Image tag + digest: **не целевой образ.** WSL2 `ozon`, Linux 6.6.87.2, Python
  3.10.12. Целевой образ здесь неприменим: собранный `llama-server` живёт вне его.
- Model and sampling params: Qwen2.5-0.5B-Instruct Q4_K_M
  (`491 400 032` Б, размер сверен с ожидаемым перед запуском), сервер
  `llama-server` версии `0.3.0-dev (build 10621, commit c1d0e7a00)` — тот самый
  коммит, который пинует отчёт M22. Флаги: `--jinja --ctx-size 4096
  --n-gpu-layers 0 --temp 0`. **Модель взята не за качество, а за размер:** 0.5B
  влезает в 5 GiB, доступные WSL2. Рекомендованная 7B не влезает (`TEAMMATE.md` §1).
- Hypothesis: три отдельных вопроса. (1) Какую форму `tool_calls` отдаёт
  llama.cpp на самом деле. (2) Проходит ли наша лестница протоколов против
  настоящего сервера. (3) Переживает ли петля настоящую модель, а не заглушку.
- Command:

  ```sh
  sh /root/verify2.sh     # форма ответа, два режима: --jinja и без
  sh /root/probe_live.sh  # scripts/probe_endpoint.py против живого сервера
  sh /root/real_run.sh    # собранный submission, ./run.sh, фикстура calc.py
  ```

- Environment/budgets: `AGENT_TIMEOUT_S=420`, `LOCAL_AGENT_WORKDIR=/root/airi/task`,
  окружение очищено через `env -i`. Архив собран `scripts/build_submission.py`
  (`f6984524e353…`, 54 618 Б) и распакован `unzip`, то есть проверялся
  поставляемый артефакт, а не рабочее дерево.
- Raw log path + SHA-256: не сохранялся; весь вывод короткий и приведён ниже.
- Result:

  **(1) Форма tool call — против ожидания.** Документация коммита обещает
  `finish_reason: "tool"` и плоские `tool_calls[].{name, arguments}`. Настоящий
  сервер того же коммита отдаёт канонический OpenAI-формат:

  ```
  finish_reason: tool_calls
  entry keys: ['function', 'id', 'type']
  FLAT    (name at top level): False
  WRAPPED (function is dict) : True
  arguments type: str
  ```

  Одинаково в обоих режимах — с `--jinja` и без него. То есть расходится не наш
  разбор с сервером, а документация llama.cpp с её собственным сервером.

  **(2) Лестница протоколов — 4/4 PASS.**

  ```
  PASS  native_tools  ok, 172+20 tokens
  PASS  json_schema   ok, 38+14 tokens
  PASS  json_object   ok, 78+6 tokens
  PASS  text          ok, 78+6 tokens
  ```

  Первый раз, когда `probe_endpoint.py` отработал против чего-то, кроме заглушки.

  **(3) Полный прогон.** `agent done: solved=False stop=stuck stage=none
  deliverable=none steps=5/30 recovery=0 tokens=5157`, `exit=0`, wall 15 с,
  `post-run check: FAIL`. Трасса шагов (снята разовым диагностическим скриптом
  вне репозитория, `agent/` не трогался):

  ```
  [1] tool     run_shell   exit=127
  [2] protocol llm         tool call arrived in content, not tool_calls
  [2] degrade  llm         not json: Expecting value -> call style json_schema
  [2] tool     run_shell   exit=127
  [3] llm      bad-action  run_shell needs `command`, and it was missing or empty
  [4] budget   repeat-failure  ... already failed 2 times (exit:127)
  [5] budget   repeat-failure  ... already failed 2 times (exit:127)
  ```

- Per-task failures: одна задача, failure mode `reasoning` — модель 0.5B не
  выдала ни одного корректного действия. Это ожидаемо и не является измерением
  нашей петли.
- Conclusion and next experiment: три вывода. Во-первых, документированная плоская
  форма не подтверждается исполнением, поэтому фикс `0ef2b6f` перестаёт быть
  «фиксом под llama.cpp» и остаётся защитной нормализацией под неизвестный
  endpoint — формулировка исправлена в `verdict-local-model-baseline.md` §2 и §6.
  Во-вторых, лестница живёт против настоящего сервера. В-третьих, защиты петли
  сработали ровно как задумано на настоящем мусорном выводе: вызов в `content`
  распознан, спуск на `json_schema` выполнен, неполные аргументы отбиты,
  повторяющаяся неудача остановлена — агент вышел сам за 15 с из 420, а не
  досидел до таймаута. Следующий эксперимент — тот же набор на 7B, где
  решаемость впервые станет осмысленной величиной (`TEAMMATE.md` §1).
- Limitations: **это не baseline и не мера решаемости.** 0.5B взята по объёму
  памяти; одна синтетическая задача; прогон в WSL2, а не в целевом образе; один
  повтор при `temperature=0`. Из прогона переносится только то, что относится к
  обвязке: форма ответа сервера, работоспособность четырёх ступеней и поведение
  детекторов. Ничего о качестве скрытой модели оценки отсюда не следует.
### E011 — первый baseline решаемости на настоящей модели

- Date/time (MSK): 2026-08-26, 17:12–17:55
- Commit / dirty state: `2b5fd26`, worktree чистый; в `agent/` состояние идентично `09bf54b`
  (коммит добавлял только `scripts/eval/`). Архив заморожен как `build/submission-baseline.zip`
- Image tag + digest: задачи — `python:3.12-slim` (`sha256:7a8b475003c4…`), собранные из
  `environment/Dockerfile` каждого пакета; целевой `secureintelligent/acp` в этом прогоне
  **не участвует** — пакеты объявляют свой базовый образ сами
- Model and sampling params: `qwen2.5-coder:latest` (7.6B, Q4_K_M, context 32768),
  `ollama/ollama` в контейнере, хранилище моделей смонтировано только на чтение;
  `temperature: 0`, `max_output_tokens` 2048. CPU-only, 8 ядер, GPU нет.
  Прогретая скорость генерации — ~6 токенов/с
- Hypothesis: агент решает какую-то часть задач; распределение нулей по классам покажет,
  какой шлюз чинить первым
- Command:

  ```sh
  sh scripts/eval/start_model.sh
  python3 scripts/build_submission.py
  python3 scripts/eval/run_suite.py --model qwen2.5-coder:latest \
      --base-url http://sber-model:11434/v1 --api-key ollama \
      --label E011-baseline --out build/eval-E011
  ```

- Environment/budgets: сеть `sber-eval` создана `--internal` — из контейнера задачи
  достижим только контейнер модели, интернета нет (проверено прямым запросом).
  Экспортируются ровно `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`;
  `AGENT_TIMEOUT_S` **не передаётся**, как и у Harbor, поэтому агент живёт на
  `DEFAULT_TASK_TIMEOUT_S=600` и чекпоинте `EARLY_DELIVERABLE_DEADLINE_S=90`, а реальный
  предел (95 или 180 с из `task.toml`) навязывает внешний `timeout`. Лимиты контейнера
  задачи — `cpus` и `memory_mb` из пакета
- Raw log path + SHA-256: `build/eval-E011/` (`results.tsv`, `suite.json`, `logs/`) —
  не коммитится, воспроизводится командой выше
- Submission zip SHA-256 + size: `8fe23ab91adc31cce86b24a02ba78b17293911919479344ee8a81a4abd27ffe3`,
  53 160 байт
- Result: **solved 0/18**, 47 440 токенов (сумма по строкам, где агент успел напечатать
  итог), 2 558 с суммарного wall time

| Класс | Задач | Что это значит |
|---|---:|---|
| `timeout` | 11 | прогон убит внешним таймаутом раньше, чем появился артефакт |
| `verification` | 6 | агент отчитался `solved=True`, верификатор задачи дал `reward=0` |
| `reasoning` | 1 | детектор `stuck`: повтор одного и того же действия |

- Per-task failures: `timeout` — audit-hardcoded-token, audit-parser-trust-boundary,
  ctf-duplicate-parameter, fix-cache-key-scope, fix-zip-slip-extract,
  forensics-sharded-exfil, forensics-ssh-bruteforce, forensics-webshell-chain,
  mut-audit-query-gate, mut-forensics-egress-ledger, mut-salutation-beacon;
  `verification` — audit-path-normalization, ctf-signed-cookie, fix-redirect-scheme,
  mut-fix-inventory-filter, mut-fix-session-lookup, mut-greeting-farewell;
  `reasoning` — ctf-header-override
- Найденные дефекты (каждый закрыт отдельным коммитом с воспроизводящим тестом):
  1. `7bab094` — убитый прогон терял **весь** stdout: pipe буферизуется блоками, SIGTERM
     буфер не сбрасывает. Одиннадцать строк таблицы остались без workspace, шагов и токенов;
  2. `12a4b75` — `mut-greeting-farewell` показал цепочку целиком: `WORKDIR` образа не `/app`,
     workspace выродился в каталог самого агента, запрет записи вне workspace отправил
     артефакт туда же, self-verify его там нашёл, отчёт — `solved=True` при `reward=0`;
  3. `617b774` — правки к (2), найденные независимым критиком, включая дыру, которую
     открыла первая версия самой правки
- Conclusion: шлюз C впервые измерен и **не закрыт**. Оба класса нулей имеют разную
  природу и разную цену: `timeout` на этой машине — свойство железа (7B на CPU даёт
  10–25 с на шаг, значит в 95 с помещается 4–6 шагов), `verification` — свойство агента
  и переносится на любой endpoint
- Limitations: **ни одно число отсюда нельзя переносить на закрытый набор.**
  Модель оценки неизвестна и почти наверняка не эта; 18 фикстур сгенерированы нами
  (M5) и строже публичных в формулировках (`audit-path-normalization` требует точной
  строки `export_store.py:open_export`, которой в инструкции нет); один повтор на задачу,
  хотя `temperature: 0` делает прогон почти детерминированным; целевой образ
  `secureintelligent/acp` в этом прогоне не участвовал


### E012 — политика путей: первые решённые задачи

- Date/time (MSK): 2026-08-26, 17:56–18:27
- Commit / dirty state: `617b774`, worktree чистый
- Отличие от E011 — **один фактор**: разрешение workspace и политика записи
  (`agent/paths.py`, `12a4b75` + `617b774`). Модель, образы, масштаб таймаута, порядок
  задач и способ запуска не менялись
- Model and sampling params: те же, что в E011
- Command:

  ```sh
  sh scripts/eval/start_proxy.sh
  python3 scripts/build_submission.py
  python3 scripts/eval/run_suite.py --model qwen2.5-coder:latest \
      --base-url http://sber-proxy:8000/v1 --api-key ollama \
      --counter-container sber-proxy --label E012-path-policy --out build/eval-E012
  ```

- Submission zip SHA-256 + size: `62b4bcaa00bd513caa259663e124bff7666d4422509f609760d70b4261cb1885`,
  60 644 байта
- Result: **solved 3/18** (`mut-audit-query-gate`, `mut-greeting-farewell`,
  `mut-salutation-beacon`), потеряно 0, 1 881 с

| Класс | E011 | E012 |
|---|---:|---:|
| решено | 0 | **3** |
| `timeout` | 11 | 3 |
| `reasoning` | 1 | 8 |
| `verification` | 6 | 4 |

- Что подтвердилось: гипотеза про механизм была верна. Три из четырёх пакетов, у которых
  `WORKDIR` образа не `/app`, в E011 попадали в `timeout` — агент исследовал собственное
  дерево исходников и тратил на это шаги. С верным workspace те же задачи решаются за
  71–86 с и 5–7 шагов. Четвёртый (`mut-forensics-egress-ledger`) сдвинулся из `timeout`
  в `verification`: дошёл до конца, но артефакт не тот
- **Токены E011 и E012 сравнивать нельзя.** В E011 одиннадцать строк не имели числа
  вообще (агент был убит до того, как успел его напечатать), и 47 440 — сумма по семи
  оставшимся. В E012 считает прокси на endpoint, поэтому 148 021 — это все восемнадцать
  задач. Первое честное измерение токенов на сьюте — это E012, а не рост в три раза
- Conclusion: следующий фактор виден в новой форме отказов. Восемь прогонов остановлены
  детектором `stuck` на 4–9 шаге, через 56–88 с, при бюджете 95–180 с и пустом диске —
  то есть петля убивала их раньше, чем срабатывал её же чекпоинт раннего артефакта
- Limitations: один повтор на задачу; трассы действий в этом прогоне ещё не собирались
  (сбор дописан после старта), поэтому *почему* модель ходила по кругу, здесь не видно —
  только то, что ходила


### E013 — застревание как сигнал «производи»: отклонено

- Date/time (MSK): 2026-08-26, 18:33–19:16
- Commit / dirty state: измерялся `5aa0cea`; после измерения откачен как `ffa51be`,
  `agent/` побайтово вернулся к состоянию E012 (`git diff 617b774 HEAD -- agent/` пуст)
- Отличие от E012 — **один фактор**: `detectors.stuck` и два подряд отклонённых действия
  больше не завершали прогон, пока на диске пусто, а один раз выдавали ту же инструкцию,
  что и чекпоинт раннего артефакта, плюс три шага отсрочки
- Hypothesis: восемь прогонов E012 останавливались детектором на 4–9 шаге при живом
  бюджете 60–120 с; если вместо остановки просить результат, часть из них должна дойти
  до артефакта
- Result: **гипотеза не подтвердилась**

| Метрика | E012 | E013 |
|---|---:|---:|
| решено | 3/18 | 3/18 |
| приобретено / потеряно | — | 0 / 0 |
| токены (счётчик endpoint) | 148 021 | 182 651 (+23%) |
| wall time | 1 881 с | 2 546 с (+35%) |
| `reasoning` | 8 | 1 |
| `timeout` | 3 | 11 |

- Что произошло на самом деле: прогоны, которые раньше останавливались по застреванию,
  теперь доживают до внешнего таймаута — и ничего не производят по дороге. Малая модель,
  которой сказано «запиши лучший вариант сейчас», не записывает вариант: она продолжает
  ходить по кругу, держа инструкцию в контексте. Каждый лишний шаг оплачивается дважды —
  токенами и временем, а это обе вторичные метрики ранжирования
- Что из этого следует, и это важнее самого отката: **детектор застревания не был
  причиной тех восьми нулей.** Он останавливал прогоны, которые уже провалились, и более
  долгая жизнь сделала провал только дороже. Тот же вывод переносится на чекпоинт раннего
  артефакта (`EARLY_DELIVERABLE_DEADLINE_S`): он тоже всего лишь инструкция, а модель
  этого класса не исполняет инструкцию «произведи», пока не поняла, что производить.
  Значит следующий фактор надо искать в том, почему модель не понимает задачу, а не в
  том, когда её просят закончить
- Conclusion: изменение откачено по правилу проекта — фактор, не прибавивший ни одной
  решённой задачи и стоящий токенов, не принимается, каким бы разумным ни было
  рассуждение за ним
- Limitations: один повтор на задачу; отрицательный результат измерен на 7B/CPU, где шаг
  стоит 10–25 с. На быстром endpoint лишние шаги дешевле по времени, но по токенам —
  ровно так же дороги, и именно токены ранжируются вторыми


## Запланированные абляции токен-экономии (M8)

Приняты формой из `docs/research/token-economy.md`, разбор —
`verdict-token-economy.md`. **Все пять блокированы** дефектом бюджета логического
вызова LLM (`docs/decisions.md`, 2026-08-26): пока один вызов может стоить 364 с,
измеряется таймаут, а не политика.

Общий baseline `B` фиксирует: модель, tokenizer/шаблон, `temperature=0`, порядок
задач, сброс контейнера, дедлайн, verifier, `MAX_STEPS` и протокол вызова
инструментов. Каждая абляция меняет **ровно один** фактор политики.

Правило приёмки одно на все пять: (a) не потеряна ни одна задача, решённая `B`;
(b) solved count не ниже `B`; (c) сумма logical input + output ниже `B`; (d) нет
роста parser errors, неверных аргументов, провалов verifier и повторной разведки.
На 15 задачах это инженерный gate, не статистика — p-value здесь не считается.

| ID | Меняемый фактор | B → treatment | Метрики | Блокирован |
|---|---|---|---|---|
| A001 | политика отображения вывода инструмента | `raw full` → текущий `head 4k + tail 2k` → typed preview + raw artifact и повторное чтение | solved; каждый красный verifier; была ли нужная строка в видимом контексте; первое расхождение следующего действия; повторные чтения; input tokens | дефект бюджета вызова |
| A002 | `dedup_exact_records: false → true` | политика наблюдений — победитель A001; удаляются только байт-идентичные записи, первая сохраняется вместе с provenance и порядком | удалено дублей; сокращение токенов; изменились ли действия; verifier; аудит хэшей | A001 |
| A003 | формат ответа модели при неизменном `LLM_MAX_OUTPUT_TOKENS` | текущий ответ → action-first со схемой, без пересказа наблюдений | completion tokens; валидность разбора; корректность аргументов; циклы и повторы; solved | A002 |
| A004 | только `LLM_MAX_OUTPUT_TOKENS` | победитель A003 (или `B`) с `2048` → меньший заранее зафиксированный cap | completion tokens; `finish_reason=length`; обрезанные аргументы; verifier; solved. Строится кривая success-vs-cap | A003 |
| A005 | политика истории | полная история → один structured summary выше порога; последняя пара `action+observation` всегда verbatim, токены суммаризатора входят в итог | solved; вызовы и токены суммаризатора; первое действие после сжатия; заблокированные и повторные действия; peak и total input | A004 |

Скользящее окно — **шестая** абляция против прошедшего A005, не вместе с ним.
У нас уже есть `OBSERVATION_WINDOW = 8`, поэтому сравнивать надо окно против summary
при одинаковом видимом контексте. Сжатие статического control prompt (включая
английский перевод скелета) — только после неё.

Не входят и не планируются без отдельного доказательства: learned token pruning,
нестандартная нотация вывода, минификация исходников, многопроходный LLM-суммаризатор.

## Карточка эксперимента

Для значимого прогона добавить под таблицами:

```text
### E### — короткое имя

- Date/time (MSK):
- Commit / dirty state:
- Upstream SHA:
- Image tag + digest:
- Model and sampling params:
- Hypothesis (one changed factor):
- Command:
- Environment/budgets:
- Raw log path + SHA-256:
- Submission zip SHA-256 + size, if applicable:
- Result:
- Per-task failures:
- Conclusion and next experiment:
- Limitations:
```

Если worktree был dirty, перечислить изменённые файлы; такой результат не считается
контрольным baseline, пока не повторён на чистом commit.


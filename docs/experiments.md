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
  - Нет Harbor wrapper: контейнер запущен с `--entrypoint /bin/sh`, а не так, как
    его запускает оценивающая обвязка.
  - `workspace=/app` — намеренное поведение `resolve_workspace`, но в голом
    контейнере `/app` пуст (только `.venv`); на реальной задаче туда что-то
    монтируется, и это не проверено.
  - «Стартует» не означает «решает»: цикла ещё нет, stdout ограничен статусной
    строкой.


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


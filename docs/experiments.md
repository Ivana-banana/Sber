# Журнал экспериментов

Ни одно число из этого файла нельзя переносить в README/отчёт без commit SHA,
команды и контекста модели. Public задачи не являются оценкой закрытого score.

## Сводка

| ID | Дата | Commit | Изменение | Model/image | Solved | Tokens | Wall time | Verdict |
|---|---|---|---|---|---:|---:|---:|---|
| E000 | 2026-08-24 | — | Старт; baseline ещё не запускался | — | — | — | — | ожидает этап 0 |
| E001 | 2026-08-25 | см. карточку | Контракт упаковки: builder + preflight собранного архива | endpoint не вызывался | — | — | — | контракт зелёный, решаемость не измерялась |

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


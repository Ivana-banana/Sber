# Журнал экспериментов

Ни одно число из этого файла нельзя переносить в README/отчёт без commit SHA,
команды и контекста модели. Public задачи не являются оценкой закрытого score.

## Сводка

| ID | Дата | Commit | Изменение | Model/image | Solved | Tokens | Wall time | Verdict |
|---|---|---|---|---|---:|---:|---:|---|
| E000 | 2026-08-24 | — | Старт; baseline ещё не запускался | — | — | — | — | ожидает этап 0 |

## Per-task результаты

| Experiment | Task/capability | Repeat | Result 0/1 | Input | Output | Total tokens | Seconds | Calls/steps | Failure mode |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| — | — | — | — | — | — | — | — | — | — |

Допустимые failure modes: `startup`, `packaging`, `endpoint`, `tool`, `reasoning`,
`verification`, `timeout`, `unknown`.

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


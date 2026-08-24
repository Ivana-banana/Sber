# Архитектурная гипотеза v0

Статус: **гипотеза до baseline**, а не утверждённая финальная архитектура.

## Цели

- обобщаться между code audit, code fix, forensic и CTF-задачами;
- работать с малой локальной LLM без интернета;
- завершаться до timeout и оставлять бюджет на внешнюю проверку;
- быть наблюдаемой и достаточно простой для быстрой диагностики нулей;
- помещаться с исходниками в архив ≤10 МБ без runtime install.

## Минимальный контур

```text
run.sh
  └─ runtime adapter: instruction, env, deadline
      └─ agent loop
          ├─ inspect: pwd/tree/rg/read/processes
          ├─ reason: capability hypothesis + next action
          ├─ act: shell/read/write/patch
          ├─ observe: exit code + bounded output
          ├─ verify: tests/oracle/artifact checks
          └─ finish or recover within budget
```

Один orchestrator владеет budget и состоянием. «Специалисты» на старте — короткие
playbooks внутри prompt/context, а не отдельные LLM-процессы. Это уменьшает число
вызовов и не теряет наблюдения между агентами.

## Предлагаемые модули

Имена уточняются после импорта template; границы важнее имён.

| Модуль | Ответственность | Не делает |
|---|---|---|
| runtime | читает env/args, ставит deadline, запускает loop | не решает задачу |
| llm client | совместимый вызов endpoint, usage, retry policy | не прячет бесконечные retry |
| tools | локальные операции с typed result/exit code | не получает интернет |
| loop | состояние, следующий шаг, budgets, stop/recovery | не знает имён public cases |
| context | ограничение stdout, summaries, dedup | не удаляет критические ошибки |
| verifier | запускает наблюдаемый oracle по гипотезе агента | не верит self-report LLM |
| telemetry | calls/tokens/time/failure summary с redaction | не пишет секреты/full context |

## Состояние цикла

Минимально храним:

- исходную инструкцию и проверенную рабочую директорию;
- deadline, token budget (если известен), calls/steps used;
- краткую карту файлов/сервисов;
- текущую гипотезу, последний action/result;
- созданные/изменённые файлы;
- verifier result и причину finish/failure.

Историю tool output не носим целиком: сохраняем релевантный fragment и ссылку/путь.

## Политика budget

- Жёсткий wall-clock deadline контролирует код, а не модель.
- Резерв времени на verifier и корректное завершение выделяется заранее.
- Tool result ограничивается по размеру с явной пометкой truncation.
- Повтор идентичного action блокируется или требует новой гипотезы.
- Retry endpoint ограничен числом и временем; постоянная ошибка завершается
  диагностируемо, а не зависанием.
- Конкретные числа выбираются только после измерения target runtime и известных
  ограничений. До этого они `TBD`.

## Capability playbooks

Playbook — подсказка порядка исследования, не router с жёсткой классификацией.

- **Code audit:** структура → sources/config/deps → data flows/sinks → минимальное
  доказательство → требуемый артефакт.
- **SWE fix:** воспроизвести → локализовать → минимальный patch → targeted test →
  regression; тесты не ослаблять.
- **Forensics:** сохранить исходные артефакты → timeline/IOCs → корреляция →
  проверить формат ответа.
- **CTF/local service:** инвентаризация → поверхность → дешёвые гипотезы →
  локальное взаимодействие → проверить flag/artifact.

## Что намеренно не строим до эксперимента

- отдельную LLM на каждый класс;
- vector DB/RAG по публичным решениям;
- динамические плагины и собственный agent framework;
- обучение модели в runtime;
- сложную долговременную память;
- хардкод путей, названий задач и reference answers.

## Архитектурные вопросы для baseline

1. Как example agent сообщает Harbor о завершении и usage?
2. Насколько надёжно целевая модель поддерживает native tool calls?
3. Какие CLI реально есть и запускаются без дополнительных пакетов?
4. Можно ли получить remaining time/tokens из окружения или нужен собственный cap?
5. Какой verifier доступен внутри задач, а что остаётся скрытым?
6. Какой объём stdout/tool result начинает заметно ухудшать модель?

Ответы превращаются в решения только через записи `docs/decisions.md`.


# Стратегический аудит решения — 24.08.2026

Статус: завершённый статический аудит и выбор направления; runtime-выводы требуют
повторной проверки на машине с Docker и локальным LLM endpoint.

## Короткий вердикт

Не следует ни слегка переписывать prompt example agent, ни сразу строить ансамбль
специалистов. Наиболее сильная ставка при текущих ограничениях — **гибридный каскад**:

```text
deterministic preflight + budget/deadline controller
                         ↓
          one universal small-LLM tool loop
                         ↓
     capability playbook selected as a soft hint
                         ↓
         external verification + one recovery pass
```

Причина: бинарная оценка сильнее всего наказывает инфраструктурные и
верификационные ошибки. Детерминированный слой бесплатно закрывает часть этих нулей,
а единый LLM-loop сохраняет переносимость на неизвестные классы закрытых задач.

## Что и как проверено

- ТЗ из `Тз сбер.docx` сопоставлено с публичным репозиторием организаторов.
- Полная история upstream изучена до commit
  [`71f90810d7a608677bbcf44f409f0aefded38340`](https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/commit/71f90810d7a608677bbcf44f409f0aefded38340)
  от 09.07.2026.
- Статически разобраны `agent.py`, `run.sh`, `local_agent.py`, task configs,
  инструкции, Dockerfiles, public verifiers и reference solutions.
- Структура `sample_submission.zip` проверена как архив, а не по README.
- Подход сопоставлен с первичными материалами по
  [SWE-agent ACI](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md),
  [Harbor installed agents](https://www.harborframework.com/docs/agents) и
  [CTFTiny/CTF agent tuning](https://ojs.aaai.org/index.php/AAAI/article/view/40210).

Не выполнено: Harbor/Docker/LLM baseline. На этой машине нет `docker` и `uv`,
локальный Python 3.13.6 не является целевой средой. WSL2 distro `ozon` установлен,
но не запускается: `HCS_E_HYPERV_NOT_INSTALLED` (виртуализация/Virtual Machine
Platform недоступна). Поэтому никакие значения solved/tokens/time в этом аудите не
заявляются; R0.1 должен выполнить владелец другой Docker-машины или этой машины
после исправления виртуализации.

## Фактическая поверхность public набора

В `local_task/` семь каталогов, но **оцениваемых task packages шесть**.
`insecure-api-app/` — общий fixture/generator для трёх SQLi-задач, у него нет
`task.toml`.

| Capability | Tasks | Что реально проверяет verifier | Главный риск агента |
|---|---|---|---|
| точное создание артефакта | `hello-file`, `bye-file` | файл с точным содержимым | потратить 120 секунд на ненужное рассуждение |
| security audit/report | `find-sqli-login` | JSON и наличие конкретного SQLi finding | написать хороший текст, но не машинный артефакт |
| security code repair | `fix-sqli-login`, `fix-sqli-search` | security tests + полная регрессия API | изменить не тот sink или сломать функциональность |
| forensic correlation | `incident-log-forensics` | четыре точных `key=value` | прочитать только начало 5,8 МБ JSONL и пропустить событие |

Public набор **не проверяет** полноценные CTF binary/reverse/crypto, memory/disk
forensics, многоязычные репозитории, большие dependency graphs и работу при реально
выключенном интернете. В task configs стоит `allow_internet = true`, хотя финальная
среда по регламенту offline. Поэтому даже `6/6` — только contract floor.

## Аудит example agent

### Критические риски

| ID | Наблюдение | Как превращается в 0 | Требуемая проверка/исправление |
|---|---|---|---|
| B1 | wrapper запускает `run.sh` из `/opt/harbor/local-agent`; явный task workdir не передаётся | относительные команды исследуют код агента, а не `/app` | runtime probe; определить `/app`/cwd, сообщить модели оба пути |
| B2 | `REQUEST_LIMIT = 1000`, но нет wall-clock controller | loop до внешнего timeout | внутренний deadline, max steps, per-tool timeout, graceful finalize |
| B3 | нет обязательной фазы verifier и резерва | модель объявляет успех без артефакта/теста | state machine с verify budget и одним recovery pass |
| B4 | `read_file` возвращает первые 16 000 символов; forensic fixture 5,8 МБ | нужное событие может лежать далеко после начала | `search`, line/range reader, tail/head/sample и JSONL filtering |
| B5 | shell subprocess не имеет timeout | зависший pytest/service съедает всю задачу | per-command timeout + kill process group + bounded capture |
| B6 | каждый tool result идёт и в LLM, и в подробный JSON log | лишний I/O/context, огромные логи | отдельные compact observation и redacted diagnostic summary |

### Существенные риски

| ID | Наблюдение | Последствие |
|---|---|---|
| B7 | redaction смотрит на имя поля, но stdout может содержать секрет | `cat .env` попадёт в `stdout` log |
| B8 | только `append_file`; точная перезапись делается через shell | малая модель чаще ошибается с quoting и newline |
| B9 | нет специализированных `list/search/read_range` ACI | дорогие shell-команды и шумный контекст |
| B10 | нет явного детектора повторяющегося action | циклы с одинаковой ошибкой |
| B11 | native tool calling зависит от поведения неизвестной локальной модели | несовместимый endpoint/model может не вызывать tools |
| B12 | Harbor рекомендует наполнять context по мере выполнения, example wrapper делает это только после run | при timeout/error диагностика может потеряться |
| B13 | README требует `run.sh` в submission, но sample zip содержит верхний каталог `sample_submission/` | упаковочный контракт неоднозначен; нужна фактическая Harbor-проверка обоих layouts |

### Что в baseline уже разумно

- `run.sh` использует POSIX `sh`, `set -eu` и вычисляет собственный каталог.
- Модель/endpoint/ключ приходят из окружения, а не хардкодятся.
- Есть tool-result exit code и явная строка для пустого stdout/stderr.
- Unified diff безопаснее полной LLM-перезаписи больших файлов.
- Организаторы уже исправляли stream-event API и SQLi fixtures; копировать старую
  версию из случайного архива особенно рискованно.

## Рассмотренные стратегии

Оценка 1–5 — инженерный prior, не leaderboard probability. Веса отражают порядок
официальной оценки и недельный срок.

| Критерий | Вес |
|---|---:|
| покрытие неизвестных задач | 30% |
| надёжность на малой LLM | 20% |
| реализуемость до КТ №4 | 15% |
| токены и время | 15% |
| диагностируемость | 10% |
| устойчивость к public overfit | 10% |

| Стратегия | Cover | Small LLM | KT4 | Efficiency | Diagnose | Generalize | Итог / 5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| S1. Example agent + prompt tuning | 2.0 | 2.0 | 5.0 | 3.0 | 2.0 | 2.0 | **2.60** |
| S2. Усиленный монолитный general agent | 4.0 | 3.0 | 4.0 | 3.0 | 4.0 | 4.0 | **3.65** |
| S3. Router + отдельные LLM-специалисты | 4.5 | 3.0 | 2.0 | 1.5 | 3.0 | 4.0 | **3.18** |
| S4. Детерминированные solver pipelines | 2.5 | 4.5 | 3.0 | 5.0 | 5.0 | 2.0 | **3.55** |
| S5. Hybrid cascade: guardrails + один loop + verifier | 4.5 | 4.0 | 3.5 | 4.0 | 5.0 | 4.5 | **4.23** |

### Почему остальные проиграли

- **S1** быстро даёт submission, но сохраняет B1–B13 и почти наверняка проигрывает
  команде, которая просто контролирует timeout и verification.
- **S2** жизнеспособен, но заставляет LLM тратить токены на детерминированные вещи:
  поиск workspace, контроль deadline, форматирование tool output и выбор verifier.
- **S3** выглядит эффектно, но каждый specialist повторно получает контекст, router
  может ошибиться, а shared budget размывается. Возвращаться к нему только после
  абляции на разнообразном наборе.
- **S4** очень силён на известных классах и слаб на закрытом хвосте. Это хорошая
  часть S5, но плохая единственная архитектура.

## Выбранная стратегия S5

### Слой 0 — deterministic preflight

Без LLM и с жёстким временем:

1. определить instruction, cwd, `/app`, writable roots и доступные CLI;
2. выделить из инструкции явные deliverables, exact-format constraints,
   запреты и предложенные test commands;
3. построить компактный tree/search summary, не читать большие файлы целиком;
4. завести monotonic deadline, max steps и verifier reserve;
5. передать LLM короткий environment brief.

### Слой 1 — один универсальный controller

Один разговор сохраняет причинную цепочку. На каждом шаге модель возвращает:

- текущую гипотезу;
- одно следующее действие;
- ожидаемый сигнал успеха/ошибки;
- нужен ли переход в verify.

Capability определяется мягко: `artifact`, `audit`, `repair`, `forensics`,
`ctf/general`. Ошибка классификации не должна запрещать инструменты другого класса.

### Слой 2 — ACI для малой модели

Минимальный набор:

- `list_tree(path, depth, limit)`;
- `search(pattern, paths, max_matches)`;
- `read_range(path, start, count)`;
- `run(command, timeout, output_limit)`;
- `write_file(path, content, exact)`;
- `apply_patch(path, diff)`;
- `file_info(path)`.

SWE-agent показывает, что компактный file viewer, краткий search output, lint после
edit и явное сообщение о пустом output существенно меняют качество ACI. Нельзя
отдавать малой модели бесконтрольный `cat` как основной интерфейс.

### Слой 3 — verifier/recovery

Verifier выбирается из instruction и среды:

1. exact file/schema check;
2. syntax/lint/JSON parse;
3. targeted test;
4. regression suite;
5. service health/probe;
6. artifact existence and final diff sanity.

Если verifier красный и остался budget — один recovery pass с краткой ошибкой.
Если budget исчерпан — корректно завершиться, не начинать новый широкий поиск.

## Что может опровергнуть выбор

S5 пересматривается, если эксперимент показывает одно из следующего:

- native tool loop нестабилен на целевой модели, а text-action fallback не помогает;
- deterministic preflight расходует заметное время и не предотвращает ни одного
  failure mode;
- specialist call прибавляет решённые задачи без регрессии tokens/time;
- target image уже содержит зрелый coding agent, который стабильно превосходит нашу
  реализацию на одинаковой модели и budget;
- точные token/time limits настолько малы, что нужен почти полностью
  детерминированный одношаговый solver.

## Оценка шансов на победу

Численную вероятность сейчас давать нельзя: неизвестны модель, budgets, закрытая
сложность, leaderboard КТ №3 и решения конкурентов. Любой процент был бы выдумкой.

Вместо этого используем уровни доказательности:

| Уровень | Доказательство | Что можно утверждать |
|---|---|---|
| R0 | есть только документы | шанс не измерим, submission отсутствует |
| R1 | clean archive запускается offline | не получим тотальный ноль из-за упаковки |
| R2 | `6/6 public × 3` на target model/image | закрыт public contract, не доказано обобщение |
| R3 | ≥80% на независимых мутациях, нет hangs/startup failures | архитектура жизнеспособна |
| R4 | стабильна на своей смешанной suite audit/fix/forensics/CTF | можно ожидать meaningful closed score |
| R5 | результат КТ в пределах одной задачи от лидера | победа становится реалистичной; оптимизируем слабые классы и tie-break |

Текущий уровень — **R0**. Example agent сам по себе оценивается как низкая ставка:
public cases просты, а системные failure modes не закрыты. S5 даёт хороший
инженерный путь к конкурентности, но обещать победу до R4/R5 нечестно.

## Источники и границы переноса

- [Официальный public repository](https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic)
  — контракт соревнования и examples.
- [Harbor agents](https://www.harborframework.com/docs/agents) — lifecycle и context.
- [SWE-agent ACI](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md)
  — эмпирические правила file/search/edit интерфейса.
- [AAAI 2026: effective offensive-security agents](https://ojs.aaai.org/index.php/AAAI/article/view/40210)
  — необходимость отдельно измерять trajectories и hyperparameters.

Эти источники подтверждают дизайн экспериментов, но не предсказывают закрытый score
AIRI. Решение принимается окончательно только по нашим прогонам и контрольным точкам.

# E020 Phase Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** получить пофазную разбивку wall-clock по всем 18 локальным задачам на текущем HEAD и на её основе зафиксировать ровно один кандидат-фактор для следующей абляции.

**Architecture:** три слоя, снизу вверх. `run_suite.py` уже пишет колонку `phases` в `results.tsv` — добавляется только обратный разбор этой строки. `summarise.py` агрегирует колонку в четыре строки, отвечающие на вопросы «сколько wall съедает генерация», «сколько стоит один вызов» и «в какой фазе задача умерла». Затем — живой прогон E020 как control на текущем HEAD и разбор его чисел. Живая модель не запускается, пока обе кодовые задачи не зелёные: прогон стоит ~46 минут и не должен тратиться на отладку парсера.

**Tech Stack:** Python 3.12, только стандартная библиотека (`statistics`, `re`, `dataclasses`); pytest; Docker + Ollama `qwen2.5-coder:7b-instruct-q4_K_M` через counting proxy на internal-сети `sber-eval`.

**Spec:** `docs/superpowers/plans/2026-09-03-next-session-prompt-e020.md`; измерения-предпосылки — карточки **E018** и **E019** в `docs/experiments.md`; baseline для сравнения — **E017** там же и `docs/research/live-runs/E017-summary.txt`.

## Global Constraints

- Целевая среда — Python 3.12 без интернета; в рантайме агента только стандартная библиотека. `scripts/` в архив не пакуется, но это не повод тянуть в него зависимости.
- Хардкод под публичные задачи запрещён: ни имена задач, ни их таймауты (`95.0`/`180.0`) не попадают в код агента ни как константа, ни как условие.
- Одна абляция меняет **ровно один** фактор. E020 — control, он не меняет ничего в `agent/`.
- Идентификаторы, docstring'и, сообщения об ошибках и комментарии в коде — английские; документация и карточки экспериментов — русские.
- Результат без команды воспроизведения и сохранённого сырого summary не считается.
- Коммит после каждого законченного логического шага, префиксы по `GIT.md` (`eval:`, `agent:`, `docs:`), в конце сообщения строка `Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt`.
- Не коммитить `.env`, ключи, `build/`, контейнерные логи и сырые дампы; в карточку идут пути и SHA-256, а не сами тяжёлые файлы.
- Перед коммитом и push: `git fetch --all --prune`, `git status -sb`, просмотр обеих сторон расхождения.

---

### Task 1: Обратный разбор колонки `phases`

`render()` уже сворачивает разбивку в одну ячейку TSV. Чтобы агрегировать её, нужен разбор обратно — иначе `summarise.py` пришлось бы читать сырые логи, которых у него нет.

**Files:**
- Modify: `scripts/eval/run_suite.py` (класс `PhaseBreakdown`, сразу после метода `render`)
- Test: `tests/test_eval_suite.py`

**Interfaces:**
- Consumes: `PhaseBreakdown` (поля `seconds: dict[str, float]`, `completed: dict[str, int]`, `open_phase: str`), `phase_breakdown(output: str) -> PhaseBreakdown`, `PhaseBreakdown.render() -> str` — всё уже существует в `scripts/eval/run_suite.py`.
- Produces: `PhaseBreakdown.parse(rendered: str) -> PhaseBreakdown` — classmethod, точная обратная функция к `render()`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/test_eval_suite.py`. Хелперы `phase()` и `killed_log()` уже определены в начале файла — не дублировать.

```python
def test_a_rendered_breakdown_parses_back_into_the_same_numbers() -> None:
    """The TSV cell is the only form a summary sees; it has to be lossless."""
    original = phase_breakdown(killed_log())

    assert PhaseBreakdown.parse(original.render()) == original


def test_parsing_a_run_without_telemetry_yields_an_empty_breakdown() -> None:
    assert PhaseBreakdown.parse("-") == PhaseBreakdown()
```

Дописать импорт `PhaseBreakdown` в существующий блок `from scripts.eval.run_suite import (...)` в начале файла.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python3 -m pytest tests/test_eval_suite.py -q`
Expected: `ImportError: cannot import name 'PhaseBreakdown'` либо `AttributeError: type object 'PhaseBreakdown' has no attribute 'parse'`.

- [ ] **Step 3: Реализовать `parse`**

В `scripts/eval/run_suite.py`, внутри `class PhaseBreakdown`, сразу после `render`:

```python
    @classmethod
    def parse(cls, rendered: str) -> PhaseBreakdown:
        """Read `render` back, so a summary can aggregate the table, not the logs.

        The placeholder `-` and any token this format does not own are skipped: a
        results table written by an older harness must stay readable.
        """
        breakdown = cls()
        for token in rendered.split():
            name, _, value = token.partition("=")
            if name == "open":
                breakdown.open_phase = value
            elif value:
                seconds, _, count = value.partition("/")
                breakdown.seconds[name] = float(seconds)
                breakdown.completed[name] = int(count)
        return breakdown
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `python3 -m pytest tests/test_eval_suite.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Прогнать весь набор**

Run: `python3 -m pytest -q`
Expected: `327 passed` (было 325).

- [ ] **Step 6: Коммит**

```bash
git add scripts/eval/run_suite.py tests/test_eval_suite.py
git commit -m "$(cat <<'EOF'
eval: read the phase breakdown back out of the results table

Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt
EOF
)"
```

---

### Task 2: Агрегат фаз в `summarise.py`

Четыре строки, отвечающие ровно на три вопроса из спеки: доля wall на генерацию, стоимость одного вызова, фаза смерти. Считаются из `results.tsv`, поэтому применимы и к уже сохранённым прогонам.

**Files:**
- Modify: `scripts/eval/summarise.py:13-17` (импорты), `scripts/eval/summarise.py:38-59` (функция `summarise`)
- Test: `tests/test_eval_suite.py`

**Interfaces:**
- Consumes: `PhaseBreakdown.parse` из Task 1; строки как `dict[str, str]`, полученные `summarise.read()` — ключи `task`, `wall_s`, `phases`, `reward`, `failure`.
- Produces: `phase_report(rows: list[dict[str, str]]) -> list[str]` — список готовых строк вывода; пустой список, если ни в одной строке нет телеметрии.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/test_eval_suite.py`:

```python
def row(task: str, wall_s: float, phases: str) -> dict[str, str]:
    return {"task": task, "wall_s": str(wall_s), "phases": phases, "reward": "0", "failure": "timeout"}


def test_the_phase_report_answers_share_cost_and_place_of_death() -> None:
    """Three numbers decide the next ablation; a reader must not compute them by hand."""
    rows = [
        row("t1", 95.1, "inspect=0.004/1 plan=89.990/3 act=0.017/2 open=plan@4"),
        row("t2", 180.0, "inspect=0.002/1 plan=90.000/9 act=0.010/6 verify=1.000/1"),
        row("t3", 100.0, "plan=80.000/4 act=0.020/3 open=act@5"),
        row("t4", 12.0, "-"),
    ]

    assert phase_report(rows) == [
        "phase plan  median 80.0% of wall, min 50.0% (t2), max 94.6% (t1)",
        "phase cost  16 plan calls, mean 16.2 s; dearest t1 at 30.0 s/call",
        "phase kill  act 1, none 1, plan 1; deepest step 5 (t3)",
        "phase gaps  1 row(s) without telemetry",
    ]


def test_a_run_killed_inside_its_first_plan_reports_no_completed_call() -> None:
    """The worst case measured in E018 must not divide by zero."""
    rows = [row("t1", 95.1, "inspect=0.004/1 open=plan@1")]

    assert phase_report(rows)[1] == "phase cost  no plan call completed anywhere"


def test_a_run_without_any_telemetry_adds_no_lines() -> None:
    """E017-era directories must keep summarising exactly as before."""
    assert phase_report([row("t1", 95.1, "-")]) == []
```

Дописать `phase_report` в импорт из `scripts.eval.summarise`; блока импорта из этого модуля в файле ещё нет, добавить рядом с существующим:

```python
from scripts.eval.summarise import phase_report
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python3 -m pytest tests/test_eval_suite.py -q`
Expected: `ImportError: cannot import name 'phase_report'`.

- [ ] **Step 3: Реализовать `phase_report`**

В `scripts/eval/summarise.py` заменить блок импортов на приведённый ниже. Вставка корня в `sys.path` не украшение: модуль запускается и как скрипт (`python3 scripts/eval/summarise.py …`, эта форма зафиксирована в транскриптах E011–E017), и как импорт из тестов. Без неё первая форма падает с `ModuleNotFoundError: No module named 'scripts'`. Идиома скопирована из `scripts/build_submission.py:32-36`.

```python
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval.run_suite import PhaseBreakdown  # noqa: E402
```

и добавить функцию перед `summarise`:

```python
def phase_report(rows: list[dict[str, str]]) -> list[str]:
    """Aggregate the per-task `phases` column into the three timeout questions.

    E018 answered them for one fixture by hand. Across eighteen tasks the same
    arithmetic is the difference between "the model is slow" and "the agent takes
    too many steps", and those two have opposite fixes.
    """
    parsed = [(item, PhaseBreakdown.parse(item.get("phases", "-"))) for item in rows]
    timed = [(item, phases) for item, phases in parsed if phases.seconds and float(item["wall_s"]) > 0]
    if not timed:
        return []

    shares = sorted(
        (phases.seconds.get("plan", 0.0) / float(item["wall_s"]) * 100, item["task"])
        for item, phases in timed
    )
    lines = [
        f"phase plan  median {statistics.median(share for share, _ in shares):.1f}% of wall, "
        f"min {shares[0][0]:.1f}% ({shares[0][1]}), max {shares[-1][0]:.1f}% ({shares[-1][1]})"
    ]

    calls = sum(phases.completed.get("plan", 0) for _, phases in timed)
    seconds = sum(phases.seconds.get("plan", 0.0) for _, phases in timed)
    if calls:
        dearest = max(
            (phases.seconds["plan"] / phases.completed["plan"], item["task"])
            for item, phases in timed
            if phases.completed.get("plan")
        )
        lines.append(
            f"phase cost  {calls} plan calls, mean {seconds / calls:.1f} s; "
            f"dearest {dearest[1]} at {dearest[0]:.1f} s/call"
        )
    else:
        lines.append("phase cost  no plan call completed anywhere")

    killed: dict[str, int] = {}
    for _, phases in timed:
        killed[phases.open_phase.partition("@")[0] or "none"] = (
            killed.get(phases.open_phase.partition("@")[0] or "none", 0) + 1
        )
    deepest = max(
        (
            (int(phases.open_phase.partition("@")[2]), item["task"])
            for item, phases in timed
            if phases.open_phase
        ),
        default=(0, "-"),
    )
    lines.append(
        "phase kill  "
        + ", ".join(f"{name} {count}" for name, count in sorted(killed.items()))
        + f"; deepest step {deepest[0]} ({deepest[1]})"
    )

    missing = len(rows) - len(timed)
    if missing:
        lines.append(f"phase gaps  {missing} row(s) without telemetry")
    return lines
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `python3 -m pytest tests/test_eval_suite.py -q`
Expected: `11 passed`.

- [ ] **Step 5: Вывести агрегат в отчёт**

В `scripts/eval/summarise.py`, в функции `summarise`, сразу после строки, печатающей `failures` (`print("failures    " + ...)`), добавить:

```python
    for line in phase_report(rows):
        print(line)
```

- [ ] **Step 6: Проверить на реальных сохранённых прогонах**

Run: `python3 scripts/eval/summarise.py build/eval-E017-accepted-control`
Expected: вывод как раньше, **без** строк `phase …` — в E017 телеметрии нет, и агрегат обязан молчать, а не падать. Отсутствие `ModuleNotFoundError` здесь и есть проверка вставки в `sys.path`: тесты её не поймают, потому что импортируют модуль иначе.

Run: `python3 -m pytest -q`
Expected: `330 passed`.

- [ ] **Step 7: Коммит**

```bash
git add scripts/eval/summarise.py tests/test_eval_suite.py
git commit -m "$(cat <<'EOF'
eval: aggregate the phase breakdown into the summary

Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt
EOF
)"
```

---

### Task 3: Живой прогон E020

Control на текущем HEAD. Единственное отличие от E017 — сам HEAD (телеметрия фаз и её синхронная запись в stdout). Никакие константы бюджета не трогаются.

**Files:**
- Create: `build/eval-E020-phase-control/` (не коммитится), `build/e020-phase-control.zip` (не коммитится)
- Create: `docs/research/live-runs/E020-summary.txt`

**Interfaces:**
- Consumes: `phase_report` из Task 2 — через `summarise.py`, отдельно вызывать не нужно.
- Produces: `build/eval-E020-phase-control/results.tsv` с непустой колонкой `phases` во всех 18 строках; `docs/research/live-runs/E020-summary.txt` в формате `E017-summary.txt`.

- [ ] **Step 1: Синхронизация и чистое дерево**

```bash
git fetch --all --prune
git status -sb
git log --oneline HEAD..origin/eval/live-baseline
```
Expected: расхождений нет, tracked-файлы чисты. Untracked черновики планов допустимы — builder их не пакует.

- [ ] **Step 2: Зелёные тесты и архив**

```bash
python3 -m pytest -q
python3 scripts/build_submission.py --out build/e020-phase-control.zip
python3 scripts/check_submission.py build/e020-phase-control.zip
```
Expected: `330 passed`; архив собран, размер и SHA-256 напечатаны; `5 PASS, 0 blocking problems`. Записать размер и SHA-256 — они пойдут в карточку.

- [ ] **Step 3: Поднять окружение**

```bash
sh scripts/eval/start_model.sh
sh scripts/eval/start_proxy.sh
docker inspect --format '{{index .RepoDigests 0}}' ollama/ollama
docker exec sber-ollama ollama list
```
Expected: контейнеры `sber-ollama` и `sber-proxy` запущены; модель `qwen2.5-coder:7b-instruct-q4_K_M` присутствует. Записать manifest модели, digest'ы обоих образов и версию Ollama.

- [ ] **Step 4: Прогнать сьют**

```bash
python3 scripts/eval/run_suite.py --tasks-root local_task_extra/tasks \
  --model qwen2.5-coder:7b-instruct-q4_K_M --base-url http://sber-proxy:8000/v1 \
  --api-key ollama --network sber-eval --counter-container sber-proxy \
  --timeout-scale 1.0 --archive build/e020-phase-control.zip \
  --out build/eval-E020-phase-control --label E020-phase-control
```
Expected: 18 строк, ~45–55 минут. `--timeout-scale 1.0` и отсутствие `--tell-agent-budget` обязательны: это контракт Harbor, а не удобство.

- [ ] **Step 5: Гейт на полноту телеметрии**

```bash
awk -F'\t' 'NR==1{for(i=1;i<=NF;i++)if($i=="phases")c=i;next}{print $1"\t"$c}' \
  build/eval-E020-phase-control/results.tsv
grep -c "agent phase" build/eval-E020-phase-control/logs/*.log
```
Expected: во всех 18 строках колонка `phases` непуста и не равна `-`; в каждом логе число строк `agent phase` больше нуля.

**Если хотя бы одна строка пуста или `-`, а её лог при этом содержит `agent phase`** — это дефект harvester'а, а не результат прогона. Остановиться, написать воспроизводящий тест в `tests/test_eval_suite.py` на реальную форму строки из этого лога, починить `phase_breakdown`, перегенерировать TSV из сохранённых логов и только потом продолжать. Прогон при этом переделывать не нужно: логи уже на диске.

- [ ] **Step 6: Снять сводку**

```bash
python3 scripts/eval/summarise.py build/eval-E020-phase-control
python3 scripts/eval/summarise.py build/eval-E017-accepted-control build/eval-E020-phase-control
```
Expected: первый вызов печатает четыре строки `phase …`; второй — блок `per task` с `gained`/`lost` относительно E017.

- [ ] **Step 7: Записать сырое summary**

Создать `docs/research/live-runs/E020-summary.txt` по образцу `docs/research/live-runs/E017-summary.txt`: дата/время MSK, commit, состояние дерева, полный список команд из шагов 2–6 копипастой, размер и SHA-256 архива, результат preflight, model manifest / версия Ollama / temperature / digest'ы образов / имя сети / `timeout_scale` / отметка «AGENT_TIMEOUT_S not passed», отсортированный список 18 task image ID и digest этого списка, затем блок `Output` с solved, списком решённых задач, endpoint tokens/calls/late и `summed_agent_wall_s`, и полный вывод обоих вызовов `summarise.py`.

```bash
sha256sum docs/research/live-runs/E020-summary.txt build/eval-E020-phase-control/results.tsv
```
Записать обе суммы в сам файл (сумму самого файла — как «до добавления этой строки» либо пересчитать в конце, как это сделано в E018).

- [ ] **Step 8: Коммит**

```bash
git add docs/research/live-runs/E020-summary.txt
git commit -m "$(cat <<'EOF'
eval: record the E020 phase-control transcript

Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt
EOF
)"
```

---

### Task 4: Карточка E020

**Files:**
- Modify: `docs/experiments.md` (строка в таблицу `## Сводка` после строки E019; карточка перед `## Запланированные абляции токен-экономии (M8)`)
- Modify: `HANDOFF.md` (первая строка таблицы `## Завершено`)

**Interfaces:**
- Consumes: `docs/research/live-runs/E020-summary.txt` из Task 3 — все числа берутся оттуда, не пересчитываются на глаз.
- Produces: карточка `### E020 — control на текущем HEAD с пофазной разбивкой`, ссылки на которую использует Task 5.

- [ ] **Step 1: Строка в сводную таблицу**

Добавить после строки `| E019 | …` в `docs/experiments.md`, по формату соседних строк: `| E020 | 2026-09-0X | <SHA> | Control на текущем HEAD: та же конфигурация E017 плюс пофазная телеметрия | <модель/образы> | <solved>/18 | <endpoint tokens> | <wall> | <вердикт одной фразой> |`.

- [ ] **Step 2: Карточка**

Вставить перед `## Запланированные абляции токен-экономии (M8)` карточку по шаблону из конца `docs/experiments.md`, обязательно включив:

- Date/time (MSK), commit и состояние дерева;
- Hypothesis: это control, изменённый фактор относительно E017 — сам HEAD (телеметрия и её синхронный вывод), а не политика агента;
- Model/environment: manifest, Ollama, `temperature=0`, digest'ы образов, digest списка task image ID, `timeout_scale=1.0`, «AGENT_TIMEOUT_S not passed»;
- Command + пути и SHA-256 сохранённых артефактов;
- Result: solved, endpoint tokens, wall, и **четыре строки `phase …` дословно**;
- Ответы на три вопроса спеки числами: медианная доля wall на завершённые `plan`; средняя стоимость `plan`-вызова и её разброс между задачами; гистограмма фазы смерти и максимальный достигнутый шаг против `MAX_STEPS = 30`;
- Сравнение с E017 как с ориентиром: solved, tokens, wall и per-task `gained`/`lost`;
- Limitations: дельта wall относительно E017 включает неизмеренную стоимость самой телеметрии, поэтому причинность не заявляется; 7B на CPU и локальные fixtures не являются hidden evaluator; `timeout_sec` скрытого набора неизвестен.

- [ ] **Step 3: Строка в `HANDOFF.md`**

Добавить первой строкой таблицы `## Завершено`: дата, владелец, commit, результат (E020 как новый control с пофазной разбивкой, ключевые числа), колонка «Проверка / ограничения» — `pytest -q`, гейт полноты телеметрии, SHA архива и summary, перечисленные выше ограничения.

- [ ] **Step 4: Проверить согласованность**

```bash
grep -n "E020" docs/experiments.md HANDOFF.md
```
Expected: строка сводки, карточка и строка HANDOFF ссылаются на один и тот же commit и одни и те же числа solved/tokens/wall.

- [ ] **Step 5: Коммит**

```bash
git add docs/experiments.md HANDOFF.md
git commit -m "$(cat <<'EOF'
docs: record E020, the phase-control baseline

Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt
EOF
)"
```

---

### Task 5: Decision — один кандидат-фактор

Единственный результат этой задачи — записанное решение с критерием приёмки. Никакой код в `agent/` не меняется: реализация ждёт approval.

**Files:**
- Modify: `docs/decisions.md` (новая строка таблицы)
- Modify: `PLAN.md` (следующая абляция в очереди)

**Interfaces:**
- Consumes: карточка E020 из Task 4 — фактор выбирается по её числам, а не по ожиданиям.
- Produces: строка decision со статусом `предложено`, на которую сошлётся следующая сессия.

- [ ] **Step 1: Выбрать фактор по данным**

Правило выбора, а не готовый ответ:

- если медианная доля wall на `plan` ≥ 80% **и** максимальный достигнутый шаг заметно ниже `MAX_STEPS = 30` — узкое место latency, и кандидат должен сокращать **число логических вызовов до появления проверяемого артефакта**, а не поднимать лимиты;
- если максимальный шаг упирается в 30 хотя бы у трёх задач — кандидат касается `MAX_STEPS`, и тогда его цена измеряется в токенах;
- если `verify` начинался, но не успевал — кандидат касается резерва.

Ограничение, снимающее самый вероятный неверный ход: реальные `timeout_sec` локальных пакетов (`95.0` у четырёх задач, `180.0` у четырнадцати) **не являются** основанием менять `DEFAULT_TASK_TIMEOUT_S = 600.0` или `EARLY_DELIVERABLE_DEADLINE_S = 90.0` на подогнанное число — таймауты скрытого набора неизвестны, и подгонка под публичные значения нарушает `CLAUDE.md`. Кандидат обязан быть независимым от предполагаемой длительности: например, «проверяемый артефакт существует после первого успешного действия», а не «600 → 180».

- [ ] **Step 2: Записать decision**

Добавить строку в таблицу `docs/decisions.md`: `| 2026-09-0X | предложено | <фактор одной фразой> | <обоснование числами E020; критерий приёмки: ни одна задача, решённая E020, не потеряна, и solved count не снизился; контроль — E020; изменяется ровно один фактор> |`.

- [ ] **Step 3: Поставить абляцию в очередь**

В `PLAN.md` добавить следующую абляцию в существующий раздел очереди: имя, контроль (`E020`), изменяемый фактор, критерий приёмки, отметка «требует approval до реализации».

- [ ] **Step 4: Проверить, что решение не опережает данные**

```bash
grep -n "предложено" docs/decisions.md | tail -3
```
Expected: формулировка фактора не содержит числа, взятого из `timeout_sec` публичных пакетов, и ссылается на конкретные числа E020.

- [ ] **Step 5: Коммит и push**

```bash
git fetch --all --prune
git status -sb
git add docs/decisions.md PLAN.md
git commit -m "$(cat <<'EOF'
docs: queue the next single-factor ablation behind E020

Claude-Session: https://claude.ai/code/session_01C1Tvn93rF4RYs6XvDE51tt
EOF
)"
git push
```

---

## Что явно вне плана

Менять `MAX_STEPS`, `VERIFY_RESERVE_FRACTION`, `DEFAULT_TASK_TIMEOUT_S`, `EARLY_DELIVERABLE_DEADLINE_S` или любую другую константу в `agent/limits.py`; батчинг read-only действий; security treatment (нужен отдельный approval); тюнинг Ollama — не наш периметр конфигурации; повторные прогоны E017.

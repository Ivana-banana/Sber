# Перенос архитектурных паттернов из прошлых хакатонов

Статус: **предложение**, не принятое решение. Источники — `../AIOS` (Татнефть,
мультиагентная оптимизация + суррогат), `../ozon` (роботизированная сортировка),
`../ai-v-massy` (LLM-пайплайн доказательной медицины), `../datacon_2026`
(LLM-экстракция из PDF). Отобраны только те паттерны, которые выживают в наших
ограничениях: offline microVM, малая LLM, stdlib, бинарный зачёт, 15 закрытых задач.

## 1. Что откуда берём

| # | Паттерн | Источник | Что закрывает у нас |
|---|---|---|---|
| P1 | Единственный источник истины по метрике; обвязку пишем рядом, эталон не трогаем | AIOS `CLAUDE.md`, `vendor/CHDD_PYTHON` | `agent.py`/Harbor-обёртка организаторов вендорится побайтово; наша логика живёт рядом и вызывается из `run.sh` |
| P2 | Явные «два разрыва» между стендом и оценкой, с подписью у каждого числа | AIOS, ozon («целевая среда — не симулятор») | public 6 задач ↔ закрытые 15; наша машина ↔ целевой образ |
| P3 | Результат цикла — dataclass с полем `confirmed` и человекочитаемым `summary()` | AIOS `aios/loop.py::CycleResult` | запрет завершения по self-report малой LLM |
| P4 | Guardrail против оптимизма: `J_safe = Ĵ − λ·B − ρ·d²`, «мало калибровки → не калибровано» | AIOS `aios/trust.py` | правило «нет oracle → задача не решена, а помечена `no_oracle`» |
| P5 | Предполётная проверка **файла с диска**, а не структуры в памяти; отказы vs предупреждения, exit code | AIOS `scripts/check_submission.py` | `scripts/check_submission.py` для собранного `.zip` |
| P6 | Журнал экспериментов с командой воспроизведения прямо в документе | AIOS `docs/experiments.md` | формат «команда + сырой лог + checksum» |
| P7 | Несколько конфигов рядом (`config.toml`, `config-nobalance.toml`) для абляции одним фактором | AIOS | профили агента: `full`, `no-verify`, `short-prompt`, `no-preflight` |
| P8 | Отдельный `BLOCKED.md` для внешних блокеров | AIOS | сейчас живёт в `HANDOFF.md`, этого достаточно |
| P9 | Все доменные константы в одном модуле; дублирование числа по коду = баг | ozon `src/constants.py` | `agent/limits.py`: таймауты, max steps, лимиты вывода, резерв verifier |
| P10 | CI на каждый push: `ruff` + `pytest`; красный CI чинится немедленно | ozon `.github/workflows/ci.yml` | + contract test `run.sh` + smoke распакованного zip |
| P11 | Мерить на худших случаях (`hard_scene_metrics.py`), а не на каталоге | ozon | mutation suite поверх public fixtures |
| P12 | **Ports & adapters**: Protocol-границы, адаптеры внедряются через `Deps` | ai-v-massy `src/ports/__init__.py`, `src/pipeline/deps.py` | весь loop тестируется без сети и без токенов |
| P13 | **Провал схемы — это значение, а не исключение** (`SchemaError`) | ai-v-massy `src/ports/__init__.py` | битый tool-call малой модели уходит в петлю восстановления, а не роняет прогон |
| P14 | State с reducer'ами; узлы возвращают частичные апдейты; `audit: Annotated[list[Event], operator.add]` | ai-v-massy `src/pipeline/state.py` | append-only журнал шагов = телеметрия без второго механизма логирования |
| P15 | Ограниченная петля исправления со счётчиком в состоянии (`factcheck_retries ≤ 2`) | ai-v-massy `nodes.py`, `graph.py` | `recovery_passes` — лимит держит код, не модель |
| P16 | Ленивый **переиспользуемый** клиент; token-bucket | ai-v-massy `adapters/llm/aistudio.py`, `obs/ratelimit.py` | burst из сотен вызовов иначе даёт connect-timeout; у нас это ноль по timeout |
| P17 | **Frozen-адаптер**: замороженные ответы источника для детерминированного eval | ai-v-massy `adapters/search/frozen.py`, `eval/run_frozen.py` | записанные транскрипты модели → регрессии `run.sh` **не жгут токены** |
| P18 | Роль модели (`workhorse`/`reasoning`) отделена от URI; `LLM_MODEL` переопределяет все роли | ai-v-massy `ModelRole`, `ModelConfig` | одна модель из env сегодня, разведение ролей — потом, без переписывания |
| P19 | `sanitized_summary()`: в лог уходит `api_key_present: bool`, а не ключ | datacon `src/agents/base.py` | прямое требование `CLAUDE.md` о секретах, готовая реализация |
| P20 | **Цепочка деградации вызова**: `responses.create` → `chat.completions`; `json_schema` → `json_object` → текст | datacon `LLMClient.complete_text/complete_json` | закрывает риск «локальная модель плохо вызывает tools» из `PLAN.md` |
| P21 | Дешёвый детерминированный retrieval (BM25 + regex) до LLM | datacon `src/retrieve/` | preflight `grep`/`rg` даёт кандидатов, LLM только решает — экономия токенов (tie-break №2) |
| P22 | Мост к baseline остаётся вызываемым (`baseline_bridge.py`) | datacon | example agent организаторов не удаляем: он точка отсчёта в `docs/experiments.md` |

## 2. Что осознанно **не** переносим

| Паттерн | Почему нет |
|---|---|
| LangGraph / pydantic / `openai` SDK как основа | В целевом образе их наличие не подтверждено, установка на прогоне запрещена. Берём **идею** портов и state, реализуем на `dataclass` + `typing.Protocol` + `urllib.request`. SDK — опциональный адаптер, если probe его найдёт |
| `async`/`asyncio`-граф с fan-out (ai-v-massy) | У нас одна задача за прогон и одна модель; конкурентность добавляет отказы, а не решённые задачи |
| Иерархия «планировщик → исполнители» (AIOS) | Уже отклонено аудитом до появления данных; playbooks в prompt дешевле |
| Суррогатная модель / обучение в runtime (AIOS) | Нет обучающей выборки и нет времени внутри задачи |
| BM25-индекс по корпусу (datacon) | В образе есть `rg`/`grep`; свой поиск запрещён `CLAUDE.md` |
| Веб-UI и картинки (AIOS `aios/web`, ozon) | Оценивает автоматический runner, человек трассу не смотрит. Наблюдаемость — текстовый audit-лог |

## 3. Предлагаемая раскладка

```text
run.sh                      # entrypoint: exec python3 -m agent.main "$@"
agent/
  main.py                   # runtime adapter: argv/env → Budget → loop → exit code
  limits.py                 # P9: все числа проекта здесь и больше нигде
  ports.py                  # P12/P13: Protocol'ы + типизированные исходы
  llm.py                    # P16/P18/P19/P20: клиент, usage, деградация, redaction
  probe.py                  # P20: одноразовая проверка возможностей endpoint
  preflight.py              # P21: карта среды без LLM
  tools.py                  # list/read_range/search/run/write/patch, bounded output
  state.py                  # P14: AgentState + append-only audit
  loop.py                   # P3: связывает готовые части, сам ничего не решает
  verify.py                 # P4: внешний oracle, выбор по инструкции и среде
  playbooks.py              # текстовые подсказки: audit / fix / forensics / ctf
tests/
  fixtures/transcripts/     # P17: замороженные ответы модели
  test_contract.py          # P10: run.sh, argv, env
  test_budget.py            # deadline, резерв, stop conditions
  test_frozen_runs.py       # весь loop на StubLlm, без сети и токенов
scripts/
  build_submission.py       # сборка zip
  check_submission.py       # P5: проверка zip **с диска**
  run_public.py             # P22: прогон public набора, таблица в experiments.md
```

## 4. Ключевые контракты

Только stdlib. Имена уточняются после импорта upstream, важны границы.

```python
# agent/ports.py
@dataclass(frozen=True)
class LlmReply:
    text: str
    input_tokens: int
    output_tokens: int

@dataclass(frozen=True)
class LlmError:            # P13: исход, а не исключение
    reason: str
    retryable: bool

class LlmPort(Protocol):
    def complete(self, messages: list[Message], *,
                 max_output_tokens: int) -> LlmReply | LlmError: ...

@dataclass(frozen=True)
class ToolResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool        # усечение помечается явно, не молча
    duration_s: float
```

```python
# agent/state.py — P14
@dataclass
class Budget:
    deadline: float               # time.monotonic()
    verify_reserve_s: float
    max_steps: int
    steps_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def remaining_s(self) -> float: ...
    def may_act(self) -> bool:     # решает код, не модель
        return (self.steps_used < self.max_steps
                and self.remaining_s() > self.verify_reserve_s)

@dataclass
class AgentState:
    instruction: str
    workspace: Path
    budget: Budget
    hypothesis: str = ""
    artifacts: list[Path] = field(default_factory=list)
    audit: list[Event] = field(default_factory=list)   # append-only
    recovery_passes: int = 0                            # P15
```

```python
# agent/loop.py — P3, прямой аналог AIOS CycleResult
@dataclass(frozen=True)
class Outcome:
    answer: str
    verified: bool                 # ← CycleResult.confirmed
    verifier: str                  # какой именно oracle подтвердил
    evidence: str                  # exit code / хвост вывода
    stop_reason: str               # verified | budget | no_oracle | error
    def summary(self) -> str: ...  # в лог и в failure taxonomy
```

Правило P4 в нашей формулировке: **`verified=False` — это не «наверное решено».**
Если oracle недоступен, это пишется как `no_oracle`, а не сглаживается в успех.
Так `trust.py` поступает с некалиброванным конформным запасом: подставить сюда
правдоподобное число значит заявить защиту, которой нет.

## 5. Цепочка деградации вызова модели (P20)

Лестница от богатого запроса к простому:

1. native tool calls (`tools` + `tool_choice`);
2. `response_format=json_schema` (strict);
3. `response_format=json_object` + схема в системном сообщении;
4. свободный текст, из которого JSON выковыривается жёстким парсером.

Каждый уровень имеет свой парсер и общий `LlmError` на выходе. Понижение уровня —
событие в `audit`, а не тихая смена поведения. Это снимает риск из `PLAN.md`
«локальная модель плохо вызывает tools» до того, как он станет пятнадцатью нулями.

**Правка от 25.08.2026: runtime-probe отменён.** В первой редакции здесь стоял probe
на старте каждого прогона. Это ошибка: оцениваемая модель одна и та же на всех 15
задачах, поэтому probe пятнадцать раз платит токенами за один и тот же ответ, а
токены — второй критерий ранжирования. Принято вместо этого:

- стартовый уровень — константа `limits.DEFAULT_CALL_STYLE`, выставленная по
  измерению, а не по догадке;
- измерение делает dev-скрипт `scripts/probe_endpoint.py`, который гоняется один раз
  по целевой модели и пишет строку в `docs/experiments.md`;
- в runtime остаётся только **ленивая липкая** деградация: первый отказ понижает
  уровень до конца прогона. В нормальном случае она стоит ноль.

Там же второе следствие того же правила: непарсящийся ответ **не переспрашивается**.
Сэмплирование идёт при `temperature=0`, запрос был бы байт-идентичным, значит повтор
покупает тот же ответ и платит за него токенами. Понижение уровня — да, повтор — нет.

## 6. Frozen-регрессии (P17) — почему это важно именно нам

Ранжирование: solved → **суммарные токены** → время. Локальные прогоны в токены
организаторов не попадают, но жгут наш бюджет времени и вносят стохастику.
Записанный транскрипт (`tests/fixtures/transcripts/*.json`) отдаётся через
`StubLlm`, реализующий тот же `LlmPort`. Тогда:

- весь loop, budget, парсеры, tools и verifier проверяются детерминированно;
- CI гоняется без сети и без ключа;
- live-прогон нужен только там, где меряем саму модель.

Разделение ровно как `eval/run_frozen.py` vs `eval/run_all_live.py` в ai-v-massy.

## 7. Порядок внедрения

Ставится поверх этапов `PLAN.md`, ничего в них не переставляя.

| Шаг | Что | Проверка |
|---|---|---|
| 1 | `limits.py`, `ports.py`, `state.py`, `StubLlm` | `pytest`, ноль сетевых вызовов |
| 2 | `llm.py` + `probe.py` + redaction | контракт-тест на fake endpoint, `sanitized_summary` без ключа |
| 3 | `preflight.py` + `tools.py` с bounded output | unit + прогон на одном public fixture |
| 4 | `loop.py` + `verify.py` + `Outcome` | один public кейс проходит целиком через настоящий `run.sh` |
| 5 | `build_submission.py` + `check_submission.py` | zip ≤10 МБ, распакован, smoke зелёный |
| 6 | frozen-транскрипты + mutation suite | регрессии без токенов, устойчивость к переименованиям |

## 8. Что предлагается вынести в `docs/decisions.md`

Пока это гипотезы, а не решения:

- границы модулей задаются `typing.Protocol` + `dataclass`, внешние зависимости
  подключаются адаптером и только после probe;
- любой провал парсинга/схемы — типизированный исход, исключение недопустимо в
  штатной петле;
- `Outcome.verified` — единственный признак решённой задачи в наших метриках;
- локальные регрессии по умолчанию идут на frozen-транскриптах.

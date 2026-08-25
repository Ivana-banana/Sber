# Устойчивость tool-using петли для локальной LLM: аудит открытых каркасов

**Дата среза:** 25 августа 2026 г.  
**Целевой контур:** одна локальная модель 7B–32B, OpenAI-совместимый endpoint, внешний wall-clock лимит 600 с, бинарная оценка.  
**Проверенные ревизии:** SWE-agent `3ea751c`, OpenHands Software Agent SDK `041078f`, Aider `5dc9490`, smolagents `30bb116`, AutoGen `027ecf0`, Pydantic AI `61f0da5`. Полные SHA и локальный журнал источников приложены отдельным файлом `source_snapshot.md`.

## Краткий вывод

Открытые каркасы почти единодушно умеют **ограничивать количество попыток**, **возвращать модели структурированную ошибку инструмента** и **сокращать наблюдения**. Однако полноценный детектор стагнации встречается заметно реже: наиболее прямую реализацию из рассмотренных даёт OpenHands SDK — он сравнивает недавние action/observation/error события и различает одинаковую пару, серию ошибок, монолог и двухтактный цикл.[5] Остальные важные защиты обычно разнесены между лимитами, обработкой ошибок и необязательными проверками.

Для вашего контура главная практическая граница такова: **не полагаться на самодекларацию модели ни при остановке, ни при “готово”**. Счётчик шагов, deadline, fingerprint действий, typed tool result и внешний verifier должны принадлежать коду оркестратора. Это уже согласуется с приложенной архитектурой: она явно оставляет verifier вне модели и резервирует время на него. Но в приложенном `AgentState` ещё нет памяти fingerprints, кластеров ошибок или метрики прогресса; ими нельзя управлять, пока они не стали частью состояния.

> **Терминология.** В этом отчёте «ноль» — запуск, который при бинарной проверке не оставил правильного проверяемого результата. Отношение «нулей предотвращает / цена» — инженерный порядок приоритета, а не измеренная вероятность успеха: публичные проекты не публикуют сопоставимый benchmark именно для вашей модели, инструментов и oracle.

## Метод и границы достоверности

В основную таблицу включены только ветви, которые были прочитаны в публичном коде на закреплённой ревизии. Число «по умолчанию» означает именно default в коде либо в названной default-конфигурации. Если защита доступна только opt-in, это написано явно; если лимит отсутствует (`None`), это тоже результат, а не пропуск. Жалобы из issues/discussions не доказывают механизм: они помещены только в колонку риска и помечены как таковые.

Актуальный `OpenHands/OpenHands` теперь является frontend Agent Canvas; агентское ядро вынесено в официальный `OpenHands/software-agent-sdk`. Поэтому для OpenHands анализируется SDK, а не UI-репозиторий.[5]

## 1. Таблица реализованных механизмов

| Механизм | Проект | Ссылка на строки | Порог по умолчанию | Действие при срабатывании | Цена в шагах/токенах | Риск и известные жалобы |
|---|---|---|---:|---|---|---|
| Три подряд тайм-аута команды | SWE-agent | [конфиг][1], [ветвь][2] | `execution_timeout=30` с; `max_consecutive_execution_timeouts=3` | После каждого из первых двух timeout прерывает сессию и добавляет observation с отменённой командой. На третьем выбрасывает исключение, которое завершает шаг/запуск. | До `3 × 30 = 90` с tool-time; ещё до 3 последующих модельных ходов. | Серия **разных** медленных команд не покрыта. Не ограничивает полный wall-clock ответа модели. |
| Совокупный tool-time | SWE-agent | [конфиг][1], [проверка и autosubmit][3] | `total_execution_timeout=1800` с | Перед следующим шагом выбрасывает `_TotalExecutionTimeExceeded`; общий обработчик пытается autosubmission, т.е. забрать patch из `/root/model.patch`. | До 1800 с работы команд; токенов не добавляет. | Для внешнего лимита 600 с дефолт опасно велик; проверка происходит **между** командами и код сам комментирует, что не прерывает уже идущую команду. |
| Усечение observation | SWE-agent | [программный default][4], [шаблон/ветвь][5] | `100_000` символов в классе шаблонов; bundled `bash_only.yaml` переопределяет на `10_000` | В history передаётся начало `observation[:limit]`, число выкинутых символов и warning с советом использовать `head`/`tail`/`grep`/файл. Полный хвост в prompt не сохраняется этим механизмом. | 0 LLM-вызовов; нередко 1 дополнительный tool step для адресного чтения. | Начало лога не всегда содержит ошибку; хвост stderr/traceback можно потерять. |
| Cache-control и удаление старого diff из history | SWE-agent | [processor][6], [default config][7] | `last_n_messages=2`; для `RemoveRegex` default `keep_last=0` | Cache-control маркирует лишь последние user/tool сообщения; `RemoveRegex` вырезает `<diff>…</diff>` из старых history items, сохраняя последние `keep_last`. | Снижение повторно посылаемых токенов; cache-control не требует дополнительной генерации. | Это не семантическое summary: доказательство, которое осталось только в diff, может исчезнуть из модели. |
| Повтор action/observation, error-streak, монолог, пара A–B | OpenHands SDK | [окно 20][5], [пороги][8], [сравнение/переход][9] | Scan window `20` events; одинаковая action/observation — `4`; action-error — `3`; монолог — `3`; alternating — `6` | На action-error streak сначала добавляет **одно** environment user-message с nudge. При повторной стагнации или иных паттернах ставит `ConversationExecutionStatus.STUCK`, и loop выходит. | До 4 одинаковых пар; за nudge — минимум один следующий LLM step. Сравнение не требует модели. | Exact event equality не ловит семантически одинаковые команды с разными пробелами/аргументами. Issue OpenHands #11431 описывает раздражающий эффект: stop при loop может разрушить полезный контекст; это feature request на частичную recovery, а не подтверждение уже реализованного fallback.[15] |
| Жёсткий лимит итераций conversation | OpenHands SDK | [сигнатура][10], [run loop][11] | `max_iteration_per_run=500` | При лимите emit-ится run-limit error, run прекращает продолжать бесконечно. | До 500 итераций — для 600 с это слишком много без более жёсткого deadline. | Лимит не является доказательством результата; внешнему короткому раннеру нельзя наследовать это число. |
| Усечение shell output и timeout команды | OpenHands SDK | [execute skill][12] | `MAX_OUTPUT_SIZE=50×1024` байт; `DEFAULT_TIMEOUT=10.0` с | При превышении оставляет первые 50 KiB и добавляет `... [output truncated]`; timeout возвращает текстовую ошибку. | 0 дополнительных LLM-вызовов; потерянные данные требуют повторного адресного запроса. | Это prefix-only усечение именно у `execute` skill; поздний stack trace легко теряется. |
| LLM-конденсация history | OpenHands SDK | [settings][13], [логика cap][14] | `max_tokens=None`, `keep_first=2`, `minimum_progress=0.1`, `hard_context_reset_max_retries=5`, scaling `0.8` | При включении суммаризирует forgotten events отдельной LLM; хранит начальные события и заменяет середину condensation event. При hard reset уменьшает размер входа до 5 попыток. | Минимум 1 дополнительный LLM-вызов на condensation; возможны до 5 reset-попыток. | Это **не безусловный default лимит токенов**: `max_tokens=None`, поэтому без настройки срабатывание по токенам не гарантировано. Суммаризация может опустить точную ошибку или путь. |
| Коррекционный reflection после невалидного edit/lint/test | Aider | [cap и loop][16], [ошибки edit и внешний feedback][17] | `max_reflections=3` | Ошибка edit format, подтверждённый lint error или test error записывается как `reflected_message` и возвращается модели; после трёх reflections Aider печатает warning и останавливает коррекцию. | До 3 дополнительных LLM turns; lint/test дают внешний tool-time. | Lint/test-петля завязана на включённые auto flags и пользовательское подтверждение в этой ветви. Aider не является единым typed-tool loop, поэтому переносить надо схему «ошибка → bounded retry», а не интерфейс. |
| Сжатие истории рекурсивным summary | Aider | [порог/рекурсия][18], [замена history][19] | `ChatSummary.max_tokens=1024`; максимум глубины `3`; при неуказанном input limit используется `4096−512=3584` токена | Старую head-часть отправляет summarizer-модели; сохраняет один user-message summary, оставляет свежий tail. При завершении потока `done_messages` заменяется summary только если history за это время не изменилась. | 1+ отдельный LLM-вызов; recursion до depth 3. | Необратимо теряются старые точные tool outputs. Issue Aider #4113 — пользовательская просьба лучше управлять большим контекстом, не доказательство дефекта реализации.[20] |
| Жёсткий cap шагов + best-effort final-answer | smolagents | [default и loop][21] | `max_steps=20` | После 20 шагов вызывает `provide_final_answer(task)`, сохраняет `AgentMaxStepsError` и всё равно выдаёт `FinalAnswerStep`. | До 20 шагов плюс 1 заключительный model call. | Хорошо для «оставить что-то», плохо для бинарной оценки: fallback может быть неподтверждённой догадкой. Discussion #1257 описывает именно hallucinated/unverified `final_answer` при CodeAgent; это пользовательское наблюдение, не установленный bug версии.[21] |
| Проверка final answer (opt-in) | smolagents | [вызов и default][22] | `final_answer_checks=[]` | Если пользователь передал callbacks, все должны вернуть truthy; иначе выбрасывается `AgentError`. При default пустом списке **никакой проверки нет**. | Стоимость определяется checker; встроенно 0. | Нельзя считать наличие API гарантией: default допускает раннюю самодекларацию. |
| Unknown tool / неверные аргументы | smolagents | [валидация][23], [loop сохраняет AgentError][24] | Нет счётчика retry; cap — общий `max_steps=20` | Неизвестное имя выдаёт перечисление допустимых tools; schema/тип аргумента становится `AgentToolCallError`; исключение записывается в action step, и agent идёт в следующий ход. | Обычно 1 лишний LLM turn на исправление; в худшем случае все 20 шагов. | Хорошая typed error, но нет отдельной короткой квоты для «одинаковой protocol error». |
| Усечение print/наблюдений в CodeAgent | smolagents | [default executor][25], [head+tail][26] | executor print output `50_000` символов; generic `truncate_content` default `20_000` символов | Сохраняет половину начала и половину конца с маркером. В CodeAgent результат дополнительно проходит `truncate_content`. | 0 новых LLM calls; адресное чтение требует отдельного шага. | 50 KiB/символов для малой модели всё ещё очень велико; грубый char cap не равен token cap. |
| Лимит внутренних tool итераций + structured error | AutoGen | [default][27], [JSON/unknown tool][28] | `max_tool_iterations=1` | Неуспешный JSON аргументов и отсутствие tool превращаются в `FunctionExecutionResult(is_error=True)` и возвращаются в разговор; больше tool-итераций требует явного повышения параметра. | 1 tool iteration по default; исправление требует нового agent/team turn. | Командный manager обязан уважать StopMessage/termination. Issue #4307 описывает прежний риск постоянных tool calls и отмечает, что StopMessage сам по себе не останавливает team без подходящего termination condition.[29] |
| Termination по сообщениям, токенам, времени | AutoGen | [message/token][30], [timeout][31] | У этих classes **нет числового default**: `max_messages`, token caps и `timeout_seconds` обязательны у вызывающего кода | При срабатывании создаёт `StopMessage` с фактическими счётчиками/timeout. | 0 добавочных model calls; возможна потеря незавершённой работы, если приложение не сериализует артефакт само. | Эти условия не резервируют время на verify и не гарантируют artefact finalization; их нужно связывать с вашим finalize handler. |
| Budget предзапросов/вызовов инструментов | Pydantic AI | [UsageLimits][32] | `request_limit=50`; `tool_calls_limit=None`; token limits `None`; pre-count tokens `False` | Перед следующим request/tool call бросает `UsageLimitExceeded`; request cap включает requests к модели, tool-call cap применяется только если его задал пользователь. | 0 токенов на проверку; pre-count, если включён, может стоить отдельного provider call. | Default 50 слишком высок для 600 с с локальной 7B–32B; `tool_calls_limit=None` означает, что защиту надо включать явно. |
| RetryPromptPart для schema/tool-result ошибки | Pydantic AI | [обработка][33] | Единый числовой default retries в этом месте не задан: зависит от Agent/Tool configuration | `ModelRetry`/`RetryPromptPart` превращается в структурированное сообщение модели с `tool_name` и `tool_call_id`; loop может исправить вызов. | Как минимум 1 следующий LLM request на recovery. | Без малого per-error cap можно переиспользовать общий request budget на одну и ту же ошибку формы. |

### Что **не** найдено как готовая защита

В SWE-agent на зафиксированной ревизии нет runtime-детектора exact repeat/stagnation в `sweagent/agent` и `sweagent/tools`; он полагается на командные timeout и общие циклы. В Aider нет универсального parser-native tool loop: его `reflection` применён к edit/lint/test workflow. В smolagents и Pydantic AI есть структурированные ошибки и глобальные caps, но не встроенный detector пары A↔B по умолчанию. Это важно: не приписывать framework’ам прогресс-детектор только потому, что у них есть `max_steps`.

## 2. Четыре класса отказов

### 2.1. Повтор одного действия или пары действий

**Что реально детектируется.** OpenHands SDK — единственный из рассмотренных, где это отдельная логика runtime. Он берёт не всю историю, а последние 20 событий после последнего user-message. Для одной и той же action/observation пары нужны 4 совпадения; для alternating паттерна нужны 6 событий, причём сравниваются действия и наблюдения через одно (A, B, A, B, …). Идентичность сравнивает тип, source, thought, action и tool name, но игнорирует динамические идентификаторы.[5] [9]

**Почему это полезно малой модели.** Дешёвый detector не требует нового LLM-вызова. Он ловит наиболее разрушительный случай: одна и та же команда получает тот же результат, а модель повторяет её потому, что не интерпретировала ошибку. В случае повторной одинаковой action-error серии OpenHands сначала меняет стратегию мягко — одним environment nudge, — и лишь затем ставит `STUCK`.[9]

**Чего это не ловит.** Exact fingerprint не увидит `grep foo`, затем `grep  foo`, или цикл, который меняет случайный временный файл. Поэтому в минимальном контуре полезны два слоя: exact canonical fingerprint и отдельный detector отсутствия наблюдаемого прогресса. Нельзя сразу включать embedding/LLM similarity: это добавит стоимость и нестабильность раньше, чем будет baseline.

### 2.2. Отсутствие прогресса при формально разных действиях

В рассмотренных каркасах это почти всегда замаскировано другими guard’ами, а не измеряется напрямую. OpenHands частично приближается к этому через alternating A↔B, monologue и error-streak; Aider интерпретирует failure lint/test как feedback для bounded reflection; AutoGen и Pydantic AI останавливают бюджет, но не доказывают, что действия были бесполезны.[8] [16] [32]

Для вашего локального контура прогресс следует определить **детерминированно и по классу задачи**, не через самооценку модели. Минимальный общий вектор прогресса: `(версия workspace/хэш изменённых tracked файлов, нормализованный exit class, нормализованный verifier status, появление/изменение требуемого artefact)`. Стартовый rule: остановить исследование и перейти в verify/finalize, если в последних **4** action-result парах нет ни одного из таких событий и нет новой гипотезы. Число 4 взято как консервативный перенос дефолта OpenHands для идентичной пары, а не как доказанная оптимальная величина.[8]

Важно не считать «файл изменился» автоматически положительным прогрессом: модель может бесконечно переформатировать файл. Поэтому изменение workspace повышает progress только при новом exit/verifier class, новом требуемом artifact или уменьшении набора наблюдаемых ошибок.

### 2.3. Преждевременное объявление результата без внешней проверки

smolagents явно показывает опасную форму graceful degradation: после `max_steps=20` он делает ещё один вызов `provide_final_answer`, затем возвращает ответ даже с `AgentMaxStepsError` в памяти.[21] API `final_answer_checks` способен это предотвратить, но по умолчанию список пуст.[22] Aider может вернуть failing lint/test модели, но это зависит от включённых флагов и confirmation; SWE-agent default config instructs модель перепроверить reproduction script, но это prompt-инструкция, а не code-enforced oracle.[7] [16]

> **Правило для бинарной оценки:** `DONE` не является состоянием модели. `DONE` — это кодовый переход только после `verifier.status == PASS` и наличия обязательного artefact. Любой текст модели «готово» до verifier трактовать как заявку на `VERIFY`, а не как результат.

Если verifier красный и есть budget, разрешать **один** направленный recovery pass, что совпадает с вашим `MAX_RECOVERY_PASSES=1`. Если verifier недоступен, итог обязан быть `UNVERIFIED`/`FAIL` с сохранёнными artefact paths, а не положительным answer. Это предотвращает опасный ложный PASS, хотя не превращает неразрешённую задачу в успех.

### 2.4. Несуществующий инструмент либо неверная форма вызова

Здесь open-source проекты демонстрируют полезный общий паттерн: никогда не делать из protocol failure исключение без наблюдения. smolagents перечисляет допустимые tool names при unknown name, валидирует schema аргументов и кладёт `AgentToolCallError` в следующий action step.[23] [24] AutoGen превращает `JSONDecodeError` и отсутствие имени в `FunctionExecutionResult(is_error=True)`, поэтому модель видит ровно ту ошибку, которую должна исправить.[28] Pydantic AI передаёт `RetryPromptPart` с именем tool и идентификатором вызова.[33]

Для локальной модели следует возвращать короткий typed payload, а не traceback: `TOOL_ERROR {code, tool, allowed_tools, expected_schema, received_shape, retryable}`. Для неизвестного имени полезно показать полный allow-list, но ограничить его компактным списком локальных инструментов. Для argument error показывать path (`arguments.pattern`), expected type и краткий пример valid call. После **2** одинаковых protocol errors надо прекратить execution этого tool и потребовать другой tool или finalize: это дешевле, чем сжечь общий лимит 30 шагов.

## 3. Ограничение объёма наблюдений и истории

| Каркас | Механизм | Число по умолчанию | Что остаётся модели | Что происходит с отброшенным содержимым |
|---|---|---:|---|---|
| SWE-agent | observation truncation | `100_000` chars в class default; `10_000` в `bash_only.yaml` | Начало output и warning с количеством elided chars | Не переносится в prompt этим путём; предлагается перенаправить output в файл и читать адресно.[4] [5] |
| SWE-agent | history processor | cache последних `2` role-tagged messages; regex `keep_last=0` | Последние cache-marked сообщения; diff может быть вырезан из старых | Удаляется из model history; trajectory остаётся отдельным run artifact.[6] [7] |
| OpenHands SDK | execute skill | `50×1024` bytes; timeout `10.0` s | Первые 50 KiB и literal truncation marker | Остаток не прикладывается execute skill; пользователь/агент должен запросить меньший/адресный вывод.[12] |
| OpenHands SDK | generic `maybe_truncate` | call-site dependent | Head + tail вокруг notice; если передан `save_dir`, полный content сохраняется во временный файл, path и line указаны в notice | В зависимости от call-site либо файл сохраняется, либо середина теряется.[34] |
| OpenHands SDK | LLM condensation | token trigger `None`; `keep_first=2`; min progress `0.1`; reset retries `5` | Начало, свежий view и LLM summary forgotten events | Исторические события заменяются condensation event; исходный event store остаётся основой conversation state, но не полным prompt.[13] [14] |
| Aider | ChatSummary | summary budget `1024` tokens; recursion `depth>3`; input fallback `3584` (`4096−512`) | Один user-message с summary + свежий tail | Старый head заменяется summary; исходный диалог больше не входит в `done_messages`.[18] [19] |
| smolagents | print/executor output | `50_000` chars; общий helper `20_000` chars | Половина начала + половина конца + marker | Середина удаляется из observation; отдельной pointer-to-file семантики у helper нет.[25] [26] |
| AutoGen / Pydantic AI | В рамках рассмотренных основных ветвей | Нет единого default observation cap | Зависит от подключённого model context/toolset | Нельзя предполагать, что framework сам спасёт малую модель от гигантского stdout; cap должен быть у вашего tool adapter. |

Для вашей архитектуры уже выбран более подходящий для 7B–32B профиль: shell output — **4000 байт head + 2000 байт tail**, read window — **8000 байт / 200 строк**, list — **100** entries, search — **50** matches, preflight — **1500** chars. Это сильнее OpenHands/smolagents по защите контекста, но сохраняет хвост, где обычно лежит exception. Практический недостающий элемент — обязательный `full_output_path` в typed `ToolResult`: модель видит bounded fragment и путь, а оркестратор хранит полное доказательство вне prompt.

## 4. Принудительное завершение и сохранение результата

### Что делают проекты

SWE-agent при общем tool-time или ошибке идёт в `handle_error_with_autosubmission`: пытается материализовать patch и сохраняет trajectory после каждого шага.[3] smolagents на шаговом лимите намеренно просит final answer, чтобы не вернуть пустоту.[21] AutoGen возвращает `StopMessage`, но сам по себе он не создаёт artefact — сохранение остаётся обязанностью приложения.[30] [31] Pydantic AI бросает `UsageLimitExceeded`, что хорошо для атомарного прерывания нового request/tool call, но не гарантирует output manifest.[32]

Ни один из этих механизмов сам по себе не доказывает одновременно две вещи: что процесс не пересечёт внешний deadline и что правильный deliverable уже лежит на диске. Для вашей оценки нужен собственный finalize protocol.

### Предлагаемый протокол для внешнего лимита 600 с

| Момент | Решение кода | Почему это оставляет результат |
|---|---|---|
| Инициализация | Создать `run_dir`, `events.jsonl`, `result.json.tmp`; записывать action, normalized result, artifact paths **до** следующего LLM call. | Даже при crash есть append-only trace и известны созданные файлы. |
| До 90 с | Ваша текущая `EARLY_DELIVERABLE_DEADLINE_S=90` означает: к этому моменту уже должен быть минимально проверяемый candidate/manifest, а не только мысль модели. | При неизвестном фактическом timeout 120 с kill теряет refinement, а не все артефакты. |
| До research-action | Использовать `may_act()` плюс предсказанный timeout: не запускать действие, если его budget не помещается в `spendable_s`; каждой shell process group дать timeout `min(120, spendable_s)` и убивать всю группу. | Внешний deadline не съедается дочерним процессом. |
| `remaining_s ≤ reserve` | Запретить новые exploration calls; выполнить deterministic verifier и атомарно переименовать `result.json.tmp → result.json`. | Вердикт и пути артефактов появятся до timeout без надежды на последний LLM turn. |
| Verifier FAIL | Разрешить ровно `MAX_RECOVERY_PASSES=1` только если recovery + повторный verify помещаются **вне** reserve; иначе зафиксировать FAIL с diagnostic. | Избегает бесконечной «ещё одной правки» и не тратит резерв verifier. |
| Hard stop / exception | Финальный `finally`: kill process groups; вызвать идемпотентный `finalize(status, reason, verifier, artifacts, last_event)`; не инициировать новую модельную генерацию. | Результат остаётся либо `PASS`, либо честный `UNVERIFIED/FAIL`, но не исчезает. |

У приложенного `Budget` reserve вычисляется по формуле

> `verify_reserve_s = max(30, min(120, total_s × 0.20))`.

При 600 с это ровно **120 с**; spendable окно равно **480 с**, а `early_deadline_s = min(90, 600−120) = 90 с`. Текущий `tool_timeout_s = max(0, min(120, spendable_s))` правильно не даёт одной shell-команде съесть reserve. Однако `may_recover()` сейчас проверяет только `remaining_s > 0`: его следует сделать не слабее `may_act()` и вычитать оценку повторной проверки, иначе recovery может вторгнуться в reserve. В `AgentState` уже есть append-only `audit` и `artifacts`; надо добавить persist-after-event и detector state, а не только держать это в локальных переменных.

## 5. Минимальный набор из пяти детекторов

Порядок ниже максимизирует ожидаемую защиту от бинарного нуля на единицу простого кода и дополнительных токенов. Пороги — **стартовые baseline**, а не вечные константы: после первых 30–50 локальных прогонов их нужно калибровать по trace и verifier outcome.

| Ранг | Детектор / guard | Стартовый порог | Откуда число | Реакция | Почему отношение «нулей / цена» высоко |
|---:|---|---:|---|---|---|
| 1 | **Deadline + reserve + атомарный finalize** | total `600` с; reserve `min(max(0.20×T,30),120)`; early artefact `90` с | Ваши `limits.py`/`state.py`; при T=600 reserve=120, spendable=480 | На reserve stop exploration, verifier, `result.json`, kill group при hard stop. | Почти нулевая token-цена; предотвращает самый дорогой ноль — отсутствие любого оставленного результата. |
| 2 | **Verifier-gated DONE** | `1` PASS от внешнего oracle перед DONE; `1` recovery pass | Ваш `MAX_RECOVERY_PASSES=1`; smolagents показывает, что без checks default допускает final answer.[21] [22] | Модельное DONE переводится в VERIFY; PASS — финал, FAIL — один directed fix или честный FAIL. | Один tool/verifier вызов обычно дешевле ложного финала и ловит широкий класс ошибок, включая «модель сказала готово». |
| 3 | **Protocol guard: unknown tool / schema / execution error** | Блокировать после `2` одинаковых `(tool, error_code, canonical_args)` | Первый recovery нужен для нормальной ошибки; второй подтверждает, что малая модель не поняла форму. Структура подсказана AutoGen/smolagents/Pydantic AI.[23] [28] [33] | Не выполнять третий раз; вернуть короткий typed error, require другой tool или finalize. | Несколько строк canonicalization + dict; 1–2 дополнительных turns вместо сжигания до 30 шагов. |
| 4 | **Exact repeat и A↔B loop guard** | `4` одинаковые `(tool, canonical_args, result_fingerprint)`; `6` событий для A↔B | Прямо из OpenHands defaults: action/observation=4, alternating=6.[8] | На третьем error-streak — один nudge с новой гипотезой; на пороге — block tool, then VERIFY/FINALIZE. | O(1) хэш и ring buffer; не требует LLM или embeddings. Ловит заметную долю «крутится» локальных моделей. |
| 5 | **No-material-progress window** | `4` action-result пары без нового artefact, нового verifier/exit class или новой гипотезы | Консервативно от OpenHands 4-repeat threshold; именно неточный перенос требует baseline-калибровки. | Прекратить exploration; добавьте короткое message «нет измеримого прогресса» и перейдите в verify/finalize, не в новую свободную генерацию. | Небольшой deterministic state; ловит формально разные команды, которые exact hash обходит. Риск false positive контролируется тем, что действие — VERIFY, не немедленный PASS. |

### Минимальная структура состояния

Добавьте в `AgentState` ограниченный ring buffer из последних 6 action records, а не полный stdout:

```text
ActionRecord(
  tool_name,
  canonical_args_sha256,
  result_class,          # OK | EMPTY | TIMEOUT | PROTOCOL | EXEC_ERROR
  result_fingerprint,    # exit_code + normalized bounded output + changed paths
  hypothesis_id,
  artifact_delta,
  verifier_status,
  monotonic_time,
)
```

`canonical_args` должен исключать динамические поля (`call_id`, timestamp, temp path), сортировать JSON keys и нормализовать whitespace в shell command только там, где это безопасно. `result_fingerprint` не должен хэшировать полный секретный output; безопаснее хэшировать redacted bounded output, exit code и artifact delta. Это даёт именно тот deterministic слой, которого не хватает текущему `audit`.

### Что не ставить в первый baseline

Не добавляйте сразу LLM-judge прогресса, embedding similarity, отдельного planner-агента или многократное re-summarization. Они могут быть полезны позже, но добавляют вызовы, latency и новую точку отказа. Для 7B–32B с 600 с сначала полезнее простые детерминированные guards и внешний oracle.

## 6. Непроверенное: идеи из статей и блогов без принятой реализации в этом контуре

Этот раздел **не является доказательством механизма в рассмотренных исходниках** и не входит в таблицу реализованных защит.

| Идея | Что предлагают источники | Почему пока не включать как baseline |
|---|---|---|
| Semantic near-duplicate detector | Сравнивать семантическую близость tool calls/intent, чтобы поймать близкие, но не exact запросы. AWS community article также отмечает в комментарии, что пороги, откалиброванные на synthetic traces, плохо переносятся на реальные.[35] | Нужны embedding model, калибровка порога и защита от ложных совпадений; exact hash + progress vector дешевле и наблюдаемее. |
| Инфраструктурное coalescing одинаковых in-flight calls между агентами | Не исполнять одновременно один `(tool, canonical input)` в swarm, а раздать общий result. | У вас один одноконтурный агент; многопоточность/дедупликация создаст больше кода, чем предотвратит. |
| Явные `SUCCESS`/`FAILED` строки в tool output как главный stop signal | Блог демонстрирует 14 против 2 вызовов в toy scenario и рекомендует явные terminal states.[35] | Это разумный дизайн observation, но блоговая demo не доказывает перенос на ваши четыре класса задач. Включить как format typed `ToolResult`, измерить. |
| Self-critique / reflexion моделью | Обзорный blog рекомендует self-critique и no-progress termination как patterns.[36] | Самокритика малой модели — ещё один LLM call и может образовать reasoning loop. В вашем случае внешний verifier сильнее и дешевле по риску. |
| Сбросить лишь участок loop из памяти и перезапустить | Запрос OpenHands #11431 предлагает найти начало петли, удалить только loop-tail и спросить restart.[15] | Это полезная UX-идея, но в коде текущего SDK examined path при STUCK останавливается; корректная локализация причины петли и безопасный restart требуют отдельной оценки. |

## Практическое решение для v1

Оставьте один orchestrator и один LLM вызов на шаг. Реализуйте сначала: **(1)** persist/finalize deadline, **(2)** verifier-gated DONE, **(3)** typed protocol errors с cap 2, **(4)** 4/6 exact-cycle guard, **(5)** 4-step no-progress window. Сохраните текущие строгие лимиты observation, но добавьте pointer к полному redacted output. Тогда даже при слабой модели провал превращается из «600 с молчаливого spinning и ноль» в короткий trace с диагностикой, сохранёнными artefacts и, когда это возможно, одним проверенным recovery pass.

## References

[1]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/tools/tools.py#L139-L152 "SWE-agent: execution timeout defaults"
[2]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L958-L989 "SWE-agent: consecutive execution timeout handling"
[3]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L1013-L1025 "SWE-agent: total timeout check"
[4]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L65-L82 "SWE-agent: template observation limit"
[5]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/conversation/stuck_detector.py#L18-L44 "OpenHands SDK: StuckDetector scan window"
[6]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/history_processors.py#L261-L337 "SWE-agent: history processors"
[7]: https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/config/default.yaml#L13-L69 "SWE-agent: bundled default configuration"
[8]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/conversation/types.py#L136-L162 "OpenHands SDK: stuck detection thresholds"
[9]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L706-L729 "OpenHands SDK: nudge and STUCK transition"
[10]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/conversation/conversation.py#L62-L85 "OpenHands SDK: conversation default iteration cap"
[11]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L1902-L1975 "OpenHands SDK: conversation run loop"
[12]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/skills/execute.py#L26-L86 "OpenHands SDK: execute output and timeout limits"
[13]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/settings/model.py#L191-L264 "OpenHands SDK: condenser settings defaults"
[14]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L96-L125 "OpenHands SDK: effective condensation token cap"
[15]: https://github.com/OpenHands/OpenHands/issues/11431 "OpenHands issue #11431: fallback after stuck loop"
[16]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L861-L945 "Aider: bounded reflection loop"
[17]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1585-L1623 "Aider: lint/test feedback"
[18]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L7-L96 "Aider: ChatSummary policy"
[19]: https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L98-L123 "Aider: summary replacement message"
[20]: https://github.com/Aider-AI/aider/issues/4113 "Aider issue #4113: context management request"
[21]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/agents.py#L296-L310 "smolagents: max steps default and final checks API"
[22]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/agents.py#L568-L637 "smolagents: final answer and max-step fallback"
[23]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/agents.py#L1453-L1502 "smolagents: tool name and argument validation"
[24]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/agents.py#L594-L604 "smolagents: AgentError continuation"
[25]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/local_python_executor.py#L1584-L1661 "smolagents: executor output bound"
[26]: https://github.com/huggingface/smolagents/blob/30bb1161095dbae2271e6bc3cc4c219cc3897a57/src/smolagents/utils.py#L254-L265 "smolagents: head-tail truncation"
[27]: https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L734-L744 "AutoGen: AssistantAgent tool iteration default"
[28]: https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L1538-L1624 "AutoGen: structured tool call errors"
[29]: https://github.com/microsoft/autogen/issues/4307 "AutoGen issue #4307: termination conditions within agents"
[30]: https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/conditions/_terminations.py#L57-L105 "AutoGen: maximum message termination"
[31]: https://github.com/microsoft/autogen/blob/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat/conditions/_terminations.py#L354-L397 "AutoGen: timeout termination"
[32]: https://github.com/pydantic/pydantic-ai/blob/61f0da5f7988622a9a10a10bf9f429db3bd52e19/pydantic_ai_slim/pydantic_ai/usage.py#L418-L493 "Pydantic AI: UsageLimits defaults"
[33]: https://github.com/pydantic/pydantic-ai/blob/61f0da5f7988622a9a10a10bf9f429db3bd52e19/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L679-L700 "Pydantic AI: retry prompt on tool errors"
[34]: https://github.com/OpenHands/software-agent-sdk/blob/041078f26698ccba4b78af6c3069e37bb1556b32/openhands-sdk/openhands/sdk/utils/truncate.py#L50-L112 "OpenHands SDK: generic truncation and optional persistence"
[35]: https://dev.to/aws/how-to-prevent-ai-agent-reasoning-loops-from-wasting-tokens-2652 "AWS community post: debounce and clear terminal tool states"
[36]: https://happycapy.ai/blog/loop-engineering-ai-agents "Happycapy blog: loop engineering patterns"

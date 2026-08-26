# Сокращение токенов в tool-using агенте: доказательства, риски и план абляций

**Контекст рассмотрения.** Рассматривается один локальный агент с циклом `inspect → plan → act → observe → verify`, до 30 итераций, с накоплением истории и приоритетом ранжирования **число решённых задач → суммарные токены → wall time**. Поэтому полезна только экономия, которая не отнимает уже решённые задачи. Результаты из RAG, крупных облачных моделей и long-horizon бенчмарков ниже являются не прогнозом для 15 задач, а основанием для локальной проверки.

> **Правило интерпретации.** Метка **«непроверено»** означает, что для самого приёма не найдено опубликованного end-to-end измерения на сопоставимом tool loop. Процент из другой постановки не переносится как ожидаемая экономия вашего агента.

## Короткий вывод

Наиболее надёжная первая экономия не требует компрессора: **байт-в-байт дедупликация**, запрет агенту переписывать уже увиденные наблюдения, сохранение полного вывода инструмента в локальном артефакте и адресное повторное чтение. Напротив, универсальное `head + tail` для stdout — рискованная эвристика: ошибка, путь, имя упавшего теста или важная строка traceback могут лежать в середине. Работы по терминальным и coding-агентам прямо связывают такие точные строки с последующим верным действием [1] [2].

Суммаризация, скользящее окно и сокращение системного/tool-control промпта могут экономить много, но это **второй этап после baseline**. Для agent-control prompt уже при сохранении 75% текста получена небольшая, но реальная регрессия успеха; при 50% и ниже спад резкий [3]. При вашем запрете на регрессию это не «безопасная оптимизация», а только контролируемая абляция.

## 1. Input-токены: таблица доказательств

| Приём | Ожидаемая экономия | Риск потери качества | Как измерить у себя | Источник |
|---|---:|---|---|---|
| **Байт-в-байт дедупликация повторного observation / одинакового фрагмента** | Ровно доля удалённых идентичных токенов. В измеренном RAG: 0,07% на чистом query-наборе, 24,03% на построенном enterprise-корпусе; 80,34% в cumulative-chat канале, но последнее — **не** quality-validated агентный результат. | Низкий, если удаляются только идентичные записи, сохраняются порядок первого вхождения, provenance и ссылка на raw. Не применять к near-duplicates. | До prompt assembly хешировать нормализованное **без изменения байтов** содержимое записи; логировать `raw_bytes`, `dedup_bytes`, `hash`, число удалённых записей. Принимать лишь при отсутствии потери решённых baseline-задач. | Multi-vendor RAG study дала 14,13% и 71,98% byte reduction с верхней 95%-границей materially-different результата 1,40–3,25% и 1,90–4,34%; это preprint и не terminal-agent benchmark [12]. |
| **Усечение tool output фиксированным `head + tail`** | **Непроверено для данной эвристики.** Лимит гарантирует только максимум видимых байтов, не экономию в токенах: байт/токен зависит от tokenizer и типа лога. | Высокий для shell/traceback: критическая строка может быть в середине. | Сравнить raw-full vs текущие `4000+2000` bytes при одинаковых seed, deadline и actions; отдельно считать задачи, где следующий action меняется, а verifier становится красным. Сохранить для ручного анализа строку, потерянную truncation. | Нет качественного published измерения именно head+tail. TACO оставляет error/exception outputs несжатыми и называет paths, failing test names и errors критическим доказательством [1]. |
| **Типизированное сжатие нового observation с raw-артефактом и повторным чтением** | Ближайшие данные: TACO уменьшил *полные* токены на 12,1–27,0% на четырёх coding-бенчмарках при сохранении/росте accuracy; на TB 2.0 — лишь 2,7% [1]. CoACT на одной конфигурации SWE-bench: 3,795M→2,428M total tokens (−36,0%) и pass@1 57,0→60,5 [2]. Это не обещание для вашей модели. | Средний. Без action-preservation/re-read возможна потеря точного свидетельства. | Всегда писать полный stdout/stderr в локальный файл; в prompt передавать exit code, command, stderr, маркеры `error|exception|fail|traceback`, первый/последний блок строк, путь + offsets. На error/non-zero — не сжимать или позволять `read_range`. Логировать recovery reads. | TACO [1], CoACT [2]. CoACT специально отклоняет компрессию, меняющую следующий action. |
| **Суммаризация старой истории** | ACON сообщил 26–54% снижения *peak tokens*, в задачах 15+ шагов, с largely preserved performance. Это требует отдельного оптимизированного компрессора и не является total-input экономией [4]. | Средний–высокий, особенно при повторной суммаризации. Сводка может потерять текущую позицию в исполнении, завершение или неудачу инструмента. | Один порог compaction, одна summary-schema; сырую последнюю пару `action+observation` не заменять. Сравнить с full history по solved, verifier failure, duplicate/replay actions и input tokens, включая вызов суммаризатора. | В AppWorld сжатая история меняла распределение следующего action сильнее: divergence 0,233 против 0,149 при сохранённом raw recent update; после compaction появлялось на 0,108 больше blocked/error actions на первом ходе [5]. |
| **Скользящее окно (удалить старое, оставить последние шаги)** | **Непроверено как универсальный процент.** Оно задаёт верхнюю границу контекста, но полезная экономия зависит от длины траекторий. | Средний–высокий: теряются старые факты и уже проверенные выводы. Иногда безопаснее summary, иногда наоборот. | Зафиксировать окно в токенах по tokenizer, а не в числе сообщений; сравнить ровно с тем же max visible context, что и у summary. Считать обращения к уже выполненным инструментам и повторную разведку. | В AppWorld FIFO на 16K/8K/4K match/slightly outperformed summary; только при 2K summary был лучше: 72,8% против 42,2% [5]. |
| **Сокращение/вынос статики из system prompt** | Для agent-control context сохранение 75% текста снизило attempt-inclusive input/run на 21,6% (generic rewrite) или 19,3% (section based), но это измерено в специфичном benchmark [3]. | Высокий, если сжимать tool names, schema, preconditions, policies, recovery или output protocol. | Сначала отделить то, что может enforce код: step budget, timeout, schema validation, repeat guard. Остальной control prompt держать неизменным. Любой rewrite сравнить на held-out task packages и parser/tool errors. | Full context 93,8%; при 75% retained: 92,7% generic rewrite и 92,4% section-based. При 50% section-based 67,9%, generic 37,8%, mechanical truncation 22,1%; при 35% — 47,0/19,9/16,6% [3]. |
| **Один активный playbook вместо всех playbooks** | **Непроверено как изолированная published абляция.** Экономия детерминирована: токены неактивных playbook не входят в каждый вызов. | Средний: неверная heuristic classification может скрыть нужную процедуру. | Логировать класс, активный playbook, override и task result; сравнить один playbook, все playbooks и «короткий общий + активный». Считать input на каждом call. | Архитектурно разумно, но найденные работы не изолируют именно router/playbook. Не переносить сюда проценты ACON/LLMLingua. |
| **Ссылки `path + range + hash` на файл вместо полного content** | **Непроверено для короткого tool loop.** На большом filesystem-memory организованный store примерно вдвое снижал retrieval cost, но не повышал answer quality сам по себе [11]. | Низкий–средний: ссылка без удобного `read_range/grep` превращает доказательство в недоступное. | `tool_result` хранит artifact path, byte/line range, SHA-256, status и короткий preview. Агент должен суметь детерминированно прочитать любой диапазон без сети. Логировать повторные чтения и их токены. | Файловая память ускоряет поиск только при больших объёмах; организация деградировала у слабых management agents [11]. |
| **LLMLingua/токен-уровневое pruning для tool history** | Общая prompt-компрессия заявляет до 20×; LongLLMLingua — до 4× меньше tokens и +21,4% NaturalQuestions. Это document/RAG evidence, не tool trace [6] [7]. | Высокий для tool identifiers, аргументов и формата. | Не включать до baseline. Если тестировать — freeze compressor, прогонять same task suite, проверять action parsing/argument errors и recovery. | На agent-control benchmark unmodified LLMLingua дал 10,0% success при 50% и 0% при 25%/10%; при 25% все runs failed action parsing [3]. |

### Что именно означает «ссылка вместо содержимого»

Ссылка — не память модели. Она экономит resident context, только если следующий шаг в самом деле не нуждается в полном тексте. Поэтому корректный контракт такой: **в контекст попадает короткий, проверяемый index; raw payload остаётся локально и доступен инструментами `grep`, `read_range` и `tail`**. Нельзя заменить `stderr` из неуспешной команды только хешем: именно он может объяснять следующий шаг.

## 2. Output-токены: что известно, а что является фольклором

| Приём | Ожидаемая экономия | Риск качества | Как измерить у себя | Источник |
|---|---:|---|---|---|
| **Ограничение длины явного reasoning** | TALE на reasoning benchmarks: −67% tokens при <3% падении accuracy для dynamic budget prompting; post-training около −50% [9]. Это **не** агентный результат и не обосновывает единый cap. | Двусторонний. Слишком длинный CoT может зациклиться; слишком короткий отнимает планирование, особенно у малой модели. | Разделить `action` и необязательный `plan/recovery`; измерить длину успешных responses. Пререгистрировать cap по p95 успешного baseline, а не по «красивому» числу. Следить за incomplete tool args, parser failures и verifier result. | CoT прибавлял в среднем 7,4 pp pass@1 на code tasks, но до ≈10× tokens и truncation до 17,1%; 90,4% truncations были detected как loops [10]. |
| **Structured action вместо свободного текста** | Для «JSON вместо прозы» отдельного published процента **нет**. Экономия равна фактически устранённым словам объяснения и должна быть измерена телеметрией. | Низкий для синтаксиса с native tool call/schema; средний для семантики arguments. Валидация схемы не делает неверные значения верными. | Сравнить native tool call/JSON schema `action` с той же политикой и без текста `Observation recap`; считать completion tokens, valid parses, correct tool/args, solved. | Структурный контроль почти устраняет syntax errors, но сохраняет semantic/required-field failures [13]. |
| **Не пересказывать observation в ответе модели** | **Непроверено end-to-end.** Механически экономит длину пересказа сейчас и его повторное попадание в будущую историю. | Низкий, если сохранить одну строку `evidence pointer/status`; средний, если запрет заставляет модель скрыть критическое основание. | В schema оставить `hypothesis_delta` (коротко), `next_action`, args, `evidence_refs`; запретить free-text recap. Сравнить решение/ошибки с baseline. | Отдельной controlled publication не найдено. |
| **Нестандартные более компактные форматы вместо JSON (TOON/TRON)** | В input-only agent benchmarks: TOON −2…−18% total tokens, TRON 0…−27%; максимум model-dependent [8]. | Средний–высокий. Full output compression может вызвать parse cascade и дополнительные итерации, уничтожая выигрыш. | Не включать в baseline. Сначала оставлять output native JSON, затем отдельная абляция только input serialization на локальной модели. | Accuracy менялась от +3 до −14 pp для TRON и −1…−9 pp для TOON; в multi-turn parsing cascade увеличивал tokens [8]. |

## 3. Где именно сжатие ломает решаемость

Потеря качества — не абстрактная «семантическая неточность». В tool loop она чаще проявляется как неверный tool name, argument, пропуск precondition, нераспознанная ошибка, повтор уже выполненной разведки или неверное завершение. В benchmark по сжатию static agent-control contexts unmodified LLMLingua при 25% retained context дал **100% failures action parsing**; при 50% почти половина failures была parsing, ещё 41,1% — tool execution [3].

> **Критический случай для текущего `head + tail`.** Полный лог может содержать нормальный startup в начале и итоговый exit code в конце, но единственное имя failing test, путь к файлу или строку `ValueError` — в середине. Модель получает правдоподобный, но неполный мир и делает уверенный неверный шаг. Именно поэтому TACO сохраняет error/exception outputs, а CoACT отклоняет сжатую observation, если та меняет следующий action [1] [2].

Повторная суммаризация опасна даже тогда, когда старые факты можно повторно запросить. В AppWorld summary хуже сохраняла **локальную позицию в траектории**: ухудшалось корректное завершение, росли blocked actions и replay/refetch [5]. Поэтому «summary + последнее raw action/observation» — минимальный безопасный кандидат, а не «summary вместо всего».

## 4. Ранжированный порядок внедрения

| Приоритет | Что сделать локально, без сети и внешних сервисов | Почему сейчас |
|---:|---|---|
| 0 | **Заморозить baseline и телеметрию.** Для каждого вызова сохранить `usage.prompt_tokens`, `usage.completion_tokens`, raw tool bytes, visible-tool bytes, hashes, step, action, verifier, parse/retry reason. | Иначе нельзя отличить реальную экономию от новых повторов/падения solvability. Не оценивать tokens, если endpoint не прислал usage. |
| 1 | **Включить exact dedup только одинаковых records** перед сборкой контекста; сохранить первый экземпляр и raw artifact. | Это наиболее близко к lossless преобразованию. Не требует модели, сервиса или сети. Экономия зависит от фактической повторяемости, поэтому сначала измерить. |
| 2 | **Сделать raw tool artifact + адресное чтение**, а затем заменить универсальный byte head-tail на typed observation с критическими строками и явной меткой truncation. | Устраняет главную потерю информации, оставаясь совместимым с compact context. Продвигать только после E1 ниже. |
| 3 | **Сделать ответ модели action-first и schema-validated:** максимум одна action, компактный `hypothesis_delta`, `evidence_refs`; не позволять пересказ stdout. | Сокращение доказуемо телеметрией, schema уже соответствует архитектуре. Но не обещать published процент и не обнулять reasoning без абляции. |
| 4 | **Кодом enforce всё детерминируемое**, а не повторять это прозой: deadline, max steps, retry policy, schema, repeat guard. Системный control prompt сокращать только за счёт удалённого в код дублирования. | Это снижает статику без изменения model-visible tool contract. Не переписывать агрессивно tool descriptions, output protocol и recovery clauses. |
| 5 | **Сначала подготовить, затем включать prefix/KV cache.** Сделать system prefix, tool schemas и chat template byte/token-stable. | Это улучшает physical prefill и latency, но обычно не уменьшает логические `prompt_tokens`, от которых может зависеть ранжирование. |
| 6 | **Только после успешных пунктов 1–4:** протестировать один summary checkpoint *или* одно sliding window. | У этих методов есть сильные эффекты на long-horizon benchmarks, но нет нулевого риска. Нельзя выпускать оба одновременно. |
| 7 | **Не внедрять без отдельного доказательства:** learned token pruning, custom output notation, кодовую minification, multi-pass LLM summarizer. | Их token wins не компенсируют доказанные parsing/solvability риски при вашем лексикографическом objective. |

## 5. Список «не делать» до отдельной положительной абляции

1. **Не считать 4k head + 2k tail безопасной политикой.** Это byte ceiling без tokenizer-обоснования и без проверки средней части лога.
2. **Не сжимать static control prompt ниже 75% retained content.** Уже на 75% есть небольшая регрессия; на 50–35% success рушится нелинейно [3]. Даже 75% не проходит ваш строгий zero-regression критерий без собственной проверки.
3. **Не выкидывать последний raw action/observation при compaction.** Повторная summary подрывает execution state; summary не равна истории [5].
4. **Не подменять native JSON/function call нестандартным TOON/TRON для output.** Parsing cascade способен добавить шаги и токены [8].
5. **Не minify исходный код только потому, что преобразование сохраняет runtime semantics.** На SWE-bench input −42% сопровождался −12 pp resolution rate [14]. Читаемость — часть сигнала для агента.
6. **Не делать fuzzy/semantic dedup logs, paths, stack traces или близких строк.** Безопасный вариант — только byte-exact record dedup; один символ может быть номером версии, тестом или аргументом.
7. **Не задавать всем ходам очень маленький статический reasoning budget.** TALE наблюдает token elasticity: слишком малое требование иногда порождает больше фактического текста; малая модель может потерять выигрыш CoT [9] [10].
8. **Не считать prompt cache экономией ranking tokens без контракта оценщика.** Это ускорение вычисления, а не обязательно снижение `usage.prompt_tokens`.

## 6. Prompt/KV cache на локальных серверах

**Механика.** vLLM Automatic Prefix Caching сохраняет KV для уже обработанного префикса и пропускает prefill shared prefix следующего запроса. Он помогает только если префикс совпадает и не ускоряет генерацию новых токенов [15]. SGLang RadixAttention использует тот же принцип с radix tree по token prefixes; его published cache-hit rate определён как `cached prompt tokens / prompt tokens` [17].

**Что считать.** Локальный cache не меняет того, что клиент отправляет полный логический prompt. В llama.cpp response содержит стандартный OpenAI `usage` и отдельный `timings.cache_n` — число prompt tokens, переиспользованных из cache [16]. Следовательно, следует вести две независимые метрики:

| Метрика | Формула и назначение | Использовать для |
|---|---|---|
| `logical_input_tokens` | `Σ usage.prompt_tokens` | Ранжирование/контракт оценщика, если он говорит «input tokens» и не оговаривает cache discount. |
| `logical_output_tokens` | `Σ usage.completion_tokens` | Второй канал экономии; tool-call payload считать как completion. |
| `fresh_prefill_tokens` | `Σ (prompt_tokens − cached_tokens)` либо `Σ (prompt_tokens − cache_n)`, **только если backend явно сообщил поле** | Локальная latency/compute диагностика. |
| `cache_hit_ratio` | `Σ cached_tokens / Σ prompt_tokens` | Проверка стабильности префикса и эффективности cache. |

У vLLM compatibility API cache detail может вообще не быть в response; у разных версий SGLang он тоже server-specific. Поэтому нельзя вычитать «кэшированные» tokens из input ranking без формального правила данного benchmark. Если `usage` отсутствует, оставлять токены **незамеренными**, как уже зафиксировано в `decisions.md`; не подставлять оценку.

**Практический эффект для вашей архитектуры.** Поместить неизменный system prompt и неизменные tool schemas в начало; не вставлять timestamp, growing counters и динамический summary перед ними; не пересобирать/переставлять весь старый context на каждом ходе. Observation compression «на входе» сохраняет prefix лучше, чем ретроспективная перепись истории. Но при строгом рейтинге по суммарным logical tokens эта оптимизация — средство latency/throughput, не первая оптимизация score.

## 7. Аудит `limits.py`

### Детерминированная арифметика текущих констант

`TOOL_OUTPUT_HEAD_BYTES + TOOL_OUTPUT_TAIL_BYTES = 6 000` bytes. Если каждый такой result остаётся в history, перед 30-м действием накопленная верхняя граница **только предыдущих** observations равна `6000 × (1 + … + 29) = 2 610 000` bytes. Это намеренно не переведено в токены: корректный перевод зависит от tokenizer целевой модели.

Уже заданная оценка резидентного system prompt даёт `1100–1500 × 30 = 33 000–45 000` logical input tokens. При этом `LLM_MAX_OUTPUT_TOKENS = 2048` разрешает до `2048 × 30 = 61 440` output tokens. Это верхняя граница, а не прогноз, но она показывает, что одна только настройка cap не выражает token-sensitive policy.

| Значение в `limits.py` | Вердикт | Обоснование и точная коррекция |
|---|---|---|
| `TOOL_OUTPUT_HEAD_BYTES=4000`, `TOOL_OUTPUT_TAIL_BYTES=2000` | **Неудачно как универсальное правило.** | Выбрано в bytes, не tokens; не имеет внешней end-to-end валидации; удаляет середину, где часто лежит critical error evidence. Заменить policy, а не угадать новые числа: raw artifact, line-aware typed preview, errors/stderr без сжатия, `path/range/hash`, on-demand read. Новый лимит выбирать только после E1. |
| `LLM_MAX_OUTPUT_TOKENS=2048` | **Неподтверждённый placeholder, не безопасный default.** | TALE/SEER не подтверждают именно 2048 для малой local model и tool call. Слишком высокий cap допускает дорогой prose/loop; слишком низкий может лишить planning. Разделить action schema и optional reasoning/recovery; выбрать cap по p95 успешных baseline-responses и проверить parser/verifier. |
| `MAX_STEPS=30` | **Гипотеза, не data-selected лимит.** | Нет published универсального optimum. Нужна survival curve «cumulative solved vs steps» на public contract set и проверка, не теряются ли baseline successes после шага 30. |
| `MAX_RECOVERY_PASSES=1` | **Гипотеза.** | Источник в комментарии — shape чужого fact-check loop, не сопоставимое доказательство. Сравнить 0 vs verifier-triggered 1; не увеличивать без прироста solved. |
| `VERIFY_RESERVE_FRACTION=0.20`, `MIN=30`, `MAX=120` | **Непроверенный operational choice.** | На 120-s task reserve оказывается 30 s; это внутренне последовательно, но ни одна найденная работа не выбирает такой optimum. Логировать фактические verifier+cleanup durations, затем установить p95 плюс запас. |
| `LLM_MAX_ATTEMPTS=6`, backoff `(1,3)` | **Непроверенный и потенциально конфликтующий с short task.** | Шесть requests могут сжечь time и tokens без нового evidence. Каждый retry/fallback обязан быть ограничен `remaining_time − verify_reserve`, логировать отдельные attempts и не маскировать их как один call. |
| `LLM_TIMEOUT_S=120` при `DEFAULT_TASK_TIMEOUT_S=600`, `EARLY_DELIVERABLE_DEADLINE_S=90` | **Проблема связности, не само число.** | Tool timeout уже ограничен remaining reserve, LLM timeout в комментарии — нет. Один LLM call может пересечь 90-s early deadline short task. Ограничить LLM timeout тем же `min(configured, remaining − reserve)` правилом; число 120 не заменять произвольным. |
| `DEFAULT_TASK_TIMEOUT=600`, `EARLY_DELIVERABLE=90`, `TOOL_TIMEOUT=120` | **Не оценивать как token-оптимизацию.** | Эти значения мотивированы контрактом public tasks, а не внешним token evidence. Их менять только по offline target-image baseline и telemetry; именно policy linkage важнее нового «магического» числа. |

Не являются предметом этой token-проверки и не требуют изменения: контрактные `MAX_SUBMISSION_BYTES`, workspace/env names, старт `native_tools`, schema validation и политика «usage нет → tokens незамерены».

## 8. План из пяти контролируемых абляций

Сначала нужен один **замороженный baseline B**: текущая модель, tokenizer/template, `temperature=0`, task order, container reset, deadline, verifier, max steps и native tool protocol. Каждая абляция меняет только один **фактор политики** относительно B. Для cache-исследования либо выключить cache везде, либо одинаково prewarm/использовать одинаковый порядок; не смешивать logical-token comparison с cache warmness.

**Общее acceptance rule.** Продвигать treatment только если: (a) не потеряна ни одна задача, решённая B; (b) solved count не ниже B; (c) `Σ logical_input + Σ logical_output` ниже B; (d) нет роста parser errors, wrong tool arguments, verifier failures или repeated exploration. На 15 задачах нельзя честно доказать нулевой риск статистически; это строгий engineering gate, а не p-value.

| Эксперимент | Один меняемый фактор | B и treatment | Главные метрики и diagnosis | Решение |
|---|---|---|---|---|
| **E1. Observation rendering safety** | Политика отображения результата инструмента; три заранее зафиксированных уровня, всё остальное одинаково. | `raw full` → текущий `head 4k + tail 2k` → typed critical preview + raw artifact/re-read. | Solved; каждый неуспешный verifier; наличие нужной строки в visible context; first divergent next action; recovery reads; input tokens. | Если H+T теряет хотя бы baseline-solved task — удалить его. Typed policy принимать только если она возвращает solved B и даёт меньшие tokens. |
| **E2. Exact dedup** | `dedup_exact_records: false → true`. | В B остаётся выбранная по E1 observation policy; treatment удаляет лишь byte-identical records, сохраняет первый record, provenance и order. | Duplicates removed, token reduction, actions, verifier, raw hash audit. | Почти безопасный кандидат; откатить, если изменился любой action/result на baseline solved tasks. |
| **E3. Output contract** | Формат model response, при неизменном `LLM_MAX_OUTPUT_TOKENS=2048`. | Текущий ответ → native typed action с `hypothesis_delta`, `evidence_refs`, one tool call и без observation recap. Все timeout, max tokens, tools и history остаются как в B. | Completion tokens; parse/schema validity; args correctness; loops/retries; solved. | Принять только при нулевой потере B-solves и меньших total logical tokens. Если tool arguments ухудшились, оставить schema validation, но вернуть пространство для краткого plan. |
| **E4. Completion budget** | Только `LLM_MAX_OUTPUT_TOKENS`. | Лучший вариант E3 (либо B, если E3 не прошёл) с текущим 2048 → заранее зафиксированным меньшим cap; response schema, prompt, tools и history неизменны. | Completion tokens; finish reason `length`; parser/schema validity; incomplete args; verifier; solved. Построить success-vs-cap curve. | Выбрать наименьший cap, который не теряет B-solves; при первом lost solve вернуть предыдущий уровень. Не использовать единый «разумный» budget из чужой статьи. |
| **E5. One-shot history summary** | History policy: full history → один structured summary checkpoint выше заранее зафиксированного token threshold. | Последняя raw `action+observation` всегда остаётся verbatim; summary generated локальной моделью, его tokens входят в total. Все output limits фиксируются на уровне после E4. | Solved; summary calls/tokens; first post-compaction action; blocked/repeated actions; termination format; peak and total input. | Принять лишь если экономия включает cost summary и нет loss/replay. Не повторять summary в этом эксперименте. Скользящее окно оставить следующей, шестой абляцией против прошедшего E5, а не смешивать с ним. |

Эти эксперименты сознательно **не** включают custom serialization, learned token pruning и code minification: это не ранние оптимизации при вашем objective. **Скользящее окно** ставится шестой отдельной абляцией против прошедшего E5; сжатие static system/tool-control prompt — только после него, если E1–E5 оставили существенную долю resident input и public set полностью стабилен.

## References

[1] [Ren et al., *A Self-Evolving Framework for Efficient Terminal Agents via Observational Context Compression*, arXiv:2604.19572v3 (2026)](https://arxiv.org/html/2604.19572v3)

[2] [Chen et al., *CoACT: Action-Preserving Observation Compression for Coding Agents*, arXiv:2607.02911 (2026)](https://arxiv.org/html/2607.02911)

[3] [Hou & Yang, *Control Under Compression: Reliability Frontiers for Tool-Using Agents*, arXiv:2608.01056 (2026), preprint](https://arxiv.org/html/2608.01056)

[4] [Kang et al., *Acon: Optimizing Context Compression for Long-horizon LLM Agents*, ICML 2026 / arXiv:2510.00615](https://arxiv.org/html/2510.00615v1)

[5] [Min et al., *Toward Reliable Context Compression for Long-Horizon Agents*, arXiv:2608.06503 (2026), preprint](https://arxiv.org/html/2608.06503)

[6] [Jiang et al., *LLMLingua*, EMNLP 2023](https://arxiv.org/html/2310.05736)

[7] [Jiang et al., *LongLLMLingua*, 2024](https://arxiv.org/html/2310.06839)

[8] [Kutschka & Geiger, *Notation Matters: A Benchmark Study of Token-Optimized Formats in Agentic AI Systems*, arXiv:2605.29676 (2026), preprint](https://arxiv.org/html/2605.29676v1)

[9] [Han et al., *Token-Budget-Aware LLM Reasoning*, Findings of ACL 2025](https://aclanthology.org/2025.findings-acl.1274/)

[10] [Huang et al., *Reasoning Efficiently Through Adaptive Chain-of-Thought Compression*, arXiv:2509.14093v2 (2026), preprint](https://arxiv.org/html/2509.14093v2)

[11] [Zhou et al., *Filesystem-Based Memory for LLM Agents*, arXiv:2607.26637 (2026), preprint](https://arxiv.org/html/2607.26637v1)

[12] [Schelpe, *Byte-Exact Deduplication in Retrieval-Augmented Generation*, arXiv:2605.09611 (2026), preprint; and companion systems report](https://arxiv.org/html/2605.09611)

[13] [Song et al., *Empirical Study for Structured Output Control in LLMs for Software Engineering*, arXiv:2606.09395 (2026), preprint](https://arxiv.org/html/2606.09395v1)

[14] [Hrubec & Cito, *Reducing Token Usage of State-in-Context Agents using Minification*, arXiv:2606.01326 (2026), preprint](https://arxiv.org/html/2606.01326v1)

[15] [vLLM documentation, *Automatic Prefix Caching*](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/)

[16] [llama.cpp documentation, *API server — Timings and usage*](https://llama.app/docs/api)

[17] [Zheng et al., *SGLang: Efficient Execution of Structured Language Model Programs*, 2023](https://arxiv.org/abs/2312.07104)

---

**Статус достоверности.** [9] — рецензируемая conference publication; [1]–[8], [10]–[14] преимущественно свежие preprints и должны рассматриваться как пригодные для гипотез/абляций, не как универсальные константы. Документация [15]–[16] — первичные источники интерфейсной семантики серверов.

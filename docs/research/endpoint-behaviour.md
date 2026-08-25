# Локальные OpenAI-совместимые endpoint: совместимость tools, JSON и клиентской лестницы

**Дата проверки:** 25 августа 2026 г.  
**Область:** `POST /v1/chat/completions` у vLLM, SGLang, llama.cpp server, Ollama, Hugging Face Text Generation Inference (TGI) и LM Studio. Облачный OpenAI API не использовался как источник утверждений о поведении локальных серверов.

## Метод и границы достоверности

Поддержка ниже означает, что она подтверждена **документацией конкретного сервера либо его issue-трекером**. Она не означает, что любая модель 7B–32B корректно выполнит задачу: у всех исследованных servers tool calling зависит также от модели, chat template и parser. Если источник не подтверждает точную форму параметра, вердикт указан как **«не проверено»**, а не экстраполирован из общего заявления о совместимости.

> Под «частотой упоминания» в разделе фикстур понимается число независимых первичных источников в этой ограниченной выборке, которые сообщают именно о данной категории. Это **не** оценка вероятности сбоя в production.

## 1. Матрица совместимости

| Сервер | `tools` / `tool_choice` | `response_format: json_schema` | `json_object` | `usage` | Известные подвохи |
|---|---|---|---|---|---|
| **vLLM** | **Да.** Named, `auto`, `required`, `none`; automatic mode требует корректных parser/template. При `auto` без строгих условий parser может извлекать raw text, и arguments могут оказаться malformed. [1] | **Да.** Документирован современный nested-формат `response_format.json_schema`. [2] | **Да, с оговоркой.** Реальный issue использует этот тип, но фиксирует конфигурационный сбой, отключающий constraint. [3] | **Да** в наблюдаемом non-stream ответе (`prompt_tokens`, `completion_tokens`, `total_tokens`). [4] Условия выдачи usage в streaming по данному корпусу отдельно не проверены. | Неверный/отсутствующий tool-call parser может оставить вызов в `content` при пустом `tool_calls`. [4] В Qwen reasoning при async speculative decode `json_object` мог вернуть fenced JSON. [3] |
| **SGLang** | **Да**, но нужен соответствующий `--tool-call-parser`. Подтверждены `tool_choice="required"` и named choice через EBNF. [5] | **Да**, в OpenAI nested-форме. [6] | **Не проверено** именно для OpenAI-поля `type="json_object"`; документация подтверждает JSON Schema constraints, но не этот точный диалект. [6] | Для streaming по умолчанию usage может не включаться: `--stream-response-default-include-usage` default `False`. [7] Non-stream точная гарантия в этой выборке не проверена. | Parser зависит от model family. В stream неизвестное имя tool могло очистить буфер и испортить соседние calls. [8] |
| **llama.cpp server** | **Да.** Документация заявляет OpenAI-style tools/tool_choice: native formats для обученных моделей и generic fallback для остальных. [9] | **Да, но с другим shape:** пример использует `response_format={"type":"json_schema", "schema": ...}`. [9] | **Да.** `{"type":"json_object"}` описан как free-form JSON. [9] | **Да.** Заявлен стандартный OpenAI `usage` плюс `timings`, в том числе `cache_n`. [9] | Неполные JSON-arguments могут привести к **HTTP 500**, а не к 200 с кривым `tool_calls`. [10] |
| **Ollama** | `tools` — **да**; `tool_choice` — **нет** по официальной таблице совместимости. [11] | Schema constraints через OpenAI-compatible `response_format` заявлены, но точная вложенная форма на странице не показана. [12] | **JSON mode да**; exact JSON-object payload для wrapper отдельно не показан. [11] | Для native API есть `prompt_eval_count`/`eval_count`; в native stream они в финальном chunk. [13] В OpenAI-wrapper таблица поддерживает `stream_options.include_usage`, но exact non-stream `usage` в этой выборке не показан. [11] | Локальный endpoint keyless. Поддержанный `tools` не делает supported `tool_choice`; forced native rung клиента несовместим уже на уровне параметра. [11] [14] |
| **TGI** | **Да** как минимум с TGI 1.4.3. Семантика не равна OpenAI: `tool_choice="auto"` всегда выбирает tool; forced example использует строку имени. [15] | **Зависит от версии.** До merge #3046 TGI не принимал OpenAI `json_schema`; issue закрыт как completed, поэтому современную версию нельзя объявить несовместимой без version probe. [16] | В историческом контракте `json_object`/`json` требовали schema в поле `value`. [16] | **Да** в официальном примере: `prompt_tokens`, `completion_tokens`, `total_tokens`. [15] | Показанный tool response имеет `tool_calls` объектом и arguments в `function.parameters`-объекте, не list + string `function.arguments`. [15] |
| **LM Studio** | `tools` — **да**; сервер парсит model text в OpenAI response. Точный статус `tool_choice` текущая документация в этой выборке не подтверждает. [17] | **Да**, документирован OpenAI nested-формат. [18] | **Не проверено** в доступной официальной документации. | **Не проверено**: краткий reference Chat Completions не гарантирует shape `usage`. | Если parser не смог распознать call, LM Studio возвращает попытку в `message.content`, а не `tool_calls`; документация приводит malformed пример на Qwen2.5. [17] |

### Аутентификация локального endpoint

Для vLLM Bearer-проверка `/v1` включается только с `--api-key` или `VLLM_API_KEY`; SGLang документирует `--api-key` с default `None`; llama.cpp требует Bearer только после запуска с `--api-key`. [19] [7] [9] LM Studio по умолчанию не требует auth, и локальный Ollama на `localhost:11434` также keyless. [20] [14] Официальные TGI-примеры используют заполнитель `api_key="-"`. [21]

Следовательно, **пустой API key не является универсальной причиной не вызывать локальный endpoint**. Он может быть настоящей ошибкой лишь на явно защищённой инсталляции либо за reverse proxy.

## 2. HTTP-коды: несовместимый параметр против временной нагрузки

Ни один из шести серверов не даёт универсального правила «любой 4xx = неподдержанный параметр». Наоборот, доступные источники показывают, что один и тот же диапазон 4xx содержит независимые классы причин. Поэтому различать их можно только по **статусу, телу и known deployment state**, а не по одному `status >= 400`.

| Наблюдение | Подтверждённый смысл | Следствие для трактовки клиента |
|---|---|---|
| Ollama `400` | Bad request: missing parameters, invalid JSON и подобные ошибки; JSON-тело содержит ключ `error`. [22] | Это может быть несовместимый shape, но не обязательно: тело должно явно указывать на поле/параметр, иначе downgrade не доказан. |
| Ollama `404` | Model not found. [22] | Это конфигурация модели, а не incompatibility `tools`/`response_format`. |
| Ollama `429` | Rate limit exceeded. [22] | Это временное давление; переход по лестнице форматов не устранит rate limit. |
| Ollama `500` / `502` | Internal error / upstream cloud model unreachable. [22] | Временная или инфраструктурная ошибка; shape запроса не установлен как причина. |
| llama.cpp `401` | Документация показывает OpenAI-style `authentication_error` при неверном ключе. [9] | Ошибка credentials, не повод переходить от native tools к text. |
| llama.cpp `503` на `/health` | Server loading. [9] | Подтверждённый пример временной неготовности; при этом пользовательский `/chat/completions` status для данного периода в документации не обещан. |
| llama.cpp `500` | В issue зафиксирован `server_error` при server-side parse incomplete tool arguments. [10] | Сам 500 может быть детерминированным protocol/parser failure, а не outage. Повтор того же запроса при `temperature=0` не имеет гарантированной пользы. |

**Строгое ограничение исследования:** конкретный HTTP-код именно для «unsupported `tool_choice`» у Ollama и для каждого unsupported `response_format` у остальных серверов документацией из этой выборки не зафиксирован. Поэтому утверждать «это всегда 400» было бы недостоверно. Для неизвестного сервера корректный факт — **классифицировать нельзя по диапазону 4xx alone**.

## 3. Документированные дефекты выдачи и минимальные фикстуры

| Категория | Число первичных источников в выборке | Минимальная фикстура | Что подтверждено |
|---|---:|---|---|
| JSON обрамлён Markdown fence | 1 | ````text\n```json\n{"answer":"ok"}\n```\n```` | vLLM #43388: при Qwen reasoning + async speculative decode `json_object` constraint мог не включиться, и content начинался с ` ```json `. [3] |
| Tool call остаётся в `content` вместо `tool_calls` | 2 | `{"tool_calls":[],"content":"<tool_call>{\"name\":\"emit_action\",\"arguments\":{\"x\":1}}</tool_call>"}` | vLLM #17161 и LM Studio docs подтверждают эту форму как parser/model failure. [4] [17] |
| Неполный / усечённый JSON arguments | 2 | `{"city":"San Francisco` | llama.cpp #22072 показал 500 с missing closing quote; vLLM #37184 описывает length/buffer-related failures. [10] [23] |
| Несколько JSON-объектов склеены в одну arguments string | 1 | `{"city":"Tokyo"}{"tz":"JST"}` | SGLang #34677 фиксирует именно эту неразбираемую строку после parser corruption в stream. [8] |
| Повтор одного tool call | 1 | Два подряд одинаковых `{"name":"messenger","arguments":{"name":"A","content":"B"}}` | vLLM #30372 описывает infinite repeated tool-call loop. [24] |
| Вымышленное/непредложенное имя tool | 1 | `{"name":"get_secret_time","arguments":{}}` при отсутствии такого tool | SGLang #34677 документирует model call к undefined function и последствия parser-а для batch. [8] |
| Пустые arguments / шум между markers | 1 | `{"name":"emit_action","arguments":""}` | В сценариях воспроизведения vLLM #37184 перечислены empty arguments и noise/hallucinations между markers. [23] |
| Валидный JSON, но искажённые имена keys | 1 | `{"neme":"Ada","contennt":"hi"}` вместо `name`/`content` | vLLM #30372 описывает mutation argument names при сохранении валидного JSON. [24] |
| Одинарные кавычки | 0 | `{'answer':'ok'}` | **Не подтверждено** первичным источником в этой выборке. Это допустимая негативная parser fixture, но не маркируется как документированный local-server defect. |
| Trailing comma | 0 | `{"answer":"ok",}` | **Не подтверждено** первичным источником в этой выборке. То же ограничение. |

`_salvage_json()` в приложенном коде может извлечь объект из prose/fence только если найдёт парные фигурные скобки и JSON внутри уже строго валиден. Он не покрывает одинарные кавычки, trailing comma, concatenated objects и truncation. Это особенно существенно потому, что часть документированных ошибок — не «лишний текст», а **семантически или синтаксически другой контракт**.

## 4. Рекомендация по стартовому уровню при неизвестном сервере

**Без предварительного измерения наиболее безопасный default — `TEXT`, а не `NATIVE_TOOLS`, `JSON_SCHEMA` или `JSON_OBJECT`.** Это рекомендация о совместимости protocol shape, не утверждение о лучшем качестве ответов модели.

`TEXT` — единственный rung приложенной лестницы, который не отправляет extension-поля `tools`, `tool_choice` или `response_format`. На неизвестной комбинации server + version + model + template он избегает как минимум следующих подтверждённых диалектных рисков: неподдерживаемого Ollama `tool_choice`, иного TGI tool result shape, historical TGI `response_format` dialect и отличающегося llama.cpp JSON-schema shape. [11] [15] [16] [9]

Цена этого выбора — более слабая гарантия структуры: маленькая модель всё ещё может добавить prose, fence или сгенерировать дефектный JSON. Поэтому `TEXT` безопаснее **для переносимости**, но не для schema adherence. Для конкретного evaluation endpoint богатый стартовый rung можно выбирать только после отдельного recorded probe на **том же model id, server version, parser/template и request shape**. Один тест вызова не переносится на другую модель или конфигурацию parser.

## 5. Расхождения приложенного кода с реальным поведением

| Наш код | Реальность | Последствие |
|---|---|---|
| `LlmSettings.missing()` запрещает любой вызов без непустого key; probe наследует то же правило. | vLLM/SGLang/llama.cpp разрешают keyless запуск без auth flag, LM Studio и локальный Ollama keyless by default, TGI применяет placeholder. [19] [20] [14] [21] | Обычный локальный endpoint блокируется **до первого HTTP-запроса**; невозможно даже измерить поддержку rung. |
| Native rung всегда шлёт OpenAI named-object `tool_choice`. | Ollama явно не поддерживает `tool_choice`; TGI documented forced choice — строка имени. [11] [15] | На этих endpoints native rung не является нейтральной проверкой модели: он ломается уже на dialect request. |
| Parser принимает только `tool_calls: list` и `function.arguments: str`. | TGI example показывает `tool_calls` object и `function.parameters` object. [15] | Успешный TGI tool response классифицируется как unusable, затем формат безосновательно понижается. |
| JSON-schema rung отправляет nested `json_schema`. | llama.cpp docs показывают top-level `schema`; старый TGI нуждался в `json_object.value`. [9] [16] | Приложенный JSON-schema rung имеет подтверждённый request-shape mismatch как минимум с llama.cpp docs и historical TGI. |
| На **каждый** 4xx с schema клиент понижает rung. | 401 может означать invalid key, 404 — missing model, 429 — rate limit. [9] [22] | Credentials, model config и overload ошибочно превращаются в permanent protocol downgrade. |
| На **каждый** 5xx client повторяет тот же body/style. | llama.cpp может выдать 500 на incomplete tool args; это parser/protocol failure, не обязательно transient network fault. [10] | Нарушается собственное правило «не повторять уровень при детерминизме»: расходуются время/токены, а fallback не запускается. |
| Берётся только `calls[0]`; остальные calls молча игнорируются. | vLLM `required` допускает one or more calls; LM Studio также описывает one or more. [1] [17] | Несколько calls, конфликт или повтор не становятся наблюдаемым дефектом; теряется часть model intent. |
| Успех определяется `json.loads`, schema validation отсутствует. | vLLM #30372: valid JSON может иметь искажённые argument keys. [24] | Валидный JSON, не соответствующий заявленной schema, принимается как structured success. |
| Нет `tool_calls` → native reply unusable. | vLLM и LM Studio могут оставить tool call в `content`. [4] [17] | Клиент не отличает parser extraction failure от отсутствия намерения вызвать action и теряет диагностику. |

## Вывод

Лестница правильно исходит из того, что «OpenAI-compatible» — не единый контракт. Однако внешние факты показывают два системных разрыва: **keyless local defaults** и **семантическая неоднородность tools/response_format/ошибок**. Самая рискованная текущая эвристика — понижать rung на любом 4xx и повторять любой 5xx. Она смешивает несовместимость параметра, неверный ключ, отсутствующую модель, rate limit и server-side parser defect.

Для неизвестного endpoint совместимый старт — `TEXT`; для известного — только результат version/model/template-specific probe. В теле successful response отсутствие `usage` разумно трактуется как **неизмеренные токены**, а не как повод оценивать их; это решение приложения согласуется с неполной и различающейся документацией wrapper-ов.

## References

[1]: https://docs.vllm.ai/en/stable/features/tool_calling/ "vLLM — Tool Calling"
[2]: https://docs.vllm.ai/en/stable/features/structured_outputs/ "vLLM — Structured Outputs"
[3]: https://github.com/vllm-project/vllm/issues/43388 "vLLM #43388 — json_object not enforced after Qwen thinking"
[4]: https://github.com/vllm-project/vllm/issues/17161 "vLLM #17161 — tool_calls empty while content contains the call"
[5]: https://docs.sglang.io/docs/advanced_features/tool_parser "SGLang — Tool Parser"
[6]: https://docs.sglang.io/docs/advanced_features/structured_outputs "SGLang — Structured Outputs"
[7]: https://docs.sglang.io/docs/advanced_features/server_arguments "SGLang — Server Arguments"
[8]: https://github.com/sgl-project/sglang/issues/34677 "SGLang #34677 — unknown tool name corrupts streaming batch"
[9]: https://llama.app/docs/api "llama.cpp — API server"
[10]: https://github.com/ggml-org/llama.cpp/issues/22072 "llama.cpp #22072 — malformed/incomplete tool arguments"
[11]: https://docs.ollama.com/api/openai-compatibility "Ollama — OpenAI compatibility"
[12]: https://docs.ollama.com/capabilities/structured-outputs "Ollama — Structured Outputs"
[13]: https://docs.ollama.com/api/usage "Ollama — Usage"
[14]: https://docs.ollama.com/api/authentication "Ollama — Authentication"
[15]: https://huggingface.co/docs/text-generation-inference/en/guidance "TGI — Guidance: JSON and tools"
[16]: https://github.com/huggingface/text-generation-inference/issues/3058 "TGI #3058 — response_format incompatibility"
[17]: https://lmstudio.ai/docs/developer/openai-compat/tools "LM Studio — Tool Use"
[18]: https://lmstudio.ai/docs/developer/openai-compat/structured-output "LM Studio — Structured Output"
[19]: https://docs.vllm.ai/en/stable/usage/security/ "vLLM — API key authentication limitations"
[20]: https://lmstudio.ai/docs/developer/core/authentication "LM Studio — Authentication"
[21]: https://huggingface.co/docs/text-generation-inference/en/messages_api "TGI — Messages API"
[22]: https://docs.ollama.com/api/errors "Ollama — Errors"
[23]: https://github.com/vllm-project/vllm/issues/37184 "vLLM #37184 — Kimi-K2.5 tool parser issues"
[24]: https://github.com/vllm-project/vllm/issues/30372 "vLLM #30372 — distorted arguments and tool-call loop"

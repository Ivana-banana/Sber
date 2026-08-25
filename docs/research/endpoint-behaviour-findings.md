# Подтверждённые находки по локальным API

## vLLM

Источник: https://docs.vllm.ai/en/stable/features/tool_calling/

- Документация указывает, что vLLM поддерживает именованный вызов функций, а также `auto`, `required` (с vLLM 0.8.3) и `none` для `tool_choice` в Chat Completions API.
- Для automatic tool calling сервер нужно запускать с включённым `--enable-auto-tool-choice` и подходящим `--tool-call-parser`; примеры зависят от семейства модели и chat template.
- Страница прямо отмечает, что функция вызывает backend structured outputs для гарантии соответствия объекта аргументов JSON Schema из `tools`.

## SGLang

Источник: https://docs.sglang.io/docs/advanced_features/tool_parser

- Документация демонстрирует OpenAI-compatible `chat.completions` с полем `tools` и чтением `choices[0].message.tool_calls`.
- Для работы tool calls запуск должен содержать `--tool-call-parser`; документация перечисляет разные parsers и поддерживаемые ими семейства моделей. Значит, совместимость не определяется одним endpoint без конфигурации и модели.
- Документация говорит, что parser интерпретирует сгенерированный вывод. Для gpt-oss parser может отфильтровать analysis-channel события и оставить пустой `content`; инструментальный раунд следует завершать сообщениями роли `tool`.
- В streaming-примере аргументы tool call приходят фрагментами и должны объединяться клиентом.

## Предварительные вопросы для проверки

- Поддержка `response_format: json_schema` и `json_object`, поле `usage`, статус и тело ошибок при неподдержанном параметре и поведение ключа должны быть подтверждены отдельными первоисточниками для каждого сервера.
- Текущий клиент использует non-streaming, поэтому фрагментация stream не создаёт прямого расхождения, но подчеркивает зависимость native tool calling от parser/model/template.

## llama.cpp

Источник: https://llama.app/docs/api

Документация подтверждает `POST /v1/chat/completions`, нативные `tools` и `tool_choice`, при этом заявлен generic fallback для моделей, не обученных нативному формату. В `response_format` поддержаны оба рассматриваемых режима: `json_schema` со схемой в ключе `schema` и `json_object`. Ответы включают OpenAI-совместимый объект `usage`, а также специфичный объект `timings` с `cache_n`. Ключ Bearer требуется, только когда сервер запущен с `--api-key`; пример клиента явно передаёт произвольное `api_key="no-key-required"` без флага. Ошибки заявлены в OpenAI-формате; на странице приведён ответ 401 `authentication_error` для неверного ключа.

## Ollama

Источник: https://docs.ollama.com/api/openai-compatibility

В официальной таблице функций для OpenAI-compatible API отмечены Chat Completions, JSON mode, Tools и поле `response_format`; среди поддерживаемых request fields отмечены `response_format`, `max_tokens`, `tools`, `stream_options.include_usage`. В этой же таблице `tool_choice` отмечен как неподдерживаемый. Базовый пример устанавливает `api_key="ollama"` и комментирует, что ключ required by client but ignored, то есть пустой/несовпадающий Bearer-ключ для локального endpoint не служит аутентификацией в таком режиме.

## Предварительные последствия

Для Ollama запрос native-инструмента из приложенного клиента содержит неподдерживаемый `tool_choice`; эффект необходимо подтвердить в документации ошибок или issue, поэтому ещё нельзя считать его гарантированным HTTP-отказом. Для llama.cpp структуру `response_format.json_schema` важно дополнительно сверить: пример использует `schema` непосредственно, тогда как приложенный клиент кладёт её внутрь `json_schema` в стиле нового API.

## Hugging Face Text Generation Inference (TGI)

Источники: https://huggingface.co/docs/text-generation-inference/en/messages_api и https://github.com/huggingface/text-generation-inference/issues/3058

Документация Messages API подтверждает `POST /v1/chat/completions` начиная с TGI 1.4.0 и показывает non-streaming и streaming запросы. В примере OpenAI-клиент получает произвольный `api_key="-"`, что подтверждает отсутствие проверки данного ключа в обычном локальном примере. Документация не заявляет поля `tools`, `tool_choice` или `usage` на этой странице; статус здесь — «не проверено» до отдельного источника.

Issue #3058 документирует исходную несовместимость `response_format` с OpenAI: ранний TGI использовал `json` либо `json_object` c обязательным полем `value`, содержащим схему, а не `type: json_schema` с вложенным `json_schema`. Issue закрыт как completed 7 мая 2025 г., с упоминанием merge #3046; поэтому для незафиксированной версии TGI нельзя утверждать, что это расхождение остаётся, но этот факт критичен для старых развертываний и требует version-aware проверки.

## Дополнительное наблюдение по HTTP-контракту

Приложенный клиент всегда добавляет `Authorization: Bearer <key>`. В документациях TGI и Ollama примеры передают заполнители, а llama.cpp требует проверяемый Bearer только при явном серверном `--api-key`; следовательно, непустой заголовок сам по себе совместим с их локальными режимами. Однако текущая проверка `LlmSettings.missing()` запрещает любой вызов при пустом ключе, несмотря на то что все три локальные документации допускают placeholder/игнорируемый ключ.

## LM Studio

Источники: https://lmstudio.ai/docs/developer/openai-compat/structured-output и https://lmstudio.ai/docs/developer/openai-compat/tools

LM Studio документирует OpenAI-совместимый `POST /v1/chat/completions` и поддерживает `response_format.type = "json_schema"` с вложенным объектом `json_schema` (`name`, `strict`, `schema`); сформированный JSON возвращается строкой в `choices[0].message.content`. Документация предупреждает, что структурированный вывод не гарантирован для моделей менее 7B. Отдельная документация о Tools утверждает поддержку параметра `tools` через тот же endpoint и парсит текстовое представление model-specific template в `message.tool_calls`. Если не удаётся распознать корректно отформатированный вызов, LM Studio молча возвращает этот текст в `message.content`, а не в `tool_calls`. Приведён пример неправильной выдачи Qwen2.5-Instruct: `<tool_call>\n["name": "get_delivery_date", function: "date"]\n</tool_call>`.

## Уточнение для кода

Поведение LM Studio напрямую опровергает допущение `_read_reply()` о том, что отсутствие `tool_calls` при native-инструментах означает только непригодный ответ, который следует лечить понижением протокола: текст может содержать попытку tool call, но сервер не сумел её распарсить. После понижения клиент уже меняет контракт и не сохраняет эту диагностическую информацию.

## Воспроизводимые дефекты tool-call для набора фикстур

Источники: https://github.com/ggml-org/llama.cpp/issues/22072 и https://github.com/vllm-project/vllm/issues/17161

В issue llama.cpp #22072 для OpenAI-compatible `llama-server` показан ответ/сбой с неоконченными аргументами инструмента: `{"error":{"code":500,"message":"Failed to parse tool call arguments as JSON: parse error at line 1, column 27: invalid string: missing closing quote; last read: '\"San Francisco'","type":"server_error"}}`. Автор и участники относят это к незавершённой генерации JSON на длинных ответах. Это даёт фикстуру `{"city":"San Francisco` (без закрывающей кавычки и скобки) и показывает, что сбой может быть серверным HTTP 500, а не ответом 200 с некорректным телом.

В issue vLLM #17161 приведён полноформатный успешный HTTP-ответ, где `message.tool_calls` — пустой массив, однако `message.content` содержит `<tool_call>\n{"name":"get_current_weather","arguments":{"location":"Boston, MA","unit":"fahrenheit"}}\n</tool_call>`. Комментарий подтверждает, что настройка `--tool-call-parser hermes` устранила пример. Это документированный случай «вызов инструмента в content вместо tool_calls»; в таком ответе `_read_reply()` клиентского кода не сможет извлечь аргументы на native rung и понизит протокол.

## SGLang и Ollama: структурированные ответы

Источники: https://docs.sglang.io/docs/advanced_features/structured_outputs и https://docs.ollama.com/capabilities/structured-outputs

SGLang официально демонстрирует OpenAI-compatible `response_format.type="json_schema"` с вложенным `json_schema.name` и `json_schema.schema` на модели Llama 3.1 8B. Он заявляет гарантию соответствия заданному constraint и требует, чтобы в запросе был ровно один из `json_schema`, `regex`, `ebnf`; документация всё же рекомендует явно направлять модель инструкцией в prompt ради качества. Поддержка `response_format.type="json_object"` на этой странице не найдена — статус «не проверено», а не «нет».

Ollama документирует structured JSON и JSON Schema для локального native `/api/chat`: свободный JSON задаётся `format:"json"`, schema — через `format:<schema>`. Та же страница прямо говорит, что structured outputs работают через OpenAI-compatible API в поле `response_format`; одновременно Ollama Cloud не поддерживает structured outputs. Страница OpenAI compatibility ранее подтвердила JSON mode и `response_format`, но не даёт отдельного формата `json_schema`; поэтому для OpenAI-обёртки в матрице корректная формулировка: «schema — заявлено через response_format, точная вложенная форма не показана; json_object — JSON mode заявлен».

## TGI: tools и JSON constraints

Источник: https://huggingface.co/docs/text-generation-inference/en/guidance

Официальное руководство Guidance говорит, что начиная с TGI 1.4.3 поддерживаются JSON и regex grammars, а также tools/functions; оно заявляет совместимость с OpenAI client libraries. Следовательно, TGI поддерживает `tools` для Messages API как минимум с этой версии. Однако из этой документации не следует поддержка `tool_choice`, а также нельзя без версии утверждать поддержку именно OpenAI-вложенности `response_format.type="json_schema"`: issue #3058 показывает, что такой контракт менялся после 2025-05. Статус для `tool_choice` — «не проверено», для современных json_schema — «version-dependent; проверить установленную версию».

Попытка верифицировать usage через issue #1724 не дала пригодного прямого фрагмента: браузерная выгрузка не извлекла сам объект ответа. Поэтому наличие `usage` для TGI будет подтверждено отдельной OpenAPI-спецификацией либо останется «не проверено» в финальной матрице; это безопаснее, чем выводить его из поискового сниппета.

## SGLang: usage в streaming

Источник: https://docs.sglang.io/docs/advanced_features/server_arguments

В API-related аргументах SGLang задокументирован флаг `--stream-response-default-include-usage`: «Include usage in every streaming response (even when stream_options is not specified)», по умолчанию `False`. Следовательно, для streaming usage может отсутствовать по умолчанию; оператор может включить его глобально. Приложенный клиент использует non-streaming, поэтому этот режим не объясняет отсутствие usage в его успешных ответах, но важен для общей матрицы.

## HTTP-статусы: SGLang и Ollama

Источники: https://docs.sglang.io/docs/advanced_features/server_arguments и https://docs.ollama.com/api/errors

Документация Ollama отделяет `400` (missing parameters, invalid JSON) и `404` (model not found) от `429` (rate limit exceeded), `500` и `502`. Тело ошибки — JSON c ключом `error`. Для stream возникшая после начала передачи ошибка приходит объектом NDJSON с `error` и без изменения начального HTTP-статуса. Поэтому текущая ветка клиента, которая на любом `>=400` с structured request сразу понижает rung, неверно трактует как несовместимость не только 400, но и 404 и 429. 429 следует рассматривать как временное давление/ожидание, а 404 — как конфигурационную ошибку, не как сигнал перехода tools→JSON.

## Ollama и LM Studio: usage

Источники: https://docs.ollama.com/api/usage и https://lmstudio.ai/docs/developer/openai-compat/chat-completions

Ollama документирует в нативном API счётчики `prompt_eval_count` (input tokens) и `eval_count` (output tokens). В streaming эти поля приходят только в заключительном chunk с `done:true`. Для OpenAI-compatible API официальная таблица поддерживает `stream_options.include_usage`, но публичная страница не показывает, что non-streaming `/v1/chat/completions` возвращает именно OpenAI-поля `usage.prompt_tokens` и `usage.completion_tokens`. Поэтому коду следует маркировать такие токены как «не измерены», если объект `usage` отсутствует; преобразовывать нативные поля не следует без доказательства эквивалентности для конкретного endpoint.

LM Studio в краткой странице Chat Completions не документирует формат поля `usage` и перечисляет только часть признаваемых payload-параметров. Следовательно, наличие `usage` на `/v1/chat/completions` по доступному первоисточнику не проверено. Это не подтверждает отсутствие поля, но достаточное основание не полагаться на него при неизвестной сборке.

## Отсутствующий или пустой API key

Источники: https://docs.vllm.ai/en/stable/usage/security/, https://docs.sglang.io/docs/advanced_features/server_arguments и https://llama.app/docs/api

vLLM требует Bearer-код для `/v1/chat/completions` только когда сервер запущен с `--api-key` или `VLLM_API_KEY`; документация прямо связывает защиту с настроенным флагом. SGLang документирует `--api-key` со значением по умолчанию `None`, которое используется OpenAI-compatible server. llama.cpp требует Bearer ключ, только если запущен с `--api-key`. Во всех этих вариантах отсутствие ключа на стороне сервера означает, что клиенту не нужен осмысленный секрет; отправка пустого Bearer не должна быть обязательным предусловием. Текущий `LlmSettings.missing()` (строки 92–96) делает API key обязательным всегда и тем самым блокирует стандартные keyless local deployments ещё до первой попытки.

## Уточнения по tools, tool_choice и usage

Источники: https://docs.vllm.ai/en/stable/features/tool_calling/, https://docs.sglang.io/docs/advanced_features/tool_parser и https://huggingface.co/docs/text-generation-inference/en/guidance

vLLM поддерживает named, `auto`, `required` (с 0.8.3) и `none` для `tool_choice`. Для named/required документация заявляет schema-constrained decoding; но для `auto` без нужных strict-настроек вызовы извлекаются из raw text и аргументы могут быть malformed или нарушать schema. SGLang поддерживает `tool_choice="required"` и named choice через EBNF, если запущен подходящий parser. TGI Guidance показывает usage с `prompt_tokens`, `completion_tokens`, `total_tokens` в ответе инструмента; он же фиксирует несовместимость с OpenAI: `tool_choice="auto"` у TGI *всегда* выбирает инструмент, тогда как в OpenAI auto выбирает его только когда считает нужным. Это показывает, что presence поля не означает семантическую эквивалентность.

## TGI: конкретные несовместимости native-tool rung

Источник: https://huggingface.co/docs/text-generation-inference/en/guidance

TGI демонстрирует forced choice как строку `"tool_choice":"get_current_weather"`, а его опубликованный ответ использует `message.tool_calls` как объект (не массив), поле параметров называется `function.parameters` и является уже JSON-объектом, а не строкой `function.arguments`. В OpenAI-integration примере TGI также указывает, что `tool_choice="auto"` всегда выбирает tool. Приложенный `_read_reply()` принимает только список `tool_calls` и только строку `function.arguments` (строки 285–291); поэтому по документированному примеру TGI native rung неизбежно посчитает успешный вызов непригодным и понизит протокол.

## Дополнительные дефекты vLLM tool parser

Источник: https://github.com/vllm-project/vllm/issues/37184

Issue vLLM #37184 (Kimi-K2.5, vLLM 0.16.0–0.17.1 nightly) документирует потерю токенов между reasoning и tool channels, пустой `tool_calls` при `content:null`, finish_reason `length`, ограничение в 8 KiB на arguments, silent failures nested calls. В перечисленных сценариях воспроизведения фигурируют пустые arguments, разбитые между streaming chunk аргументы, специальные символы JSON и noise/hallucinations между marker-ами. Это один источник, не оценка распространённости; его стоит включить как version/model-specific fixture, а не обобщать на весь vLLM.

## vLLM: structured outputs

Источник: https://docs.vllm.ai/en/stable/features/structured_outputs/

vLLM документирует `response_format.type="json_schema"` с объектом `json_schema.name` и `json_schema.schema` для Chat Completions. Structured outputs включены по умолчанию, а backend выбирается автоматически или флагом сервера. Документация рекомендует добавить описание схемы и способ заполнения полей в сам prompt: это означает, что syntactic constraint не устраняет смысловые ошибки модели. Точный статус OpenAI-поля `response_format.type="json_object"` в этой странице не показан; хотя сервер поддерживает JSON structured output, в матрице это должно быть помечено «не проверено именно в этой форме», а не выведено по аналогии.

## Фикстура: JSON в Markdown fence вопреки json_object

Источник: https://github.com/vllm-project/vllm/issues/43388

В vLLM #43388 для Qwen-style reasoning model при `response_format:{"type":"json_object"}`, async scheduling и speculative decoding описан сбой, где окончание reasoning не распознаётся, JSON grammar не включается, а финальный content может начинаться с Markdown fence ` ```json `. Эту фикстуру следует считать version/configuration-specific (issue связан с последующими fix commits), но она прямо подтверждает категорию «обрамление JSON в тройные backticks» на OpenAI-compatible endpoint. Минимальная фикстура: ````json\n```json\n{"answer":"ok"}\n```\n````.

## Повторные вызовы и искажённые имена аргументов

Источник: https://github.com/vllm-project/vllm/issues/30372

Issue vLLM #30372 документирует для GPT-OSS с `--tool-call-parser openai` и `--reasoning-parser openai_gptoss` бесконечный повторный вызов messenger tool и, отдельно, валидный JSON с искажёнными именами параметров: ожидаемые `name` и `content` меняются на фонетически похожие или слегка мутировавшие. Это один version/model/language-specific report, но он подтверждает две требуемые категории фикстур: повтор одного tool call и schema-семантически неверный, но синтаксически валидный `arguments`.

## Keyless local endpoints: LM Studio и Ollama

Источники: https://lmstudio.ai/docs/developer/core/authentication и https://docs.ollama.com/api/authentication

LM Studio по умолчанию не требует аутентификации; проверка API Token включается отдельным переключателем server settings, после чего допустим только валидный token. Ollama не требует аутентификации при доступе к локальному API `http://localhost:11434`; ключи нужны для прямого API ollama.com, cloud models и некоторых облачных операций. Следовательно, обязательная проверка непустого `OPENAI_API_KEY` в приложенном клиенте расходится с документированными локальными defaults обоих серверов.

## Фикстура: неизвестный tool name повреждает остальные calls в SGLang stream

Источник: https://github.com/sgl-project/sglang/issues/34677

SGLang #34677 подтверждает, что модель может назвать не предложенный tool. В streaming parser одна такая запись очищала буфер и могла потерять/испортить соседние валидные parallel calls. Приведённый наблюдаемый результат включает `tool_choice="required"` без calls и один call `get_time` с неразбираемой строкой аргументов `{"city": "Tokyo"}{"tz": "JST"}`. Это один streaming-specific source; приложенный клиент stream не использует, но он покрывает требуемые фикстуры «вымышленное имя» и «склеенные tool arguments».

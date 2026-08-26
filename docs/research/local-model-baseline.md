# Локальный baseline для агента с неизвестным OpenAI-совместимым endpoint

**Дата проверки:** 26 августа 2026 г.  
**Цель:** получить не «лучший локальный агент», а воспроизводимый и консервативный baseline для агента, который на оценке увидит неизвестную малую модель за `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL` и `OPENAI_API_KEY`.

## Краткий вывод

Рекомендую начать с **Qwen2.5-Coder-7B-Instruct-GGUF, Q4_K_M, на llama.cpp v0.3.0**. Это реалистичный представитель доступного локального 7B-класса: файл весов занимает **4.68 GB**, модель помещается в класс **8 GB VRAM** при контексте 4K и аккуратном запасе памяти, а `llama-server --jinja` документированно понимает её **native tool-call format**. [1] [2] [3]

Этот baseline намеренно не подтверждает только «счастливый» путь. Он одновременно проверяет, что агент умеет работать с API-расхождениями: в документации llama.cpp v0.3.0 ответ на tools показан как `finish_reason: "tool"` и `tool_calls: [{"name", "arguments"}]`, а не обязательно как каноническое современное OpenAI-поле `tool_calls[].function`. Нормализация этого ответа и переход по лестнице форматов относятся к переносимым свойствам агента; качество решения конкретной модели — нет. [2]

> **Практическое решение.** Используйте Qwen2.5-Coder-7B Q4_K_M / llama.cpp как обязательный baseline на 8 GB. Если доступно 12–16 GB, добавьте Qwen2.5-Coder-14B Q4_K_M как второй пункт той же кривой. На 24 GB добавьте 32B Q4_K_M, но не заменяйте им 7B-прогон: это уже оптимистичный сценарий для неизвестной «малой» модели.

## Как читать требования к памяти

Число в колонке «файл Q4» — опубликованный размер GGUF-весов, **не** точное потребление VRAM. Для практического класса памяти ниже заложены: веса выбранной квантизации, KV-cache на контексте около 4K, рабочие буферы и примерно 1 GiB запас. В llama.cpp v0.3.0 именно 1 GiB является умолчательным `--fit-target`; фактический предел меняется с `ctx-size`, batch size, типом KV-cache, Flash Attention, offload-слоем и видеокартой. [3]

Следовательно, «8 GB» означает **практически запускаемый baseline с коротким/средним контекстом**, а не гарантию, что модель выдержит 32K контекст или высокую параллельность. Не заменяйте этот запас агрессивным Q4 KV-cache: документация llama.cpp отдельно предупреждает, что экстремальная KV-квантизация способна заметно ухудшить tool calling. [2]

## Модели по видеопамяти и форматам вызова

В таблице оценка tool-use качества является **инженерной качественной калибровкой**, а не общим бенчмарком модели. Она означает вероятность корректно выбрать инструмент, не выдумать имя и сформировать пригодные аргументы при температуре 0 и одном простом шаге. Форматы приведены для рекомендованного стека **llama.cpp v0.3.0, `llama-server --jinja`**; `JSON Schema` является server-side constrained decoding, но не доказательством OpenAI `strict:true`.

| Класс ресурсов | Модель и выбранная квантизация | Размер файла весов | Практический режим | Качество именно tool use | `tools` в llama.cpp v0.3.0 | `response_format: json_schema` | `json_object` / текст | Роль в baseline |
|---|---:|---:|---|---|---|---|---|---|
| **8 GB VRAM** | **Qwen2.5-Coder-7B-Instruct, GGUF Q4_K_M** | **4.68 GB** [4] | GPU, 4K context; первый обязательный прогон | **Среднее / рабочее.** Наиболее полезная точка для проверки выбора инструмента и исправности аргументов без оптимистичного масштаба | **Native.** Семейство Qwen2.5-Coder прямо перечислено в native handlers; необходим tool-aware Jinja template. [2] | **Да, ограничение схемой; `strict` не документирован.** [3] | **Да**; обычный текст — последняя ступень | Главная модель |
| **8 GB VRAM либо CPU** | **Llama-3.2-3B-Instruct, GGUF Q4_K_M** | **2.02 GB** [9] | GPU с большим запасом либо CPU smoke test | **Низкое–среднее.** Проверяет, что агент не зависит от сильной модели; не годится как единственный показатель | **Native** для Llama 3.2. [2] | Да, без документированного `strict` [3] | Да | Нижняя граница general-purpose модели |
| **8 GB VRAM либо CPU** | **Salesforce xLAM-2-3b-fc-r, GGUF Q4_K_M** | **1.93 GB** [8] | CPU или GPU; желательно отдельный эксперимент | **Высокая специализация на синтаксисе/композиции функций, слабее как общий агент.** Может переоценить перенос на обычную малую модель | **Generic, не native handler** в llama.cpp v0.3.0; vendor prompt/template обязателен. [2] [8] | Да, без документированного `strict` [3] | Да | Специализированная контрольная точка, не основной score |
| **12–16 GB VRAM** | **Qwen2.5-Coder-14B-Instruct, GGUF Q4_K_M** | **8.99 GB** [5] | GPU, 4K–8K при достаточном запасе | **Среднее–высокое.** Хорошая вторая точка, чтобы отделить архитектурный сбой агента от границы 7B | **Native** для Qwen2.5-Coder. [2] | Да, без документированного `strict` [3] | Да | Рекомендуемый второй прогон |
| **12–16 GB VRAM** | **Mistral-Nemo-Instruct-2407, GGUF Q4_K_M** | **7.48 GB** [7] | GPU, 4K–8K | **Среднее.** Полезная смена семейства и tool template, а не заведомо сильнее Qwen 14B | **Native** для Mistral Nemo. [2] | Да, без документированного `strict` [3] | Да | Проверка переносимости через другое семейство |
| **24 GB VRAM** | **Qwen2.5-Coder-32B-Instruct, GGUF Q4_K_M** | **19.85 GB** [6] | GPU, 4K; 24 GB — нижняя практическая граница | **Высокое внутри локального open-weight набора.** Результат полезен как верхний bound, но не как прогноз оценки | **Native** для Qwen2.5-Coder. [2] | Да, без документированного `strict` [3] | Да | Верхняя контрольная точка |
| **Только CPU** | **xLAM-2-3b-fc-r Q4_K_M** или **Llama-3.2-3B Q4_K_M** | 1.93 / 2.02 GB [8] [9] | Нужны минимум 16 GB RAM для комфортной ОС и модели; низкая скорость допустима | xLAM — специализированный синтаксис; Llama — более честный general-purpose smoke test | xLAM generic; Llama native [2] | Да, серверное ограничение [3] | Да | Только функциональная совместимость и fallback-логика |
| **Только CPU, медленно** | **Qwen2.5-Coder-7B Q4_K_M** | 4.68 GB [4] | Обычно от 16 GB RAM; лучше 32 GB, если нужен запас на контекст | Среднее, но latency уже не репрезентативна | Native [2] | Да, серверное ограничение [3] | Да | Полезно лишь если нет GPU и нужен тот же основной модельный класс |

### Явная матрица «модель × сервер» для первой ступени лестницы

Следующая таблица отвечает именно на вопрос, **на какой ступени реально должен оказаться прогон**, если вы не добавляете собственный неподтверждённый parser. Слово «forced» означает named function или `tool_choice: "required"`, где сервер сам ограничивает результат schema; это не равнозначно automatic tool selection. `JS` означает `json_schema` без доказанного OpenAI `strict:true`; `JO` — `json_object`; `T` — обычный текст.

| Семейство модели | llama.cpp v0.3.0 | vLLM 0.19.1 | Ollama v0.33.0 | Честная ожидаемая ступень |
|---|---|---|---|---|
| Qwen2.5-Coder 7B / 14B / 32B | **Native automatic tools** при `--jinja` и tool-aware template; затем JS → JO → T. [2] [3] | Для automatic tools требуется совместимый parser/template; в versioned docs его автоматический parser для Qwen2.5-Coder не подтверждён. **Forced named/required** гарантирует schema-shaped arguments, затем JS → JO → T. [10] [12] | Tools и `response_format` заявлены на endpoint, но `tool_choice` отсутствует; для raw GGUF не предполагается автоматически правильный template. Стартовать с feature probe tools, иначе JS → JO → T. [15] [17] | **llama.cpp: native tools.** На vLLM/Ollama не засчитывать auto-tools без прогона |
| Mistral Nemo Instruct | **Native automatic tools**; JS → JO → T. [2] [3] | **Native automatic tools** через Mistral parser/template; документация предупреждает о трудностях Mistral 7B с parallel calls, поэтому `parallel_tool_calls: false` разумен для одношагового агента. JS → JO → T. [10] [12] | Tools/response_format зависят от модели, template и packaging; `tool_choice` нет. JS → JO → T как fallback. [15] | llama.cpp или vLLM способны дать честный native-tools прогон |
| Llama-3.2-3B Instruct | **Native automatic tools**; JS → JO → T. [2] [3] | **Native automatic tools** с `llama3_json` parser/template; docs отмечают, что Llama 3 может неверно представить некоторые параметры и не поддерживает parallel calls. [10] | Tools/response_format endpoint есть, `tool_choice` нет; raw-GGUF template всё равно надо проверить. [15] | Хорошая короткая проверка на native-интерфейс двух серверов |
| xLAM-2-3b-fc-r | **Generic**, не native handler этой версии; vendor prompt обязателен. JS → JO → T. [2] [8] | Не считать auto-tools поддержанными без установки vendor parser plugin; forced named/required и JS доступны как server constrained path. [10] [12] | Прогон только после проверки конкретной model package/template; иначе JS → JO → T. [15] [17] | Это tool-specialised model probe, а не нейтральный API baseline |

### Что именно означает поддержка форматов

У моделей в этой таблице нет собственной реализации `response_format`: его предоставляет сервер посредством constrained decoding. Поэтому форматный результат — это пересечение **семейства модели и template/parser** для `tools` плюс **возможности сервера** для JSON. У llama.cpp v0.3.0 native handlers явно покрывают Qwen2.5-Coder, Mistral Nemo и Llama 3.2; непризнанные шаблоны идут через generic handler и могут быть менее эффективны. [2]

Для `json_schema` правильная формулировка — «сервер ограничивает генерацию схемой». Неправильная формулировка — «поддержан OpenAI strict». В version-specific документации llama.cpp указан *schema-constrained JSON response format*, но не контракт `json_schema.strict`; поэтому этот стек должен считаться ступенью **JSON Schema без подтверждённого strict-protocol**. [3]

## Сравнение серверов под Windows + WSL2

Все варианты ниже запускаются внутри Linux-дистрибутива WSL2. Версии зафиксированы, чтобы не переносить слова «OpenAI-compatible» между несовместимыми реализациями.

| Сервер и проверенная версия | `/v1/chat/completions` | Native tools и важные оговорки | `response_format` | `strict:true` | Без GPU | Сложность WSL2-установки | Когда выбирать |
|---|---|---|---|---|---|---|---|
| **llama.cpp v0.3.0** (`c1d0e7a`) | **Да.** Server README заявляет OpenAI-compatible chat completions. [2] [3] | **Да, `--jinja` обязателен.** Native handlers для Qwen2.5-Coder, Mistral Nemo, Llama 3.2; generic fallback для других шаблонов. Формат ответа в документации может быть flat `name`/`arguments`, поэтому нужен normalizer. [2] | **Schema-constrained JSON**; `json_schema` / JSON режим реализуются constrained decoding. [3] | **Не подтверждён документацией.** Не засчитывать как strict | **Да**, CPU и GPU указаны явно. [3] | **Средняя:** CMake + компилятор; для NVIDIA в WSL2 нужен CUDA Toolkit | **Рекомендованный baseline.** Наиболее прозрачный контроль GGUF, Q4 и шаблона |
| **vLLM 0.19.1** | **Да**, endpoint и OpenAI client пример задокументированы. [10] | **Да**, но для auto нужны `--enable-auto-tool-choice`, parser и подходящий chat template. Canonical `tool_calls[].function` показан в docs. В `auto` аргументы не schema-constrained. [10] | **Да:** `json_object` и `json_schema`; документирован пример с OpenAI `response_format`. [11] [12] | **Нет.** Поле принимается, но не влияет на decoding. Named/`required` tools ограничены схемой, `auto` — нет. [10] | **Да**, официальный x86 Linux CPU backend; AVX512 рекомендован, AVX2 с ограничениями. [13] | **Высокая:** Python/PyTorch, parser/template и для CPU TCMalloc/OpenMP | Второй API-semantics baseline, особенно если оценочный сервер похож на vLLM |
| **Ollama v0.33.0** (`ebf200f`) | **Да**, `/v1/chat/completions` и перечень fields опубликованы. [14] [15] | **Да**, в том числе native and parallel tool calls; однако compatibility table прямо отмечает, что `tool_choice` для Chat Completions не поддержан. [15] [16] | **Да:** JSON mode и schema enforcement; docs отдельно говорят, что structured output доступен через OpenAI `response_format`. [15] [17] | **Не документирован.** Не считать поддержанным | **Да**, практичный CPU-only путь | **Низкая:** пакет + `ollama pull`; мало контроля конкретного GGUF и шаблона | Самый быстрый portability smoke test и проверка отсутствия `tool_choice` |

> **Нельзя приравнивать `tools` к `tool_choice`.** Ollama v0.33.0 заявляет tools, но помечает `tool_choice` неподдержанным в `/v1/chat/completions`; vLLM 0.19.1 имеет auto/required/named tools, но `strict:true` — no-op. Поэтому feature probe должен проверять каждую ступень отдельно. [10] [15]

## Рекомендуемый воспроизводимый вариант: Qwen2.5-Coder-7B Q4_K_M + llama.cpp v0.3.0

Ниже команды предназначены для Ubuntu в WSL2. Они фиксируют llama.cpp на stable release `v0.3.0`, а не используют движущуюся ветку `master`. Для GPU нужен доступный из WSL2 NVIDIA driver и CUDA Toolkit: `nvidia-smi` должен отработать в WSL2, а `nvcc --version` — вернуть компилятор. Официальная инструкция llama.cpp для CUDA использует CMake с `-DGGML_CUDA=ON`. [1] [18]

### 1. Проверить WSL2 GPU и поставить зависимости

```bash
# В WSL2 Ubuntu
nvidia-smi
nvcc --version

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential cmake git curl jq ca-certificates
```

Если `nvidia-smi` доступен, но `nvcc` отсутствует, сначала установите **CUDA Toolkit для WSL** по официальной инструкции NVIDIA; не устанавливайте Linux NVIDIA display driver внутри WSL2. Затем снова проверьте `nvcc --version`. CPU-вариант возможен: в командах сборки ниже замените `-DGGML_CUDA=ON` на отсутствие этого флага и в запуске добавьте `-ngl 0`.

### 2. Склонировать и собрать зафиксированную версию

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
git checkout c1d0e7a004015f23bc0233470b747b596f29b264  # llama.cpp v0.3.0

git rev-parse HEAD
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
cmake --build build --config Release -j"$(nproc)"
./build/bin/llama-server --version
```

Ожидаемая проверка `git rev-parse HEAD` — тот же commit. Это важнее номера nightly-сборки: tool templates и выходная форма tool call меняются между версиями.

### 3. Запустить сервер с фиксированной Q4-квантизацией

Команда загрузит опубликованный Q4_K_M файл из репозитория Qwen при первом запуске. `--jinja` критичен: документация связывает OpenAI function calling llama-server именно с этим флагом. [2] [4]

```bash
cd ~/src/llama.cpp

./build/bin/llama-server \
  --hf-repo Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M \
  --jinja \
  --ctx-size 4096 \
  --n-gpu-layers all \
  --flash-attn auto \
  --fit-target 1024 \
  --temp 0 \
  --alias qwen2.5-coder-7b-q4km \
  --host 127.0.0.1 \
  --port 8080
```

Для CPU-only запуска примените тот же model identifier, но соберите без CUDA и замените `--n-gpu-layers all` на `--n-gpu-layers 0`. Для качества baseline лучше оставить `--ctx-size 4096`, температуру 0 и KV-cache стандартного типа; не «спасать» память чрезмерной KV-квантизацией. [2] [3]

### 4. Убедиться, что endpoint отвечает

Во втором WSL2-терминале:

```bash
export BASE_URL=http://127.0.0.1:8080
export MODEL=qwen2.5-coder-7b-q4km

curl --fail --silent "$BASE_URL/v1/models" | jq .

curl --fail --silent "$BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\
    \"model\": \"$MODEL\",\
    \"temperature\": 0,\
    \"messages\": [{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]\
  }" | jq '.choices[0].message.content'
```

Ожидается присутствие модели в `/v1/models` и строка `pong` в обычном completion. Это проверяет transport, base URL, model alias и chat template до того, как ошибка tools будет ошибочно принята за сетевую проблему.

### 5. Проверить реальный формат tool call

```bash
cat >/tmp/tool-request.json <<'JSON'
{
  "model": "qwen2.5-coder-7b-q4km",
  "temperature": 0,
  "messages": [
    {
      "role": "system",
      "content": "When the user asks for arithmetic, call the calculator tool. Do not answer in prose."
    },
    {
      "role": "user",
      "content": "What is 17 multiplied by 23?"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "calculator",
        "description": "Evaluate one elementary arithmetic expression.",
        "parameters": {
          "type": "object",
          "properties": {
            "expression": {"type": "string", "description": "An arithmetic expression"}
          },
          "required": ["expression"],
          "additionalProperties": false
        }
      }
    }
  ]
}
JSON

curl --fail --silent "$BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/tool-request.json | tee /tmp/tool-response.json | jq .

# Проверка документированного для llama.cpp v0.3.0 flat-варианта ответа.
jq -e '
  .choices[0].finish_reason == "tool" and
  (.choices[0].message.tool_calls | type == "array") and
  (.choices[0].message.tool_calls[0].name == "calculator") and
  ((.choices[0].message.tool_calls[0].arguments | fromjson).expression | type == "string")
' /tmp/tool-response.json
```

При успехе последняя команда печатает `true` и завершается с кодом 0. Типичный ответ v0.3.0 имеет форму:

```json
{
  "finish_reason": "tool",
  "message": {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {"name": "calculator", "arguments": "{\"expression\":\"17 * 23\"}"}
    ]
  }
}
```

Это ровно тот compatibility edge case, который baseline должен зафиксировать. В production-агенте нормализуйте оба варианта до внутреннего представления: `tool_calls[].function.{name,arguments}` **и** flat `tool_calls[].{name,arguments}`; `arguments` всегда разбирайте как JSON string и валидируйте собственной JSON Schema.

### 6. Зафиксировать capability vector, а не только score

На той же модели последовательно отправьте четыре безвредных запроса и сохраните HTTP status, response JSON и выбранную ступень:

| Порядок | Проверяемое поле | Условие успеха | Интерпретация |
|---|---|---|---|
| 1 | `tools` без `tool_choice` | Есть распознанный tool call | Native-tools путь доступен |
| 2 | `response_format: {"type":"json_schema", ...}` | Ответ проходит локальную schema validation | Есть schema-constrained путь; **не** доказан strict |
| 3 | `response_format: {"type":"json_object"}` | `json.loads(content)` успешен и верхний уровень object | Работает JSON-only fallback |
| 4 | Обычный prompt с явным JSON contract | Локальный parser либо принимает, либо фиксирует ошибку | Последняя текстовая ступень |

Не принимайте HTTP 200 за доказательство соблюдения схемы или исполнения инструмента. Для каждого шага отдельно валидируйте структуру и параметры на стороне агента.

## Что переносить на неизвестную модель оценки нельзя

Результат такого прогона — **baseline совместимости и нижняя оценка на определённом семействе моделей**, а не предсказание score на скрытой системе. Особенно нельзя переносить следующие выводы.

| Непереносимый вывод | Почему он не переносится |
|---|---|
| «Если Qwen 7B выбрал правильный инструмент, скрытая малая модель тоже выберет его» | Выбор инструмента зависит от training data, tool-use fine-tuning, языка описаний, размера модели, reasoning режима и конкретного prompt template. API-совместимость не делает эти свойства одинаковыми. |
| «Синтаксически валидный JSON значит семантически верные аргументы» | Constrained decoding может гарантировать форму, но не смысл значения, правильное имя инструмента или правильную последовательность действий. Особенно это важно для `json_schema` fallback. |
| «`strict:true` сработает в оценке так же» | llama.cpp v0.3.0 не документирует strict protocol; vLLM 0.19.1 явно принимает `strict` без эффекта. Даже позитивный JSON-прогон ничего не говорит о реальном strict capability скрытого endpoint. [3] [10] |
| «Один формат ответа tools является универсальным» | llama.cpp v0.3.0 документирует flat `name`/`arguments`; vLLM документирует канонический `tool_calls[].function`; другие серверы могут менять finish reason, id и streaming chunks. [2] [10] |
| «Если сервер принимает поле, оно реализовано» | Ollama v0.33.0 поддерживает `tools`, но явно не поддерживает `tool_choice` в Chat Completions. В других реализациях поле может игнорироваться, частично применяться или приводить к 400. [15] |
| «Q4 GGUF и 8 GB VRAM дают ту же latency и context budget, что у оценки» | Весовая квантизация, KV-cache, batch, GPU/CPU offload, flash attention, контекст, concurrency и back-end меняют memory pressure и скорость. Даже одна модель на другом сервере может иметь иной бюджет. |
| «Высокий результат xLAM переносим на обычную малую LLM» | xLAM специально обучена для function-calling; это полезный специализованный контроль, но систематически оптимистичный представитель для скрытой general-purpose модели. [8] |
| «32B score является ожидаемым score оценки» | 32B Q4 — верхняя точка локального набора, а не «малая» модель в обычном смысле. Её следует использовать только как upper bound и диагностику headroom. |
| «Падение на CPU — показатель способности модели» | CPU-прогон хорошо показывает fallback и API-совместимость, но время ответа и timeout-поведение не переносятся на GPU endpoint оценки. |

Из прогона разумно переносить только следующее: агент не зависит от одной формы `tool_calls`; он различает «поле принято» и «схема реально валидна»; он пробует capabilities в порядке `tools → json_schema → json_object → text`; он локально валидирует аргументы и готов к 400/422, пустым `tool_calls`, malformed JSON и обычному тексту. Это свойства **обвязки агента**, а не обещание качества скрытой модели.

## Рекомендуемый протокол отчётности по эксперименту

Сохраняйте для каждого запуска версию сервера/commit, модель и checksum/имя GGUF, квантизацию, `ctx-size`, KV type, `n-gpu-layers`, GPU/драйвер, temperature/seed, template/parser, HTTP request и raw response. Сводите результаты как минимум в две независимые метрики: **protocol pass rate** по каждой ступени лестницы и **semantic tool correctness** после локальной validation/исполнения безопасного инструмента. Не усредняйте эти метрики: server-side grammar способен улучшить первую, почти не улучшая вторую.

## References

[1]: https://github.com/ggml-org/llama.cpp/releases/tag/v0.3.0 "llama.cpp v0.3.0 release"
[2]: https://raw.githubusercontent.com/ggml-org/llama.cpp/c1d0e7a004015f23bc0233470b747b596f29b264/docs/function-calling.md "llama.cpp v0.3.0 function-calling documentation"
[3]: https://raw.githubusercontent.com/ggml-org/llama.cpp/c1d0e7a004015f23bc0233470b747b596f29b264/tools/server/README.md "llama.cpp v0.3.0 server documentation"
[4]: https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF "Qwen2.5-Coder-7B-Instruct-GGUF"
[5]: https://huggingface.co/unsloth/Qwen2.5-Coder-14B-Instruct-GGUF "Qwen2.5-Coder-14B-Instruct GGUF files"
[6]: https://huggingface.co/bartowski/Qwen2.5-Coder-32B-Instruct-GGUF "Qwen2.5-Coder-32B-Instruct GGUF files"
[7]: https://huggingface.co/bartowski/Mistral-Nemo-Instruct-2407-GGUF "Mistral Nemo Instruct 2407 GGUF files"
[8]: https://huggingface.co/Salesforce/xLAM-2-3b-fc-r-gguf "Salesforce xLAM-2-3b-fc-r GGUF"
[9]: https://huggingface.co/hugging-quants/Llama-3.2-3B-Instruct-Q4_K_M-GGUF "Llama-3.2-3B-Instruct Q4_K_M GGUF"
[10]: https://docs.vllm.ai/en/v0.19.1/features/tool_calling/ "vLLM 0.19.1 Tool Calling"
[11]: https://docs.vllm.ai/en/v0.19.1/serving/openai_compatible_server/ "vLLM 0.19.1 OpenAI-Compatible Server"
[12]: https://docs.vllm.ai/en/v0.19.1/features/structured_outputs/ "vLLM 0.19.1 Structured Outputs"
[13]: https://docs.vllm.ai/en/v0.19.1/getting_started/installation/cpu/ "vLLM 0.19.1 CPU installation"
[14]: https://github.com/ollama/ollama/releases/tag/v0.33.0 "Ollama v0.33.0 release"
[15]: https://docs.ollama.com/openai "Ollama OpenAI compatibility"
[16]: https://docs.ollama.com/capabilities/tool-calling "Ollama tool calling"
[17]: https://docs.ollama.com/capabilities/structured-outputs "Ollama structured outputs"
[18]: https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md "llama.cpp CUDA build guide"

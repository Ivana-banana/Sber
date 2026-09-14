# Qwen3 30B-A3B — сырое наблюдение thinking через ollama OpenAI-endpoint (2026-09-14)

Стенд: эта машина, sber-model (ollama/ollama, `--gpus all`, ctx 4096) на sber-eval,
модель `qwen3:30b-a3b` (18 ГБ, частичный офлоад на RTX 4080 16 ГБ). Запрос — из контейнера
acp на той же сети (`urllib`, stdlib), НЕ через нашу llm.py.

## Пробой 1 (max_tokens=2048, action-промпт с «think first»)

```
finish_reason: stop
message keys: ['content', 'reasoning', 'role']
content_len: 33      content = {"tool":"list_dir","path":"/app"}   (чистый JSON, БЕЗ <think>)
reasoning_present: True   (thinking в ОТДЕЛЬНОМ поле message.reasoning)
usage: prompt 37, completion 343, total 380   (thinking ~310 токенов, из 2048 — запас)
```

## Выводы (наблюдение, не вывод от отсутствия)

1. Ollama для qwen3:30b-a3b кладёт reasoning в **отдельное поле `message.reasoning`**, а
   `content` отдаёт ЧИСТЫМ — `<think>`-тегов в content нет. Наш парсер читает content →
   получает валидный action. Мой `_strip_thinking` здесь **no-op** (безвреден).
2. `finish_reason=stop`, thinking укладывается в бюджет — **стены `degrade` из-за длины НЕ будет**
   на этом сервере (опасение ревьюера снято для ollama-конфигурации).
3. НО это про ollama. Организаторский endpoint (OpenRouter/vLLM) может класть `<think>` в
   content — именно поэтому в acp-образе pydantic_ai его режет. Наш `_strip_thinking` —
   защита для ТОГО случая, локально неизмеримого. Оставляем как safety.
4. `message.reasoning` мы игнорируем (правильно): нам нужен action, а не рассуждение.

## Пробой 2 (max_tokens=2048, промпт ФОРСИРУЕТ длинные рассуждения)

```
finish_reason: length     content_len: 3539 (ПРОЗА про БД, не JSON)   reasoning_len: 5857
content_has_think_tag: False
```

Нюанс: при явном «reason at length» модель льёт рассуждение и в `content` (прозой, без
тегов), выходит за 2048 (finish=length), action НЕ выдаёт. `_salvage_json` не найдёт `{`
→ «not json» → demote. Это подтверждает опасение ревьюера про finish_reason=length —
но только когда промпт провоцирует рассуждение. Наш агентский SYSTEM_SKELETON, наоборот,
велит «Do not write reasoning» (как пробой 1 → чистый JSON, finish=stop). Реальный
прогон покажет, случается ли спилл на тяжёлых задачах (audit/fix). Если увижу стену
`degrade`/`endpoint error` на первых шагах — это оно, и тогда нужен finish_reason-фикс
(при length не деградировать) и/или enable_thinking:false.

## Пробои 3-5 (рычаги против degrade-wall, из acp-контейнера на sber-eval)

| Рычаг | finish | content | reasoning | completion | Итог |
|---|---|---|---|---|---|
| A `chat_template_kwargs.enable_thinking=false` | stop | чистый action (33) | 1150 | 278 | **ollama ИГНОРИРУЕТ — thinking остался** |
| B `think:false` (top-level) | stop | чистый action (33) | 1806 | 413 | **ollama ИГНОРИРУЕТ — thinking остался** |
| C `max_tokens=8192`, тяжёлый audit-промпт | **stop** | чистый action (51) | 2695 | 610 | **degrade-wall ИСЧЕЗ — рассуждение уложилось** |

Выводы для дизайна finish_reason-фикса:
1. Выключить thinking на ollama /v1 **нельзя** (оба поля игнорируются). На vLLM/сервере
   организаторов может работать — в код идёт как safe-попытка (неизвестное поле игнор).
2. **Рабочий рычаг — поднять `max_tokens`.** При 8192 тяжёлый audit finish=stop,
   completion всего 610. Стена в прогоне L-Q3 была из-за `LLM_MAX_OUTPUT_TOKENS=2048`, а
   НЕ из-за ёмкости 30B.
3. Локальная оговорка: `num_ctx=4096` у sber-model всё равно ограничит thinking, когда
   агентский промпт (~2-3k токенов system+observations) + рассуждение + ответ > 4096.
   Для чистого capability-прогона 30B локально нужно поднять И CONTEXT контейнера, И
   LLM_MAX_OUTPUT_TOKENS. На eval-сервере организаторов ctx у 35B, вероятно, больше.
4. Итог L-Q3: **2/6 — это degrade-wall (бюджет вывода), НЕ ёмкость 30B.** Чистая ёмкость
   30B не измерена (нужен прогон с поднятым бюджетом). Токены прогона занижены
   accounting-дырой (llm.py считает usage только на успехе).

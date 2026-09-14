# Дизайн: устойчивость к thinking-эндпоинту (учёт токенов + finish_reason)

Спроектировано 14.09 в коопе (ai-challenge-43 + ai-challenge-cc). Реализация и ревью —
следующая сессия. Оба фактора в `agent/llm.py` → tool/LLM-policy: человек + парный
замер до кандидата. Основание — карточка L-Q3 и `2026-09-14-qwen3-thinking-raw.md`:
организаторская модель (qwen3.6-35b-a3b) — thinking; на ней наш агент на reasoning-задачах
даёт degrade-wall (`llm-unusable`, tokens=0), и это НЕ ёмкость, а бюджет вывода.

## Факты из пробоев (sber-model, ollama /v1, qwen3:30b-a3b)
- Thinking приходит в отдельном поле `message.reasoning`, `content` при коротком промпте
  чистый; но thinking ВХОДИТ в `usage.completion_tokens`.
- Выключить thinking на ollama /v1 НЕЛЬЗЯ: `chat_template_kwargs.enable_thinking=false`
  и `think:false` игнорируются (reasoning остаётся). На vLLM/OpenRouter может работать.
- `max_tokens=8192` на тяжёлом audit-промпте → finish=stop, completion 610: **бюджет —
  рабочий рычаг**, стена была из-за `LLM_MAX_OUTPUT_TOKENS=2048`.

## #1 — Учёт usage на КАЖДЫЙ ответ (accounting, отдельный коммит первым)
Сейчас `_extract_usage` вызывается только на успешном `_read_reply` (llm.py ~688). Любой
ответ в LlmError (empty content / not json / schema / demote) — а на thinking-модели это
все ступени — выпадает из счётчика, хотя организаторский счётчик его видит. Итог: наши
токены на thinking-прогоне занижены, тай-брейк несравним.
- `_attempt_loop`: acc=(in,out,reported), `_extract_usage` после КАЖДОГО распарсенного
  payload, аккумулировать. Штамповать acc на ВСЕ ~8 точек возврата (transport/status/
  not-json/read-fail/demote/success) — усвоить, что демоцированные попытки суммируются
  в итог даже при успехе на нижней ступени.
- `LlmError` (ports.py) получает `input_tokens/output_tokens/usage_reported`.
- `state.add_usage` принимает `LlmReply | LlmError`; `loop.py` считает и ошибки (сейчас на
  LlmError add_usage не зовётся, loop.py:147).
- Тест-фикстура (напарник): 4 ответа по 2048 completion, все unusable → LlmError с
  output_tokens=8192 (ровно случай find-sqli на Qwen3).

## #2 — finish_reason (после #1, с рычагами по пробоям)
- Читать `choices[0].finish_reason` и наличие `reasoning`/`reasoning_content`.
- **Sticky thinking-mode (напарник):** первая обрезка по length ИЛИ первый ответ с полем
  reasoning → клиент переходит в режим «thinking endpoint» до конца прогона: max_tokens
  поднят СРАЗУ, без пробного 2048 (иначе каждый тяжёлый шаг платит +2048 впустую). Обратно
  не опускать. Per-run состояние по образцу sticky-degradation, не хардкод под модель.
- **Потолок max_tokens — от остатка бюджета задачи и контекста, не константа 8192**
  (напарник): при строгом сервере (vLLM) `prompt+max_tokens > ctx` → 400. Защита: на 400 с
  текстом про max_tokens/context повторить с меньшим значением, БЕЗ демоции.
- **enable_thinking:false / think:false — safe-попытка** на повторе для vLLM/сервера
  организаторов (ollama игнорит, вреда нет). Не хардкодить `/no_think` в промпт.
- При length с пустым/непарсящимся content — НЕ `_demote` (форма не виновата, лестница
  теряется зря); вместо этого повтор на той же ступени с поднятым бюджетом; если и повтор
  length → LlmError «reply truncated at N tokens».
- Компромисс: enable_thinking:false может снизить ёмкость (thinking — часть «ума» 35B-A3B);
  порядок рычагов — сначала бюджет, thinking-off только на повторе. Всё подлежит замеру.

## Оговорка для чистого capability-прогона 30B локально
num_ctx=4096 у sber-model режет thinking при большом агентском промпте. Для честного
замера ёмкости 30B поднять И CONTEXT контейнера (start_model.sh), И LLM_MAX_OUTPUT_TOKENS.
Это меняет baseline (E049 мерился под 4096) — отдельным помеченным прогоном.

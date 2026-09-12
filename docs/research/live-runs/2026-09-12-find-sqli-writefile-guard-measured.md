# `find-sqli-login`: измерение write_file JSON-guard (f6c10cb) — 12.09.2026

## Что мерили

Починка `f6c10cb` («write_file сообщает, что JSON-deliverable не разбирается»)
живым прогоном не измерялась ни разу. Первый шаг P3 — измерить именно её, один
фактор, неизменные архивы, один стенд.

- **with-fix** архив `def0e66928436e17794b7b5b61c6b49dd4212c0eeaf09b3889c02ec495aac0b2`
  (чистый HEAD `fix/public-solves`, содержит `f6c10cb`).
- **without-fix** архив `133b81d09b0dc38be986ec85eceacb35809672da63828a31a4478e450decf53c`
  — реверс только `f6c10cb` на `agent/tools.py`; **байт-в-байт совпал с E-BASE-r1**,
  поэтому без-фикс плечо — это уже измеренный E-BASE, повторный прогон не нужен.
- Модель `qwen2.5-coder:7b-instruct-q4_K_M` (digest `dae161e27b0e`), context 4096,
  offload 61% GPU / 39% CPU, стенд `claude-e-model-20260912`, `--timeout-scale 1.0`.

## Результат

| плечо | reward | tokens | ep_tokens | wall | steps | stop | deliverable |
|---|---|---|---|---|---|---|---|
| without-fix (E-BASE-r1) | 0 | 71758 | 74175 | 306.4 | 30 | steps | `{"findings": []}` |
| **with-fix (f6c10cb)** | **0** | 70062 | 72438 | 205.4 | 30 | steps | `{"findings": []}` |

**Починка не даёт зачёт.** reward 0 → 0.

## Почему — покадрово

На **call 9** обе руки записывают **байт-в-байт одинаковую** находку в
`/app/security_report.json`: SQL-инъекция в `auth.py:17`, evidence с самим уязвимым
f-string. Этот текст **проходит все три gate** официального verifier
(`sql_injection_signals`, `login_signals`, `detail_signals`). Но файл невалиден:
незакрытая/неэкранированная кавычка в `evidence`, `json.loads` падает на
`Expecting ',' delimiter: line 1 column 309`. Правильно экранированная версия этой же
находки дала бы зачёт — узкое место не поиск и не понимание.

Guard сработал как задумано. Наблюдение, которое модель получила на call 9
(воспроизведено на точном контенте через `agent.tools.write_file`):

```
wrote 598 bytes to .../security_report.json; the file is not valid JSON:
Expecting ',' delimiter: line 1 column 309 (char 308)
```

Реакция модели на это сообщение: **call 10** = `{"findings": [` (одна оборванная
попытка переоткрыть массив), **call 11–30** = `{"findings": []}` двадцать раз, до
`stop=steps`. Diff траекторий with-fix против E-BASE: guard изменил ровно **одно
действие** (call 10: `{"findings": [` вместо сразу пустого), после чего — тот же
коллапс. Остальные расхождения — дрейф формулировок verify/summary от
нон-детерминизма стенда, структурная траектория (находка на call 9 → невалидный
JSON → коллапс в пустой отчёт) идентична.

## Вывод

- write_file JSON-guard **детектирует** дефект, но 7B не **чинит** экранирование:
  получив сообщение о невалидном JSON, модель делает максимум одну бесплодную попытку
  и уничтожает свою же верную находку в `{"findings": []}`.
- Это ровно результат P2-пробы, перенесённый в P3: **диагностика/уведомление ≠
  действие по починке** для этой модели. Ещё одно текстовое уведомление не поможет.
- Узкое место `find-sqli-login` — **не навигация, не понимание, не аудит** (всё это на
  call 9 выполнено верно), а **сериализация JSON слабой моделью вручную** и
  деструктивное восстановление. Кандидат следующего фактора (tool policy, требует
  ревью человеком перед релизом): не заставлять модель руками экранировать JSON —
  принимать находку как структурный аргумент tool и сериализовать через `json.dumps`
  на стороне harness (слой, который модель уже заполняет корректно). Не хардкодить
  схему public-задачи; безопасный кейс по-прежнему допускает пустой отчёт; verifier
  по-прежнему судит.

## Воспроизведение

```sh
# with-fix прогон
python3 scripts/eval/run_suite.py --tasks-root local_task --only find-sqli-login \
  --archive build/p3-withfix-*/submission.zip \
  --model qwen2.5-coder:7b-instruct-q4_K_M \
  --base-url http://claude-e-proxy-20260912:8000/v1 --api-key ollama \
  --network sber-eval --counter-container claude-e-proxy-20260912 \
  --timeout-scale 1.0 --reuse-images --out build/p3-withfix-run-XXXX
# without-fix архив == E-BASE-r1 (133b81d0…), см. его строку в results.tsv
```

Сырые логи в `build/p3-withfix-run-LG7qF5/logs` (gitignored). call-9 контент
идентичен E-BASE, sha находки — по трейсу.

# F4 — парный замер, готовый к прогону на стенде 4080 (2026-09-14)

F4 снимает 90-с pre-checkpoint clamp с LLM-вызова, как только замечен медленный/thinking
эндпоинт (`agent/state.py` `allow_long_llm`, устанавливается в `loop.py` и `macro.py`
через `_endpoint_is_slow_or_thinking`). Дизайн — `2026-09-14-F4-design.md`, вариант A.
Мотивация — карточка L-Q3-FIX (4/6 на `qwen3:30b-a3b`): `incident-log-forensics` умер по
**бюджету времени** (`stop=budget`, 480 с), а thinking-вызовы `find-sqli`/`fix-sqli`
обрезались ровно на ~90 с этим клампом и уходили в «lower bound» без payload.

Этот документ — **не результат**, а готовая к исполнению процедура. Причинность даёт
только парный прогон на стенде, которого нет на этом ноутбуке (RTX 3050, 4 ГБ; 30B не
помещается). Собрано и проверено здесь offline; замер запускается на машине с RTX 4080.

## Что уже сделано (offline, на этом ноутбуке)

- F4 интегрирован в ветку `fix/write-json-deliverable`; runtime-коммит **`8b568ac`**.
  Полный тест-прогон: **788 passed, 1 skipped** (`python3 -m pytest -q`).
- Diff `64636f9 → 8b568ac` затрагивает только `agent/loop.py`, `agent/macro.py`,
  `agent/state.py` + `tests/test_budget.py`, `tests/test_loop.py` — **ровно один фактор**.
- Собраны и проверены оба архива (`scripts/check_submission.py` — все PASS):

  | Сторона | Коммит | Архив (локальная сборка) | SHA-256 | Размер |
  |---|---|---|---|---|
  | control (без F4) | `64636f9` | `build/nof4.zip` | `19cc77e162abb79771c47615142b36c2717abc7734ad4584fa5de548538bf4f9` | 121490 |
  | treatment (F4) | `8b568ac` | `build/f4.zip` | `246ef93eba31aa5881325ff811d71a1fcde7c0c6ece89ed6a6f0208951b2a102` | 122418 |

  SHA относятся к сборке на этом ноутбуке. `build_submission.py` не гарантирует
  побайтовую воспроизводимость между машинами — на стенде **пересобрать из тех же
  коммитов** (команды ниже) и записать полученные SHA в итоговую карточку. Источник
  истины — коммиты `8b568ac` (treatment) и `64636f9` (control), не сами zip.

## Сборка пары на стенде (из коммитов, гарантирует один фактор)

```sh
git fetch --all --prune
git worktree add --detach /tmp/f4-treatment 8b568ac
( cd /tmp/f4-treatment && python3 scripts/build_submission.py --out build/f4.zip \
    && python3 scripts/check_submission.py build/f4.zip && sha256sum build/f4.zip )

git worktree add --detach /tmp/f4-control 64636f9
( cd /tmp/f4-control && python3 scripts/build_submission.py --out build/nof4.zip \
    && python3 scripts/check_submission.py build/nof4.zip && sha256sum build/nof4.zip )
```

## Стенд

Один оператор endpoint — согласовать очередь перед прогоном. Поднимать **только**
`scripts/eval/start_model.sh` + `scripts/eval/start_proxy.sh` (легаси-nvidia fallback уже
внутри; иначе молча уедешь на CPU). Проверить размещение до прогона:
`docker exec <model> ollama ps` — PROCESSOR должен быть преимущественно GPU на 30B.

- **30B thinking (основной замер):** `qwen3:30b-a3b`, `CONTEXT=16384`, RTX 4080/18 ГБ.
  ```sh
  CONTEXT=16384 STORE=/var/lib/ollama bash scripts/eval/start_model.sh
  bash scripts/eval/start_proxy.sh
  ```
- **7B (регресс-страж):** `qwen2.5-coder:7b-instruct-q4_K_M`, `CONTEXT=4096`.

## Прогон

```sh
# 30B: основной замер (control -> treatment на одном тёплом стенде, 3 повтора)
MODEL=qwen3:30b-a3b PROXY=<proxy-container> TAG=q3-30b \
  scripts/eval/f4_paired_measurement.sh /tmp/f4-control/build/nof4.zip /tmp/f4-treatment/build/f4.zip

# 7B: регресс-страж (та же пара, ctx 4096)
MODEL=qwen2.5-coder:7b-instruct-q4_K_M PROXY=<proxy-container> TAG=7b \
  scripts/eval/f4_paired_measurement.sh /tmp/f4-control/build/nof4.zip /tmp/f4-treatment/build/f4.zip
```

Каждый прогон — новый `--out` (скрипт делает это по `TAG`), старые summaries не
перезаписывать. `results.tsv` каждого — источник для карточки.

## Шлюзы приёмки

1. **Валидность:** control сходится сам с собой между 30B и его же повтором в пределах
   известного разброса; иначе стенд дрейфует и сравнение недействительно.
2. **30B, ожидание (не гарантия):** на treatment у thinking-задач меньше или нет пометок
   «lower bound»; `incident-log-forensics` завершается не по `budget`; solved **≥** control
   (в идеале +1). Рост токенов допустим — полный thinking считается честнее, это не регресс
   по solved.
3. **7B, критично:** solved и deliverable-count treatment **не ниже** control. На быстром
   эндпоинте `allow_long_llm` не выставляется, поведение должно совпасть с клампом.
   Любая потеря здесь — блокер интеграции F4.
4. Ни один вызов не killed раннером из-за отсутствия раннего deliverable на 7B.

## Решение

- Все шлюзы зелёные и на 30B есть выигрыш (или хотя бы отсутствие регресса при меньшем
  числе обрезанных вызовов) → F4 к интеграции; **человеческое ревью budget-политики
  обязательно** (CLAUDE.md: prompt/tool policy/budgets — не в `main` без ревью).
- 7B регресс → F4 отклонить или ужесточить сигнал `_endpoint_is_slow_or_thinking`.
- Записать карточку в `docs/experiments.md`: коммиты, SHA пересобранных архивов, SHA
  каждого `results.tsv`, модель/ctx/образ, tokens input/output/total, wall, stop и
  классификацию каждого нуля. Результат без команды воспроизведения не считается.

## Оговорки

- 4/6 и потолок 6/6 достижимы только на 30B-классе; на 7B `fix-sqli-login` опровергнут
  пятикратно. F4 бьёт по одному из двух оставшихся нулей 30B (`incident`, упор во время);
  второй (`fix-sqli-login`, неверная правка) F4 не трогает — это отдельная работа.
- Первый обрезанный вызов на 30B всё равно теряется (сигнал по timeout приходит после
  него); F4 спасает остаток прогона, а не самый первый вызов.

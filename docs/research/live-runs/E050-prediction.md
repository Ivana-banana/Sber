# E050 — baseline 18 fixtures на исправленном стенде

Предзарегистрировано до запуска, 05.09.2026. Контрольный exact archive E039:
`36c6cb4d34733d0dc538a0e19e05d03daf76ff146216defc6ffee3305e0824a7`.
Это новая точка на другом наборе; E049 не является парным control на 18 fixtures.
Старые CPU/95–180s прогоны — только исторический контекст.

Команда: `python3 scripts/eval/run_suite.py --tasks-root local_task_extra/tasks --archive build/e039-anchored-history.zip --model qwen2.5-coder:7b-instruct-q4_K_M --base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0 --label E050-fixtures-gpu --out build/eval-E050-fixtures-gpu`.
Запуск только `setsid nohup`, sequential model access. Архив не пересобирать.

| Метрика results.tsv | Полоса ожидания | Условие опровержения |
|---|---|---|
| timeout | 0–4/18 | ≥5; доминирование timeout: больше половины всех нулей |
| solved | 2–8/18 (слабое ожидание) | вне полосы, без причинного вывода |
| wrote | 12–18/18 | <12 |
| ep_tokens / ep_calls | 1500–3000 | вне полосы |
| sum wall_s | 1800–4200 с | вне полосы |

При неполных 18 строках или startup/packaging дефекте это не законченный baseline.
Каждый ноль получает failure/stop из harness и ограничение классификации; verified
при reward=0 — ложная внутренняя проверка. Входящие/исходящие токены отдельно TSV
не сохраняет: не восстанавливать их догадкой из суммарных. Далее E051 повторяет
точные bytes, budgets и модель; лишь после повтора оцениваем изменения.

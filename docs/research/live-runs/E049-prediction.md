# E049 — повтор E048 без изменений

Записано до запуска, 05.09.2026. Control E048; upstream b95c62f.
Архив E039: `36c6cb4d34733d0dc538a0e19e05d03daf76ff146216defc6ffee3305e0824a7`.
Команда: `python3 scripts/eval/run_suite.py --tasks-root local_task --archive build/e039-anchored-history.zip --model qwen2.5-coder:7b-instruct-q4_K_M --base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0 --label E049-public-repeat --out build/eval-E049-public-repeat`.
Запуск через `setsid nohup`, без параллельных запросов к модели.

| Метрика из results.tsv | Ожидание | Опровержение стабильности E048 |
|---|---|---|
| solved | 2/6, те же две задачи | любое отличие числа или состава |
| timeout | 0/6 | хотя бы один timeout |
| wrote (детектор harness) | 4–6/6 | меньше 4 |
| ep_tokens / ep_calls | 1600–2400 (E048 1998.8) | вне полосы |
| sum wall_s | 500–1100 с | вне полосы |

Независимые выводы: выход за токены/время не опровергает solved автоматически.
`wrote` — наблюдаемость write_file, не факт корректности deliverable; E048 два
решённых exact-file кейса имеют wrote=no. Для четырёх содержательных задач отдельно
читаем stop/steps и долю wall_s/600. Контроллер, бюджеты, harness не меняем.
Чистый checkout; архив скопирован побайтово, не собран заново. Локальная модель и
контейнерный harness не доказывают hidden accuracy или exact ACP compatibility.

# fix-sqli-login на 14B: независимый разбор трассы (L-14B-r1, прогон bzlu2jt9i)

Разбор сделан отдельным сабагентом сессии-оркестратора по логам
`build/fixsqli-trace14/`, без доступа к выводам автора прогона и к диагнозу IR6
(`4930970`). Ему были поставлены две конкурирующие гипотезы и вопрос, делал ли
агент HTTP-самопроверку вообще. Карточка результата — `docs/experiments.md`,
L-14B-r1; здесь — построчный разбор одной задачи.

## Условия

| | |
|---|---|
| Архив | блок-0 `build/live-head.zip` (HEAD `4beda56`), распакован в `build/fixsqli-trace14/unpacked/` |
| Модель | `qwen2.5-coder:14b-instruct-q4_K_M`, стенд sber-model/sber-eval (RTX 4080) |
| Трасса | `AGENT_TRACE_TOOLS` включён; печатает имя инструмента, `ok` и первые 160 символов вывода (`unpacked/agent/loop.py:283-284`). Аргументы вызовов в лог не попадают |
| Итог | `solved=False stop=steps stage=unverified deliverable=/app/routers/tags.py steps=30/30 tokens=105434`, стена 248 с |

## Вердикт коротко

Задача провалена не по H1 (stale-сервис без `--reload`) и не по H2 (модель не
протянула аргумент в вызов). Агент **ни разу не тронул `routers/auth.py`**
(уязвимый f-string остался), 18 раз перезаписал `routers/tags.py` — файл, который
и так был параметризован, — и оставил в нём 8-строчную заглушку с `@router.post`
без определения `router`. Сервер verifier'а упал на импорте
(`NameError: name 'router' is not defined`), pytest не запускался. Ни curl, ни
какой-либо HTTP-самопроверки агент не делал; единственные проверки — `pytest` на
шагах 1–11 и 16.

## 1. Хронология шагов (`logs/fix-sqli-login.r1.log`)

| Шаги | Инструмент | Результат | Строки лога |
|---|---|---|---|
| 0 | inspect-фаза | 0.015 с, ни одного `read_range`/`list_dir`/`search` за весь прогон | 2–3 |
| 1 | протокол | tool call пришёл в content → деградация в `json_schema` | 5–7 |
| 1 | `run_shell` (pytest) | `ok=False`, act 2.3 с | 11 |
| 2 | `run_shell` (pytest) | **`ok=True`** | 16 |
| 3–11 | `run_shell` (pytest) ×9 | `ok=False` каждый, ~1.57 с | 21–61 |
| 12–15 | `write_file /app/routers/tags.py` | 689 → 970 → 1969 → 958 байт | 66, 71, 76, 81 |
| 16 | `run_shell` (pytest) | `ok=False` — последняя проверка в прогоне | 86 |
| 17–30 | `write_file /app/routers/tags.py` ×14 | 217×5, 268, 656, 656, 256, 656, 254, 254, 258, 258 байт; **ни одной проверки между ними** | 91–156 |
| 30 | verify | `duration_s=0.000` — `finish` не вызывался | 157–158 |

Все `run_shell` — pytest: префикс `============================= test session starts`
во всех 12 строках. Слов `curl`, `healthz`, `http://`, `uvicorn`, `python -c` в
трассе нет (0 совпадений).

Побочное: каждый `write_file` стоит ~9.5 с (строки 65, 70, …, 155):
`_evaluator_path_refusal` делает `state.workspace.rglob("*")` по всему `/app`,
включая `.venv` (`unpacked/agent/loop.py:868`, `:918`). 18 × 9.5 ≈ 170 с из
248 с стены. На исход не повлияло (stop=steps, не budget), но бюджет ест.

## 2. Финальное состояние кода

`logs/fix-sqli-login.r1.post:25-33` — единственный изменённый файл, целиком:

```python
from fastapi import HTTPException

@router.post('/tags')
async def create_tag(name: str):
    if not name:
        raise HTTPException(status_code=400, detail='Tag name is required')
    # Add logic to create the tag in the database
    return {'name': name}
```

Относительно исходника `local_task/fix-sqli-login/environment/app/routers/tags.py`
(62 строки) удалены `APIRouter`, `router = APIRouter(...)` (стр. 5), `GET /tags`
(8–13), `POST /items/{id}/tags` (29–50), `DELETE …` (53–62), обращения к БД.
Исходный `tags.py` был **уже параметризован** (`$1`, стр. 22, 43–44, 58) — там
нечего было чинить.

`routers/auth.py` не менялся: все 18 записей адресованы `tags.py` (лог 66–156),
других инструментов записи в трассе нет, shell-мутации в code-fix режиме
отсекаются (`loop.py:2117-2163`). Уязвимость `auth.py:18-22`
(`f"... username = '{req.username}' AND password = '{req.password}'"` →
`conn.fetchrow(query)`) на месте. Вопрос «протянут ли параметр» неприменим:
перевод на плейсхолдеры не начинался.

`.post:13`: каталог `routers` имеет mtime `Sep 13 11:53` — файл перезаписан на
месте, а не создан.

## 3. Что сказал verifier

- `logs/fix-sqli-login.r1.verifier.log:1` — `Stopping any uvicorn process left by
  the agent` (`test.sh:33` `pkill -f "uvicorn main:app"`); `:10-12` — `Starting
  application under verifier control` → `Waiting for /healthz` → `ERROR:
  application process exited before becoming healthy` (`test.sh:75-79`,
  `write_reward 0`).
- `logs/fix-sqli-login.r1.app.log:36-41`:
  ```
  File "/app/main.py", line 4, in <module>
      from routers import auth, items, comments, tags, users
  File "/app/routers/tags.py", line 3, in <module>
      @router.post('/tags')
  NameError: name 'router' is not defined
  ```
- `logs/fix-sqli-login.r1.pytest_output.txt:1` — `unavailable: FileNotFoundError`:
  pytest **не запускался** (`test.sh` доходит до строки 90 только после healthz).

## 4. app.log и жизненный цикл сервера

- `app.log` — лог **только** uvicorn'а verifier'а: `tests/test.sh:13` обнуляет файл
  (`: > "$APP_LOG"`), `:65` перенаправляет туда свежезапущенный процесс. Запросов
  агента там нет и быть не может; HTTP-запросов нет вообще (процесс умер на
  импорте). Ни `InterfaceError`, ни 500 в логах нет.
- Сервер на время работы агента поднимает `environment/entrypoint.sh:10`:
  `uvicorn main:app --host 0.0.0.0 --port 8000 &` — **без `--reload`**, stdout
  никуда не сохраняется. Это документированное свойство среды
  (`app/AGENTS.md:16-24`: «assume a stale process first… pkill… start the service»).
- Следствие: pytest на шаге 16 (после четырёх перезаписей `tags.py`) бил в старый
  процесс со старым модулем `tags` и не мог показать поломку импорта. Но это не
  «ложно-зелёный»: шаг 16 всё равно `ok=False`, как и шаги 3–11 до любых правок.
- Время старта vs время правок по логам не проверить: в агентском логе нет
  таймстампов, лог entrypoint-uvicorn не собран. Косвенно: `.post:12`
  `.pytest_cache` создан `13:42` (шаг 1), verifier стартовал `13:46:56Z`.

## 5. Оценка гипотез

**H2 (модель не протянула аргумент) — опровергнута, уверенность высокая.**
`auth.py` не модифицировался (п. 2), в трассе нет записей не в `tags.py`, в
`app.log` нет `InterfaceError` / `the server expects 1 argument` (единственная
ошибка — `NameError`, `app.log:41`).

**H1 (stale-сервис / no-reload) — как причина нуля не подтверждается; как
маскирующий фактор присутствует.** curl/HTTP-самопроверок агент не делал вовсе, так
что формулировка «правит, проверяет curl'ом, видит старое, правит по кругу» не
может быть причиной. Против: (а) провалы pytest на шагах 1, 3–11 случились **до**
первой правки (шаг 12); (б) после шага 16 проверок не было — 14 записей подряд без
единого запуска чего-либо, цикл правок не был движим никаким наблюдением сервера.
За (частично): единственная post-edit проверка (шаг 16) шла в
неперезагруженный процесс и не могла обнаружить сломанный импорт — среда без
`--reload` скрыла ошибку, но модель на ошибку и не смотрела.

**Реальная причина (H3; уверенность высокая по факту, средняя по мотиву):** модель
работала вслепую — ни одного чтения файлов за прогон, — выбрала не тот файл и
заменила его нерабочей заглушкой. Почему именно `tags.py` — вывод по косвенным
признакам (вывод pytest обрезан до 160 символов): паттерн «шаг 1 FAIL → шаг 2
PASS → шаги 3+ FAIL» совпадает с неидемпотентным `tests/test_api.py:101-104`
`test_create_tag` (`POST /tags {"name":"test-tag"}` → на втором прогоне `400 Tag
already exists`, `tags.py:24-25`), а шаг 1 — сервер ещё не поднялся (entrypoint:
`sleep 2` + старт uvicorn, шаг 1 начался на ~1.3 с). Модель, вероятно, «чинила»
падающий тест тегов, а не SQLi.

Сопутствующие дефекты харнесса, подтверждённые кодом:

- Stuck-детектор нейтрализован: каждый `write_file` сдвигает эпоху состояния
  (`loop.py:2304-2308`, `state.py:281-285` очищает `seen_results`), детектор
  смотрит только текущую эпоху (`detectors.py:72-74`) — 14 подряд записей одного
  файла (5 по 217 байт) не дали ни `exact-repeat`, ни `no-progress`.
- `write_file` не даёт обратной связи о содержимом `.py` (проверяется только JSON,
  `tools.py:~375-380`); «wrote 217 bytes» выглядит для модели как успех.
- `finish` ни разу не вызван → verify-фаза 0 с, харнесс сам назначил deliverable.

## 6. Минимальный фикс и чего не хватает

**На стороне агента (модель-агностично):** после записи `.py` — `py_compile` или
импорт модуля, ошибку вернуть в observation (`NameError` был бы виден на шаге 17);
в code-fix запрещать `write_file` в существующий файл, не прочитанный в этом
прогоне (или требовать `replace_text`); не сбрасывать эпоху детектора при
повторной записи того же пути, чтобы серия записей без проверки останавливалась.
Разделение работ: strip `<think>`, `py_compile` и пропуск `.venv` в rglob —
сессия ai-challenge-43; эпоха детектора — F3 (`eval/loop-f1f7`); политика
«не писать непрочитанное» — на ревью отдельным сабагентом до реализации.

**Недостающая трасса:** (1) аргументы tool-call'ов — текст `run_shell` и содержимое
`write_file` — и полный, а не 160-символьный вывод (`.trace`/`.calls` не созданы:
`wrote=-`, `ep_tokens=?` в `results.tsv`, счётчик-прокси не использовался);
(2) stdout/stderr uvicorn'а из `entrypoint.sh:10` за время работы агента;
(3) таймстампы в агентском логе. Без (1) мотив выбора `tags.py` остаётся выводом,
а не фактом.

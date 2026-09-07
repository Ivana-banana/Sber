# N3: передача агенту, который запускает модель

Код готов в `eval/n3-source-diagnostic`; локальный worktree
`.worktrees/n3-source-diagnostic`. Автор кода больше не запускает модель по
уточнению владельца от 08.09.2026. Runtime и official fixtures не менялись.

## Сначала исправленный verifier

При подготовке N3 обнаружен дефект общего `scripts/eval/run_suite.py`:
`Runner.install` копирует tests в `/opt/verifier`, а официальный audit `test.sh`
исполняет `pytest /tests`. В новом контроле `build/eval-N3-B` pytest собрал
**0 тестов**, завершился с кодом 4 (`file or directory not found: /tests`),
launcher записал reward=0. Это ошибка инфраструктуры, не содержательный verdict.

N3 wrapper теперь копирует исходные tests в `/tests` **после завершения агента**,
затем вызывает исходный verifier без изменения assertions. Копирование не влияет
на наблюдения агента. Ошибка staging возвращает reward=None и явную причину.
Общий runner не исправлен этой веткой: запускать N3 именно через wrapper.
Число выполненных тестов и их исходы проверять в `.verifier`, не только reward.

Из этого не следует, что N1 решал задачу: его сохранённые отчёты пустые. Но
валидность live verifier N1 требует отдельной проверки; один reward=0 её не
доказывает. Для нового B сохранённый JSON отдельно проверен оригинальными
assertions с перенаправленным REPORT_PATH: existence PASS, два content FAIL.

## Команды (выполняет агент прогонов)

Из корня основного checkout, при свободном стенде. Если ветка установлена в
другом checkout, заменить только путь к скрипту. Уже существующие out не
переиспользовать. Стенд не перезапускать при чужом активном прогоне.

```sh
sha256sum build/b-deep-base.zip
# 21a43317d6ed9e78473e7334b6a9ed04e21d0dee1cf80af6ed3e1a782d91bf99

python3 .worktrees/n3-source-diagnostic/docs/research/n3-source-diagnostic.py \
  --tasks-root local_task --only find-sqli-login \
  --archive build/b-deep-base.zip \
  --model qwen2.5-coder:7b-instruct-q4_K_M \
  --base-url http://sber-proxy:8000/v1 --api-key ollama \
  --counter-container sber-proxy --timeout-scale 1.0 --repeats 1 \
  --label N3-B-fixed --out build/eval-N3-B-fixed

python3 .worktrees/n3-source-diagnostic/docs/research/n3-source-diagnostic.py \
  --source environment/app/routers/auth.py \
  --tasks-root local_task --only find-sqli-login \
  --archive build/b-deep-base.zip \
  --model qwen2.5-coder:7b-instruct-q4_K_M \
  --base-url http://sber-proxy:8000/v1 --api-key ollama \
  --counter-container sber-proxy --timeout-scale 1.0 --repeats 1 \
  --label N3-A --out build/eval-N3-A
```

B/A используют один архив с native_tools default. Единственное различие входа A —
дословный исходник с путём в конце исходной инструкции. Это assisted diagnostic,
не автономное улучшение public score. В исходнике уже есть комментарий VULNERABLE;
он сохраняется, и успех нельзя приписывать распознаванию дефекта без подсказок.
Ни checker, ни solution, ни ожидаемый JSON не добавляются в инструкцию.

## Что вернуть

- `suite.json`, `results.tsv`, все `logs/*.log`, `.trace`, `.calls`, `.post`,
  `.instruction`, `.candidate`, `.verifier`, `.capture.json`.
- SHA кода/harness/архива, состояние модели и контекста, input/output/total tokens
  (суммы по `.calls`), wall time, endpoint_late, причина каждого нуля.
- Проверка, что первый сохранённый request A содержит исходник целиком и модель
  его использовала; провал доставки/усечение не считать провалом понимания кода.
- Official audit ожидает три теста. Reward=1 без их исполнения не принимать.
  Дополнительно прочитать finding: checker ищет текстовые признаки SQLi и сам
  по себе не доказывает корректный data flow или эксплуатацию.

При A=1 выполнить обратную пару A2→B2 с новыми out. При A=0 разобрать
candidate/trace/verifier до следующего изменения: навигация может объяснять
только часть проблемы. Если исходник не дошёл до модели, опыт невалиден.
Автономный сбор исходников разрабатывать после этого разделения причин.

## Проверки кода

```sh
cd .worktrees/n3-source-diagnostic
python3 -m pytest -q tests/test_n3_diagnostic.py tests/test_eval_suite.py
```

20 passed. До исправления два regression test падали на неверном reward=0;
после исправления оба проходят. Проверено: staging, явная ошибка копирования,
сохранение успешного candidate/verifier, дословный source без checker в input.
Независимый code review: существенных замечаний нет, те же 20 тестов PASS;
ревьюер не запускал модель или контейнеры.
Исправленное размещение tests ещё не проверено новым контейнерным прогоном:
это следующий шаг агента прогонов. Существующий B выполнялся старым wrapper.

Replay сохранённого B без LLM/контейнера (из корня основного checkout):

```sh
python3 - <<'PY'
import importlib.util
from pathlib import Path
path = Path('local_task/find-sqli-login/tests/test_security_report.py')
spec = importlib.util.spec_from_file_location('official', path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.REPORT_PATH = Path('build/eval-N3-B/logs/find-sqli-login.r1.candidate')
for name in sorted(dir(checker)):
    if name.startswith('test_'):
        try:
            getattr(checker, name)()
            print(name, 'PASS')
        except (AssertionError, ValueError) as exc:
            print(name, 'FAIL', str(exc))
PY
```

Исторический B запускался harness-коммитом `8d97cc1`, первой командой пары выше
с `--label N3-B --out build/eval-N3-B`. Его live verifier невалиден; эта команда
указана для происхождения данных, а не для повторного использования дефекта.

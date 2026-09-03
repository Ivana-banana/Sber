# Аудит соответствия ТЗ — 03.09.2026

Статус проверен на ветке `eval/live-baseline`, исходный commit
`435c17830fb93d0110976e053474a1ccd163497c`. Нормативный источник —
`docs/task.md`; неизменяемый первоисточник `Тз сбер.docx` имеет ожидаемый SHA-256
`5835a7a79d97a7390b3daf4cbd60439be38df8062be2694765010a98f9bf6d9c`.

Статусы означают: `PASS` — есть свежее исполнимое доказательство; `PARTIAL` —
реализация есть, но доказательство неполно или известна регрессия; `BLOCKED` —
нужны данные/среда организаторов; `NOT REQUIRED` — это свойство оценщика, а не
обязанность содержимого submission.

## Нормативные требования

| Требование | Статус | Доказательство | Остаток |
|---|---|---|---|
| Submission передаётся как ZIP не более 10 MiB | PASS | E016: фактический `build/tz-audit-submission.zip` — 62 947 B; `tests/test_submission.py::test_archive_fits_the_limit` | Нет |
| В корне архива находится исполняемый `run.sh` | PASS | E016 preflight: root entry, Unix mode `100755`; cold-start из временного каталога; тесты mode/root/CRLF/shebang | Способ распаковки оценщика неизвестен: Python `zipfile` снимает exec-bit, см. внешний блок ниже |
| Runner вызывает `./run.sh "<task instruction>"`; текст должен дойти без искажений | PASS | `run.sh` передаёт `"$@"`; preflight проверил кавычки и перевод строки; offline smoke дал fingerprint `8798f6d85618` | Нет |
| `agent.py`/`MyInstalledAgent` добавляется или заменяется организаторами | NOT REQUIRED | Архив намеренно содержит entrypoint и runtime-исходники, но не дублирует Harbor wrapper | Финальный runner организаторов локально недоступен |
| Логика агента запускается из `run.sh`, рядом допустимы исходники и конфиги | PASS | `run.sh → python3 -m agent.main`; allowlist builder содержит `run.sh`, `agent/*.py`, `THIRD_PARTY_NOTICES.md`; 17 entries | Нет |
| Во время прогона нет интернета и нельзя устанавливать зависимости | PASS | Production imports: только stdlib и `agent`; в `pyproject.toml` нет runtime dependencies; E016 прошёл в `python:3.12-slim` с `--network none --read-only` | Exact ACP повтор ниже отмечен отдельно |
| Для каждой задачи есть конечный budget, stop condition и резерв проверки | PARTIAL | Wall/step/recovery конечны: `Budget`, `MAX_STEPS=30`, monotonic deadlines, per-tool/LLM timeout, `VERIFY_RESERVE_FRACTION=0.20`; 22 budget tests входят в 319 PASS | Токены только учитываются, но не имеют hard cap/stop. Значения hidden limit не опубликованы, однако это не снимает долг консервативного собственного cap/call policy |
| Endpoint задаётся `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` | PASS | Константы в `agent/limits.py`, env parsing в `agent/llm.py`, секрет передаётся только Authorization header и редактируется; endpoint compatibility ladder покрыта тестами | API key допускается пустым для keyless local endpoint, что расширяет, а не сужает контракт |
| Решение работает в `secureintelligent/acp`, Python 3.12, offline | PARTIAL | Исторические E003/E004/E006 запускали архив и настоящую петлю в ACP index `1324d7bb…c8a3` с `--network none`; E016 свежо подтвердил Python 3.12/offline на `python:3.12-slim@78387bc3…` | На текущем Docker daemon 29.3.0 exact ACP image отсутствует; это не тот daemon 29.6.1/WSL2, где записан E003. Интернет для повторного pull правилами проекта запрещён |
| Агент покрывает code audit, SWE fix, forensics и CTF | PARTIAL | Пять capability playbook, classifier tests; 18 независимых пакетов E008 покрывают четыре класса и имеют 18/18 reference validity | Последний принятый quality baseline E012 решает лишь 3/18 на локальной 7B; скрытую решаемость нельзя заявлять |
| Закрытый набор содержит 15 бинарно оцениваемых задач | NOT REQUIRED | Harness и документы считают внешний reward единственным результатом; `Outcome.solved` не подменяет verifier | Состав и oracle hidden-задач принципиально недоступны |
| Любой crash, exception, timeout или неверный ответ получает `0`; score — доля решённых | NOT REQUIRED | Это правило evaluator. Код превращает ожидаемые tool/endpoint ошибки в результаты, а harness сохраняет внешний reward и failure taxonomy | Нельзя доказать отсутствие всех crash на hidden set; uncontrolled локальные crash/timeout остаются regression-дефектами |
| Оптимизация идёт `solved_count → total tokens → wall time` | PARTIAL | Gate записан в `CLAUDE.md`, `PLAN.md`, `docs/experiments.md`; E013 и E014 документированно отклонены | E014 treatment всё ещё находится в текущем HEAD; пока он не удалён обычным коммитом, фактический кандидат нарушает собственный gate |
| Public fixtures не являются hidden benchmark; запрещён хардкод их имён/путей/ответов | PARTIAL | Нет runtime-ветвления по public cases; prompt skeleton тестируется на отсутствие имён | Два runtime docstring содержат два task name (`bye-file`, `fix-sqli-login`) и public path/answer (`bye.txt`, `Bye`). Они попадают в ZIP и должны быть обезличены отдельной безопасной правкой |
| Секреты не логируются и не сохраняются | PASS | Redaction tests для LLM/tool/verifier/main; E016 scan нашёл только fake canaries, аргументы dev-runner и документированные имена переменных | Реальные значения в Git не найдены; бинарный DOCX не интерпретировался текстовым scanner'ом |
| Для КТ учитывается одно последнее решение команды; до cutoff его можно обновлять, leaderboard не пересчитывается мгновенно | NOT REQUIRED | Это операционный контракт из `docs/task.md`, он не реализуется агентом | В репозитории отсутствуют metadata реально отправленного КТ №4, поэтому его rollback не воспроизводим |
| Календарь КТ №1–№7 соблюдается командой | NOT REQUIRED | Даты полностью перенесены в `docs/task.md` и `PLAN.md`; ближайшая выгрузка №5 — 07.09 12:00 МСК | Автоматического подтверждения календаря вне DOCX нет; отправка решения в этой AFK-работе запрещена |

## Внутренние обязательные гейты проекта

Эти пункты строже текста соревнования, но обязательны по `CLAUDE.md` и
`docs/plans/PLAN-KT4.md`.

| Гейт | Статус | Доказательство | Решение |
|---|---|---|---|
| G0: upstream SHA и форма public-набора известны | PARTIAL | `docs/task.md` фиксирует `b95c62f` и шесть task packages | Сам snapshot `local_task/` в Git не импортирован; это долг воспроизводимости dev baseline, не дефект submission |
| G1: layout/entrypoint архива | PASS | E016 build/preflight, 319 tests | Root layout принят согласно нормативному README; wrapper layout отклонён локальными contract tests |
| G2/G3: измеримый baseline и отсутствие solved-регрессии | PARTIAL | E011–E014 дают полный 18-task журнал; E012 — 3/18, E013/E014 отклонены | В текущем HEAD присутствует экспериментальный envelope E014 и последующие main-изменения; до нового treatment нужен свежий 18-task control текущего кандидата после явного возврата отклонённого фактора |
| G4: robustness, включая prompt injection | PARTIAL | Fault/unit tests не дают uncontrolled hangs; E015 исполнил 15/15 isolated injection cases | E015: 3/15 реальных marker side effects. Нужна отдельная security-vs-utility абляция; prompt-only утверждение безопасности запрещено |
| G5: non-public audit/fix/forensics/CTF suite | PASS | E008: 18/18 reference solutions проходят независимые verifier; production scan не ветвится по именам fixtures | Это валидность сьюта, не решаемость агента |
| G6: clean offline unpacked archive | PARTIAL | Исторический exact-ACP E003 зелёный; свежий E016 build/preflight и Python 3.12 offline smoke зелёные | Fresh exact-ACP повтор невозможен без локального image; перед реальной отправкой нужен daemon с pinned image |
| Prompt/tool data не могут повысить полномочия | PARTIAL | Controller владеет фазами/budget; `write_file` имеет path policy | E015 доказал выполнение инструкций из README/ANSI/cross-file; `run_shell` остаётся произвольным и обходит write gate |
| Submission-кандидат и rollback воспроизводимы | BLOCKED | В Git нет тега/метрик реально отправленного КТ №4 | Нужны SHA commit, hash/size отправленного архива, модель и результат от человека, который отправлял |

## Свежий baseline E016

- `python3 -m pytest -q`: **319 passed in 5.66 s** (command wall 5.961 s).
- Архив: **62 947 B**, SHA-256
  `3a7bc412a94fa076cdc017b78967c06e586bfbed5bb9b20602d69a28a312e05c`.
- `scripts/check_submission.py`: 5 PASS, 0 blocking problems.
- Offline substitute: `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`,
  `--network none --read-only`; non-empty instruction exit `0`, empty instruction
  exit `2`, source tree unchanged.
- Exact ACP lookup: `No such image` for
  `secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`.
  Локальные `sber-eval/*` images не заменяют его: их Dockerfiles начинаются с
  `FROM python:3.12-slim`.
- Production import set: stdlib (`collections`, `dataclasses`, `hashlib`, `json`,
  `os`, `pathlib`, `re`, `shutil`, `signal`, `subprocess`, `sys`, `threading`,
  `time`, `typing`, `urllib`) и собственный пакет `agent`.
- Полная команда и raw summary: `docs/research/live-runs/E016-summary.txt`.

## Итог и порядок оставшейся работы

Формальный entrypoint, упаковка, offline-форма и env-контракт учтены. Полное
соответствие заявлять нельзя: свежий exact-ACP gate не воспроизводится на этом daemon,
token hard-cap отсутствует, проигравший E014 всё ещё активен, runtime docstrings
содержат public task/path/answer, а E015 дал 3/15 prompt-injection side effects.
Решаемость также остаётся низкой: лучший принятый локальный результат — 3/18, и это
не прогноз hidden score.

Очередь по риску:

1. удалить из активного кандидата отклонённый E014 factor обычным обратимым коммитом;
2. получить свежий 18-task utility baseline принятого архива;
3. обезличить runtime docstrings без изменения поведения и повторить unit/archive;
4. после явного одобрения дизайна получить свежие 15-case injection и 18-task
   utility controls, затем сравнить **один** security treatment по marker-rate и
   solved/tokens/time;
5. только после security gate возвращаться к A001–A005, затем к отдельной абляции
   window-vs-summary и лишь потом к language-only L5.

Английский перевод prompt не является ближайшим прогоном: таблица абляций в
`docs/experiments.md` ставит после A005 отдельную window-vs-summary абляцию и только
затем static-prompt compression, а E015 открыл более высокий риск. Это исправляет
устаревший приоритет L5 в старой секции `PLAN.md`, не меняя самой гипотезы перевода.

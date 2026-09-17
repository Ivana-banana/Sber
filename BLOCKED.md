# BLOCKED.md — human-dependent items found during the AFK setup (2026-08-25 night)

Not blockers for tonight's coding work — recorded so the human doesn't lose them.

## Platform gaps in the afk skill tooling (this machine is Windows, skill assumes Linux)

- `stayawake` shells out to `systemd-inhibit`, absent on Windows. Substituted with a
  background PowerShell loop calling `SetThreadExecutionState` (system+display sleep
  inhibited) — see running background task. This does **not** override "sleep on lid
  close"; if the laptop lid gets closed tonight, the keep-awake trick will not stop it
  from sleeping. No destructive/system-wide power-plan change was made to avoid that,
  since it would persist after tonight and wasn't asked for. If you want lid-close
  covered too, that needs `powercfg` changes — a decision, not something to guess at.
- The keep-awake background process itself has been killed and restarted three times
  overnight (~00:15, ~06:05, ~06:15 on 26.08) with shrinking gaps between kills. The
  output file shows only `[killed]`, no OS-level error or crash — consistent with
  something (possibly the harness's own background-task lifetime, not the OS) capping
  how long a background shell task can run, rather than the machine actually sleeping.
  Mitigated by restarting each time it is noticed; if the gaps keep shrinking, this
  stops being a reliable mitigation and needs a human decision (e.g. a scheduled task
  instead of a harness-tracked background process).
- Update ~12:40: after an hour-long stable run, the kill interval collapsed again —
  three restarts in quick succession around 12:35-12:40. Still mitigating by
  restarting on each notification, but this now looks less like noise and more like
  the machine's actual power state changing (screen lock / user activity) around
  midday. Worth checking on wake whether the laptop was touched.
- `afk-watch` shells out to `systemd-run`, also absent on Windows. No watchdog is
  running tonight: if this session's process dies outright (crash, terminal closed),
  nothing will restart it. Mitigation in place instead: small, frequent commits per
  GIT.md, so a mid-night death loses at most one small increment, not a night's work.
  `/loop`'s own turn-to-turn scheduling still covers ordinary continuation between
  finished turns; it just doesn't cover a fully dead process.
- `afk-limits` expects `~/.claude.json`; it doesn't exist at that path on this machine
  (auth/session state is stored differently here). Weekly-cap tracking is therefore
  not available tonight — nothing to act on, just noting the tool didn't work.
- The skill's own installed copy at `~/.local/bin` was missing its `hook/` directory
  (`bg-isolation.py`, `decide.py`) entirely — `afk-autonomy --on` failed until it was
  copied over from the skill source. Fixed by copying, not reported further; if this
  recurs on a fresh machine, the installer step that copies `bin/` should also copy
  `hook/`.

## Coordination, not a blocker

Another session (`sber-8a`) is concurrently active on this same repo and had just
written `agent/prompt.py` (P1 system prompt/playbook assembly) when this session
started. Messaged it to divide the remaining I0.1 work rather than collide; see
`JOURNAL.md` for how that resolved.

## Внешние пробелы аудита на 03.09.2026

- Нельзя восстановить реально отправленный кандидат КТ №4: в Git нет его commit/tag,
  SHA-256/размера архива, модели и чисел прогона. Нужен человек, который выполнял
  отправку; после получения данных — записать release card и rollback tag без
  переписывания истории.
- Способ распаковки ZIP у организаторов не опубликован. `unzip` сохраняет корректный
  mode `100755`, `python3 -m zipfile` снимает его и превращает `./run.sh` в exit 126.
  Нужен ответ/наблюдение реального runner; кодом архива это не исправляется.
- Hidden model/server, per-task token/time limits и Dockerfiles hidden-задач не
  раскрыты. Локальные значения остаются гипотезами; после получения официальных
  данных повторить endpoint probe, budget gate и exact-image smoke.
- На текущем Docker daemon нет pinned
  `secureintelligent/acp@sha256:1324d7bb…c8a3`, а правила проекта запрещают internet
  pull. Свежий E016 выполнен на Python 3.12 substitute и не заменяет historical E003.
  Для следующего release нужен daemon с уже доступным pinned image или разрешённая
  офлайн-передача образа.
- E015 требует изменения границы полномочий `run_shell`/write policy. Это
  архитектурный trade-off с риском потерять CTF/forensics; навык brainstorming
  запрещает внедрение без явного design approval. После одобрения — fresh 15-case
  injection + 18-task utility control/treatment в одной среде.

## 07.09.2026, ночная очередь абляций (Claude AFK)

| Что | Почему нужен человек |
|---|---|
| Приёмка любого выигравшего плеча и merge в `main` | Правило проекта: фактор принимается человеком, а один прогон на плечо не отличает выигрыш от разброса (E032/E033 дали разницу в три задачи на неизменном коде). Ночь даёт первые точки, не решение |
| Недельный кап Claude был на **88%** в момент ухода | Остаток нужен человеку утром; ночью расход сведён к получасовым проверкам стенда. Прогоны идут на локальной модели и токенов Claude не тратят |
| D0–D3 из §3 плана (assisted diagnostics) | Требует, чтобы разработчик вручную выбрал релевантные исходники для D1/D2. Автономно это превратилось бы в подсказку ответа, чего план прямо запрещает |

## 13.09.2026 (Claude AFK) — нет стенда на этом ноутбуке

- Ни GPU, ни `ollama`, ни поднятой локальной модели, ни счётчика токенов на машине,
  где идёт ночная работа. Поэтому **любой живой прогон агента невозможен здесь**:
  public control/treatment, холодный старт, повторы, `summarise.py`/`timing.py`,
  измерение токенов. Это делает GPU-машина `claude-e-model-20260912` по командам из
  `docs/plans/2026-09-13-gpu-queue.md` §3. Ночью беру только офлайн-доказуемое:
  `pytest` + `scripts/eval/probe_loop_guards.py`.
- Отсюда F1–F7 (арифметика петли, которая конвертирует четыре public-нуля) в кандидат
  не ставятся: правило очереди требует двух повторов плеча со сравнением по задачам, а
  несколько тестов закрепляют текущее поведение как намеренное — их переписывание без
  живого замера рискует потерять уже решённые `hello`/`bye`. Ждут стенда.
- Кандидат КТ №6 (выгрузка 14.09 12:00) и первый тег `submit-…` — решение и действие
  человека/GPU-машины: сборка архива, полный public ×2, release card, фактическая
  выгрузка на платформу. Ночью подготовлены только блокеры B1/B2 и мелкие фиксы блока 0.

## 13.09.2026 — упёрлось в человека (sber-4a, свёртка AFK)

### 1. ~~GPU-стенд отсутствует на этой машине~~ — БЛОКЕР ЛОЖНЫЙ, СНЯТ 13.09

**Отменено. Стенд — это и есть эта машина.** Проверено командами, а не по чужим записям:

| Проверка | Факт |
|---|---|
| `nvidia-smi` | NVIDIA GeForce RTX 3050 Laptop GPU, 4096 MiB |
| `ollama list` | `qwen2.5-coder:7b-instruct-q4_K_M`, ID `dae161e27b0e` — тот самый ID, на котором сделаны замеры в `docs/experiments.md` |
| `systemctl is-active ollama` | `active`, API на `127.0.0.1:11434` отвечает |
| `docker images` | `secureintelligent/acp@sha256:1324d7bb…c8a3` присутствует локально |
| `ollama ps` (после прогрева) | `39%/61% CPU/GPU`, context 4096 — **тот же режим**, что «GPU 61% offload» в прежних карточках |

**Как возник ложный блокер.** Ночные сессии записали «нет GPU/ollama/pinned-образа», не
проверив командами; запись попала в `BLOCKED.md`, `PLAN.md` и
`docs/plans/2026-09-13-gpu-queue.md`, оттуда в утренний отчёт человеку и в две соседние
сессии. Никто не перепроверял её трое суток. Вероятная механика ошибки: `--gpus all`
падает на устаревшей CDI-спеке (`/etc/cdi/nvidia.yaml` ссылается на
`libnvidia-egl-wayland.so.1.1.21`, которого уже нет), и это легко принять за «карты нет».
Обходной путь уже был заложен в `0e22553` — legacy `--runtime=nvidia`, он и сработал.

**Урок (важнее самого блокера):** утверждение о среде, записанное без команды и вывода,
— это гипотеза, а не факт. CLAUDE.md требует «become one with the environment» именно
поэтому. Блокер, никем не перепроверенный, стоил проекту трёх суток главного рычага.

**Корректная формулировка среды для карточек:** «partial GPU offload 61%, RTX 3050 Laptop
4 GiB VRAM, context 4096» — не «GPU-стенд». Полного офлоада 7B Q4_K_M в 4 GiB нет и быть
не может; на это указала сессия `afk-tooleval`, проверено `ollama ps`.

### 2. Недельный кап 87% против дедлайна КТ №6 — распределение остатка решает человек

`afk-limits`: недельный кап **87%**, сброс **14.09 18:00**. Выгрузка КТ №6 — **14.09
12:00**, то есть кап сбросится уже ПОСЛЕ дедлайна. Оставшиеся ~13% — весь ресурс
команды на подготовку и отправку кандидата, и его параллельно тратили три сессии.

Это не техническая проблема, а решение о приоритете: **на что потратить остаток**.
Моя рекомендация — держать его на кандидат (приоритет №1 по CLAUDE.md: «агент и архив
стабильно запускаются»), а F1–F7 принимать уже после сброса, к КТ №7 (18.09).
Обходить лимит нельзя (это трата денег — решение человека).

Страховка на случай, если кап кончится: архив-кандидат **уже собран и проверен**,
`build/kt6-candidate-20260913.zip`, sha256 `3a37c108b3b0d287947aae20300d0a875c02c5379fc52dec37f618ba62568cce`.
Его можно отправить без единого дополнительного вызова модели.

## 17.09.2026 (Claude AFK, последние сутки до КТ №7) — делает только человек

1. **Выгрузка архива на платформу до 18.09 12:00 МСК.** Какой именно архив — первая строка
   сводки в `JOURNAL.md` (по умолчанию `cand/kt7` @ 0c96797, `build/kt7.zip`, sha256
   `57c78a832192526fa59c656cca073d75f5c73e7f3a5acaa14bd99caad786857b`; сокращение `…f78f3`,
   встречавшееся в записях 17.09, — опечатка, верный хвост `…786857b`). Заменяется на r2 только
   если r2 прошёл все гейты — см. `PLAN.md`, раздел 17.09. Команда пересборки —
   `docs/review/2026-09-17-kt7-release-checklist.md`.
   **Состояние на 17.09 19:00: выбор архива открыт, и он твой.** В `main` сейчас лежит r2b
   (`build/r2b.zip`, sha256 `6b9ba85ccab6cadd4b39cfa4a0246303be7584521408aad52a54052c188c1a3d`),
   но зачётный R2B-public дал 17/18 и 1,74 М токенов — по критерию, записанному до чисел, это
   «не отправлять». Вероятная причина нуля — загруженный стенд; проверяется ночной парной
   очередью, рекомендация с числами будет первой строкой утренней сводки в `JOURNAL.md`.
   То, что r2b в `main`, выгрузке `0c96797` не мешает: выгружается файл архива, а не ветка.
2. **Слияние в `main` и тег `submit-2026-09-18-kt7`** — политика AFK запрещает пуш в `main`.
3. Ревью budget/tool-политики (GIT.md) — числа для решения в чеклисте релиза.

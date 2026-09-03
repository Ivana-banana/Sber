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

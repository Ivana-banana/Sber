# Готовые Agent Skills для AIRI: отбор, адаптация и приоритеты

**Дата проверки:** 25 августа 2026 года.  
**Граница анализа:** в таблицу вошли только открыто лицензированные источники, которые либо применимы к процессу разработки, либо дают сжимаемое доменное содержание для офлайн-агента. Механика Claude Skills **не переносится** в сдаваемый архив: внутри агента остаются только компактные playbook’и и детерминированный код.

> **Ключевой вывод.** Не следует пытаться встроить готовые skills в `submission.zip`. На малой локальной модели их progressive disclosure, многосессионность и зависимости дадут больше накладных расходов, чем пользы. Наиболее ценные находки — это дисциплина **проверяемого agent loop**, **абляционная оценка endpoint**, **artifact preflight** и четыре коротких доменных роутера.

Все значения звёзд и дат коммитов зафиксированы на дату проверки; для строк с отдельным skill они относятся к родительскому репозиторию. Лицензии MIT и Apache-2.0 допускают коммерческое использование и производные работы при соблюдении notices.[1] [2] [3]

## A. Skills для процесса разработки

| Название | Ссылка | Последний коммит | Звёзды | Лицензия | Что делает | Какой наш слот закрывает | Что придётся переписать | Риск |
|---|---|---:|---:|---|---|---|---|---|
| **Agent Harness** | [SKILL.md][4] | 2026-08-21 | 24.9k | MIT | Задаёт контракт `goal → plan → execute/verify → close`, отдельную верификацию с evidence, конечные бюджеты и retry только с изменённым подходом.[4] [5] | Отладка agent loop: budgets, stop conditions, trace, зависания и таймауты. | Убрать multi-session/«human escalation»; реализовать один локальный `state.jsonl`, жёсткий allowlist команд и `max_steps/max_retries/wall_clock`. | **Высокая ценность, средний риск.** Нельзя исполнять команды из task-generated plan/state: исходный skill сам предупреждает, что это trust boundary.[4] |
| **LLM Eval Harness** | [SKILL.md][6] | 2026-08-24 | 1.3k | MIT | Проверяет availability, доставку system prompt/tools/history, скорость, стабильность и quality regression у OpenAI-/Anthropic-compatible endpoint’ов; использует повторные прогоны и отдельные классы ошибок.[6] [7] | Валидация малого OpenAI-compatible endpoint; измеримые токен/время-абляции. | Убрать `uv` и внешние пакеты; написать stdlib-клиент и fake endpoint fixtures. Добавить AIRI-specific ladder: native tools → JSON schema → JSON object → text + конечные retries. | **Высокая ценность, средний риск.** Skill ориентирован на интернетные gateway и более тяжёлый benchmark; на AIRI нужны короткие фиксированные smoke/eval cases. |
| **Autoresearch `/ar:run`** | [SKILL.md][8] | 2026-08-21 | 24.9k | MIT | Ведёт один эксперимент за итерацию: читает историю, меняет один фактор, прогоняет неизменяемый evaluator, фиксирует KEEP/DISCARD/CRASH и откатывает регрессии.[8] | Журнал прогонов, seed/reproducibility, one-factor comparison и классификация провалов; сокращение токенов абляциями. | Заменить git-branch workflow на `experiments.jsonl`; хранить seed, version/sha, prompt hash, task IDs, input/output tokens, elapsed, result и failure class. | **Высокая ценность, низкий риск.** Не переносить scheduled loop и самоизменение промпта без held-out набора. |
| **Ship Gate** | [SKILL.md][9] | 2026-08-21 | 24.9k | MIT | Делает preflight: выбирает применимые проверки, разделяет PASS/FAIL/SKIP/manual и выдаёт блокирующий verdict по severity.[9] | Упаковка и предполётная проверка артефакта сдачи. | Создать отдельный `preflight_submission.sh`: build zip; размер ≤10 MB; `unzip -t`; распаковка в новый temp dir; ровно корневой исполняемый `run.sh`; cold execution; запрет network/install; отчёт с SHA-256. | **Высокая ценность, низкий риск.** Готовые cloud/frontend/database checks почти все нерелевантны; нужна целевая AIRI-реализация, а не копирование skill. |
| **PR Review Expert** | [SKILL.md][10] | 2026-08-21 | 24.9k | MIT (репозиторий) | Дает diff workflow: scope creep, blast radius, secrets/auth/SQLi, test delta, breaking changes и приоритизацию находок.[10] [5] | Ревью изменения против проектных запретов, а не «общая чистота кода». | Вместо GitHub/GitLab PR взять `git diff`; завести 10–15 локальных запретов: no network, no runtime install, no new heavy deps, no uncontrolled shell, no evaluator edits, output contract unchanged, token cap not worsened без ablation. | **Средняя ценность, средний риск.** Нельзя превращать общий 30+ item checklist в обязательный gate: он будет расходовать время и давать нерелевантный шум. |

### Интерпретация категории A

Лучший переносимый принцип здесь не «попросить сильную модель быть внимательнее», а **сделать неверное состояние технически недопустимым**: независимая проверка, конечные бюджеты, артефактный preflight и неизменяемый evaluator. Это особенно важно при ранжировании по solved tasks, токенам и времени.

## B. Доменные знания для сжатия в системные playbook’и агента

| Название | Ссылка | Последний коммит | Звёзды | Лицензия | Что делает | Какой наш слот закрывает | Что придётся переписать | Риск |
|---|---|---:|---:|---|---|---|---|---|
| **Ghost `ghost-scan-code`** | [SKILL.md][11] | 2026-03-11 | 399 | Apache-2.0 | Разделяет SAST на: контекст репозитория → выбор релевантных vectors → план → кандидаты-файлы → анализ → верификация findings → отчёт.[11] [12] | Аудит исходников: инъекции, auth, secrets; порядок действий вместо CWE-списка. | Дистиллировать в 20–28 строк / примерно 260–370 токенов. Оставить только `map sinks/sources → rank files → inspect context → seek evidence → verify`; выкинуть `loop.sh`, cache и multi-agent orchestration. | **Средний риск.** Репозиторий не обновлялся с марта; его scripts рассчитаны на Claude Code. Берите порядок, не исполняемую механику. |
| **SWE-agent minimal-fix workflow** | [default.yaml][13] | активен; SHA `3ea751c` | 20.1k | MIT | Конфигурация предписывает: найти релевантный код, создать reproducer, сделать минимальный fix в исходниках, перезапустить reproducer и проверить edge cases; на submit повторно проверяет reproducer и защищает тестовую целостность.[13] [14] | Минимальный безопасный фикс: воспроизвести → локализовать → исправить причину → проверить. | 18–24 строки / примерно 230–320 токенов. Не запрещать любые test files по имени: вместо этого заморозить evaluator/official tests и показывать diff. | **Высокая ценность, низкий риск.** Не переносить установку/полный framework; small model должна прежде выполнить короткую локальную проверку, а не писать длинное рассуждение. |
| **Linux Log Forensics Investigation** | [SKILL.md][15] | 2026-08-24 | 31k | Apache-2.0 | Инвентаризирует `auth`, syslog, journal, kernel, audit, cron; извлекает successful/failed logins, sudo, account changes и источники brute force, формируя сводку и доказательства.[15] [16] | Форензика по логам: таймлайн, IOC, корреляция, формат ответа. | 25–35 строк / примерно 330–470 токенов. Заменить конкретные пути логов на task discovery, нормализовать timestamp/timezone, вывести `timeline`, `IOCs`, `hypothesis`, `evidence`, `confidence`. | **Средняя ценность, средний риск.** Не предполагать наличие `journalctl`, `auditd`, `ausearch`; сначала проверять доступные артефакты. Порог brute force не должен быть жёсткой истиной. |
| **API Injection Vulnerabilities** | [SKILL.md][17] | 2026-08-24 | 31k | Apache-2.0 | Стартует с инвентаризации входов в headers/path/query/JSON/XML и типов sinks: SQL, NoSQL, command, LDAP и server-side URL fetch.[17] | Аудит инъекций и локальный service probing. | 20–30 строк / примерно 270–400 токенов. Оставить дифференциальные диагностические проверки и поиск root cause в source; убрать SQLMap/Burp/NoSQLMap, payload corpus и всё, что требует сети. | **Средний риск.** Длинные offensive payloads тратят токены и слабо переносятся на закрытый benchmark; нужен дешёвый evidence-first порядок. |
| **CTF Challenge Solver** | [SKILL.md][18] | 2026-08-22 | 3.1k | MIT | Даёт dispatcher: сначала triage файлов/сервиса/hints, затем категория, потом pivot при тупике и проверка найденного флага как доказательства, а не похожей строки.[18] [19] | CTF-подход к локальному сервису: инвентаризация, поверхность атаки, дешёвые гипотезы перед дорогими. | 18–25 строк / примерно 240–340 токенов. Оставить `file/strings/headers/ports/source/hints`, category routing, `simpler path first`, pivot; убрать CTFd API, WebSearch, installer и удалённые сервисы. | **Высокая ценность, средний риск.** Не вклеивать каталоги pwn/crypto техник в системный prompt: маршрутизатор должен загрузить только один мини-playbook по наблюдаемым признакам. |

### Интерпретация категории B

Для закрытого AIRI-agent нужен не большой «кибербезопасностный prompt», а **роутер + четыре коротких режима**: `code_audit`, `secure_patch`, `log_forensics`, `local_ctf`. В каждом режиме ограничьте шаги и заставьте модель фиксировать наблюдение перед действием. Суммарный resident контекст всех четырёх роутеров стоит удержать примерно в диапазоне **1,100–1,500 токенов**, а углублённые чеклисты загружать только после классификации задачи.

## Топ-5: что внедрять первым

| Приоритет | Внедрение | Оценка адаптации | Почему первым | Конкретный эффект |
|---:|---|---:|---|---|
| 1 | **Свой Submission Preflight** по паттерну Ship Gate | 4–6 ч | Форматная ошибка в zip/run.sh/offline environment превращает хорошую логику в 0 баллов; проверяется детерминированно. | **Решённые задачи:** защищает все 15 от инфраструктурного нуля. **Время:** резко снижает last-mile debugging. |
| 2 | **Минимальный loop controller** по Agent Harness | 6–10 ч | Контролирует самый дорогой режим сбоя малой модели: повтор одинакового действия, таймаут и ложное «готово». | **Токены и время:** уменьшает холостые retry; **качество:** каждый успех привязан к evidence. |
| 3 | **Endpoint contract + eval suite** по LLM Eval Harness | 8–12 ч | Нельзя проектировать tool protocol, пока не проверено, доставляет ли endpoint system/tools/history и как считает токены. | **Решённые задачи:** избегает несовместимого protocol design. **Токены/время:** даёт baseline для выбора режимов и лимитов. |
| 4 | **`secure_patch` playbook** из SWE-agent + **`code_audit` triage** из Ghost | 3–6 ч | Это самая близкая связка к security-SWE классу: сначала доказать defect, затем минимально исправить и проверить. | **Решённые задачи:** повышает шанс на patch tasks. **Токены:** отсекает широкие бессистемные сканы. |
| 5 | **`local_ctf` и `log_forensics` роутеры** из ctf-skills и forensic skill | 4–6 ч | Добавляет ширину на два остальных класса без большого framework’а. | **Решённые задачи:** расширяет coverage CTF/log задач. **Время:** дешёвый triage до дорогих гипотез. |

> **Решение по стратегии.** В первую ночь соберите preflight, endpoint smoke fixtures и loop-state. Только после этого добавляйте доменные playbook’и и измеряйте каждый одним фактором на фиксированном локальном наборе. Иначе команда не сможет отличить прирост от флуктуации endpoint или изменения среды.

## Чего готового не нашлось

| Слот | Статус | Из чего собрать своё |
|---|---|---|
| Точный AIRI preflight: zip ≤10 MB, executable root `run.sh`, проверка распакованной копии, secureintelligent/acp, no-install/no-network | **Не найден.** Общие ship/release skills есть, но не знают контракт AIRI. | Bash/Python stdlib script: build → `du -b` → `unzip -t` → extract to temp → assert root layout/mode → run smoke under blocked network → persist report and hashes. |
| Clean-container/offline validation без Docker/download во время прогона | **Не найден.** Testcontainers и release skills предполагают доступную инфраструктуру/образы. | Локальный image/VM runner, заранее собранный в development; test matrix для пустого `$HOME`, read-only deps, выключенного DNS и отсутствия pip/npm. |
| Fallback ladder native tool calls → JSON schema → JSON object → text + robust parsing malformed small-model output | **Не найден.** Есть endpoint-eval skills, но не готовая встраиваемая деградация под малую offline model. | Один адаптер с capability probe; Pydantic-free/stdlib parser; extract-first-JSON, schema validation, repair-free retry максимум 2, затем plain-text action grammar; все ветки покрыть fixture responses. |
| Token compression именно под small local model без потери качества | **Не найден.** Есть метод эксперимента, но не доказанный universal compression skill. | Locked evaluator + Autoresearch ledger: менять один блок prompt/инструмент за раз; хранить accuracy, tokens, latency; принимать только Pareto-improving варианты. |
| Универсальная форензика по произвольным форматам логов и артефактам в 15–40 строк | **Не найден.** Найден Linux-centered workflow, но не единый playbook для web/proxy/app/Windows artifacts. | Свой общий output schema (`timeline`, `entities`, `IOCs`, `links`, `claim/evidence/confidence`) плюс 3–4 parser routes по наблюдаемому формату. |

## Прямые цитаты, полезные для сжатия в B-playbook

Ниже приведены короткие фрагменты первоисточников. Они показывают **порядок действий**, а не payload corpus; именно их разумно сжимать.

> “Find security issues in a repository. This skill plans which vulnerability vectors to scan, then executes those scans against each project.” — Ghost `scan-code`.[11]

Это превращается в короткое правило: *«Сначала распознай стек и vectors; затем ранжируй файлы; утверждай finding только после проверки контекста.»*

> “Create a script to reproduce the error … Edit the sourcecode … Rerun your reproduce script and confirm that the error is fixed!” — SWE-agent.[13]

Это почти готовое ядро `secure_patch`: *«Не патчи до reproducer; после патча запускай тот же reproducer; затем проверяй крайние случаи.»*

> “Forensic analysis of these logs enables investigators to reconstruct user sessions, identify unauthorized access, detect privilege escalation, trace lateral movement, and establish comprehensive event timelines.” — Linux Log Forensics skill.[15]

Это превращается в структуру ответа: *«Таймлайн → субъекты/IOC → цепочка доступа/эскалации → утверждение только с исходными строками evidence.»*

> “Explore files — List the challenge directory, run `file *` on everything” и “Simplify — If an exploit is too complex, check if there’s a simpler path.” — CTF Challenge Solver.[18]

Это полезный anti-thrashing принцип: *«Инвентаризация до эксплуатации; default creds/config/source/logic bug раньше сложной цепочки.»*

> “Never adjudicate your own verification.” и “Budgets are terminal states, not suggestions.” — Agent Harness.[4]

Это не B-доменное знание, а обязательное правило контроллера: *«Верификация выполняется кодом; исчерпанный budget — терминальный failure class, а не success.»*

## Отбор и исключения

Из исследования исключены источники, которые не проходят категориальные или лицензионные фильтры. В частности, архивированный CAI содержит proprietary additions для **non-commercial research only**, поэтому его содержание не включено, несмотря на тематическую релевантность.[20] Официальный `anthropics/skills` полезен как эталон формата, но его опубликованные document/API skills не закрывают AIRI slots, поэтому он тоже не занимает строку в таблицах.[21]

## References

[1]: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/LICENSE "MIT license — alirezarezvani/claude-skills"
[2]: https://raw.githubusercontent.com/daymade/claude-code-skills/main/LICENSE "MIT license — daymade/claude-code-skills"
[3]: https://github.com/ghostsecurity/skills "Ghost Security Skills — Apache-2.0"
[4]: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/engineering/agent-harness/skills/agent-harness/SKILL.md "Agent Harness SKILL.md"
[5]: https://api.github.com/repos/alirezarezvani/claude-skills/commits/main "Latest commit — alirezarezvani/claude-skills"
[6]: https://raw.githubusercontent.com/daymade/claude-code-skills/main/llm-eval-harness/SKILL.md "LLM Eval Harness SKILL.md"
[7]: https://github.com/daymade/claude-code-skills "daymade Claude Code Skills repository"
[8]: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/engineering/autoresearch-agent/skills/run/SKILL.md "Autoresearch run SKILL.md"
[9]: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/engineering/skills/ship-gate/SKILL.md "Ship Gate SKILL.md"
[10]: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/engineering/skills/pr-review-expert/SKILL.md "PR Review Expert SKILL.md"
[11]: https://raw.githubusercontent.com/ghostsecurity/skills/main/plugins/ghost/skills/scan-code/SKILL.md "Ghost scan-code SKILL.md"
[12]: https://api.github.com/repos/ghostsecurity/skills/commits/main "Latest commit — ghostsecurity/skills"
[13]: https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/config/default.yaml "SWE-agent default configuration"
[14]: https://github.com/SWE-agent/SWE-agent "SWE-agent repository"
[15]: https://raw.githubusercontent.com/mukul975/Anthropic-Cybersecurity-Skills/main/skills/performing-linux-log-forensics-investigation/SKILL.md "Linux Log Forensics Investigation SKILL.md"
[16]: https://api.github.com/repos/mukul975/Anthropic-Cybersecurity-Skills/commits/main "Latest commit — Anthropic-Cybersecurity-Skills"
[17]: https://raw.githubusercontent.com/mukul975/Anthropic-Cybersecurity-Skills/main/skills/exploiting-api-injection-vulnerabilities/SKILL.md "API Injection Vulnerabilities SKILL.md"
[18]: https://raw.githubusercontent.com/ljagiello/ctf-skills/main/solve-challenge/SKILL.md "CTF Challenge Solver SKILL.md"
[19]: https://api.github.com/repos/ljagiello/ctf-skills/commits/main "Latest commit — ctf-skills"
[20]: https://raw.githubusercontent.com/aliasrobotics/CAI/archive/LICENSE "CAI research-use license"
[21]: https://github.com/anthropics/skills "Official Anthropic Skills repository"

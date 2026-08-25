# Проверенные находки — AIRI Agent Skills

Дата проверки: 25 августа 2026 (GMT+3).

## Метод отбора

В итог включать только (A) реально поставляемые пакеты для Claude Code/Codex с воспроизводимым практическим эффектом в процессе разработки, или (B) открыто лицензированные исходные инструкции/репозитории, из которых можно дистиллировать автономный офлайн-playbook. Удалять сетевые/SaaS-обёртки и всё, что нельзя использовать в коммерческом производном продукте. Данные о stars, времени последнего коммита и лицензии брать с GitHub-страниц/лицензий; при конфликте отметить «не проверено».

## Официальный Anthropic skills

- URL: https://github.com/anthropics/skills
- На странице указано: 171k stars, 20.4k forks, 52 commits; последняя видимая фиксация «Update claude-api skill: Python SDK 0.x -> 1.x upgrade guide» 3 дня назад.
- Репозиторий содержит `skills/`, `spec/`, `template/`; пригоден как эталон структуры `SKILL.md`, но видимые готовые навыки посвящены документам и Claude API, а не задачам AIRI.
- Вердикт: не включать отдельной строкой A/B; полезен только как источник формата.

## Ghost Security skills

- URL: https://github.com/ghostsecurity/skills
- На странице указано: 399 stars, 26 forks, 23 commits; latest commit «Enhance criteria» 5 месяцев назад; лицензия Apache-2.0.
- Плагин содержит `plugins/ghost/skills/{proxy,repo-context,report,scan-code,scan-deps,scan-secrets,validate}`.
- `scan-code/SKILL.md`: YAML front matter: `name: ghost-scan-code`, `license: apache-2.0`; задаёт явный pipeline: контекст репозитория → mapping vector’ов → план сканов → номинация файлов → анализ → верификация → отчёт. Встроены лимиты глубины (`quick`, `balanced`, `full`), max 3 одинаковых повторных ошибки и промежуточные артефакты.
- Ограничение: базовая реализация вызывает `scripts/loop.sh` и предполагает полноценный Claude Code; в агент AIRI берётся только порядок, а не исполнение skill.
- Предварительный вердикт: B, аудит исходников / секреты; потенциально A — дисциплина `plan→nominate→analyze→verify`, но одноимённые шины в процессе разработки предпочтительнее выполнять своей лёгкой реализацией.

## Официальный Claude Code plugin code-review

- URL: https://github.com/anthropics/claude-code/tree/main/plugins/code-review
- На странице указано: верхний репозиторий 143k stars, 22.9k forks; видимый коммит `db8834b` от 12 марта 2026 года.
- Плагин состоит из `.claude-plugin`, `commands/`, `README.md`, а не из пакета SKILL.md.
- Из README: `/code-review` сначала проверяет необходимость ревью, собирает проектные CLAUDE.md, суммирует diff, запускает четыре независимых ревью (соответствие CLAUDE.md, очевидные баги, git blame/history), оценивает уверенность 0–100 и отсекает результат ниже 80.
- Ограничение: интеграция с GitHub PR и параллельные субагенты не нужны сдаваемому агенту. Для A рационально извлечь идею review по проектным ограничениям + порог уверенности; нужно переписать в локальную проверку diff без PR.

## Ссылки для дальнейшей проверки категории B

- CAI: https://github.com/aliasrobotics/CAI
- SEC-bench/SWE-agent: https://github.com/SEC-bench/SWE-agent
- SWE-agent: https://github.com/swe-agent/swe-agent
- PurpleLlama: https://github.com/meta-llama/PurpleLlama
- Cybench: https://cybench.github.io/
- NYU CTF Bench: https://nyu-llm-ctf.github.io/

## Нуждается в дальнейшей проверке

- Лицензии и конкретные тексты playbook’ов в CAI, SEC-bench/SWE-agent, SWE-agent и PurpleLlama.
- Свежесть и состав skills у `levnikolaevich/claude-code-skills` и `alirezarezvani/claude-skills`.
- Наличие отдельных, свободно лицензированных reusable skills для упаковки/предполётной проверки, offline container validation, experiment ledger, OpenAI-compatible degradation и token ablation.
- Готовые forensic log workflow’ы в репозиториях агентных бенчмарков.

## Источники

1. https://github.com/anthropics/skills
2. https://github.com/ghostsecurity/skills
3. https://raw.githubusercontent.com/ghostsecurity/skills/main/plugins/ghost/skills/scan-code/SKILL.md
4. https://github.com/anthropics/claude-code/tree/main/plugins/code-review
5. https://api.github.com/repos/ghostsecurity/skills/contents/plugins/ghost/skills?ref=main

## Дополнительные проверенные источники

| Источник | Проверенные факты | Предварительное решение |
|---|---|---|
| `SWE-agent/SWE-agent` — https://github.com/SWE-agent/SWE-agent | GitHub: 20.1k stars, 2.2k forks, 2,182 commits, текущая ветка активно обновляется. Лицензия MIT. `config/default.yaml` задаёт: прочитать релевантный код → создать и запустить reproducer → внести минимальные изменения в non-test файлы → повторить reproducer → проверить edge cases; при submit требует повторного запуска reproducer и возврата любых изменённых тестов. | B: основной источник безопасного минимального workflow патча. Не переносить весь framework. Ужать до 18–24 строк, отказаться от предположения Python-only и от бесконечно длинного reasoning. |
| `mukul975/Anthropic-Cybersecurity-Skills` — https://github.com/mukul975/Anthropic-Cybersecurity-Skills | GitHub: 31k stars, 3.7k forks, 244 commits; latest visible commit около 13 часов назад; лицензия Apache-2.0. У skills есть YAML front matter. `performing-linux-log-forensics-investigation` (Apache-2.0) перечисляет log sources, выделяет auth events / sudo / account changes / cron / auditd и строит отчёт с totals + brute-force source IP. `exploiting-api-injection-vulnerabilities` (Apache-2.0) начинает с инвентаризации points: headers, path, query, JSON/XML; но длинные exploit payloads и внешние инструменты не соответствуют сдаче. | B: два компактных playbook’а: forensic log triage и source/service injection audit. Взять структуру, не payload corpus и не зависимости. Оценка полезной выжимки: 25–35 строк на forensic; 20–30 строк на audit. |
| `ljagiello/ctf-skills` — https://github.com/ljagiello/ctf-skills | GitHub: 3.1k stars, 359 forks, 91 commits; last commit 22 августа 2026; MIT license. `solve-challenge/SKILL.md` разделяет CTF на recon, categorization, category skill, pivot when stuck, evidence-based flag validation. Recon: `file`, `strings`, hex header, доступные сервисы, hints; при тупике — пересмотреть assumptions, искать hidden files/ports/headers/metadata и выбирать simpler path before complex exploit. | B: `local CTF recon + cheap hypotheses`. Изъять сетевую CTFd API-навигацию, web search, installer и весь каталог наступательных техник. Ужать до 18–25 строк. |
| `alirezarezvani/claude-skills` → `engineering/agent-harness` — https://github.com/alirezarezvani/claude-skills/tree/main/engineering/agent-harness | GitHub: 24.9k stars, 3.5k forks, 1,305 commits; MIT. Фактический skill лежит в `engineering/agent-harness/skills/agent-harness/SKILL.md`. Контракт: goal → plan → execute→verify loop → close; retry допускается только с новым подходом; budgets terminal; verify требует evidence и отделён от автора изменений; state хранится в явном файле; есть механические exit codes для vague goal, exhausted loop, unverified close. | A: лучший готовый skill для дисциплины loop и классификации failure. Адаптировать state/manifest под один офлайн-процесс, без multi-agent и human escalation. Для сдаваемого агента переносить только 8–12 коротких правил в системный prompt плюс контроллер кода. |

## Исключённые первичные источники

| Источник | Основание исключения |
|---|---|
| `aliasrobotics/CAI` — https://github.com/aliasrobotics/CAI | Репозиторий архивирован 22 августа 2026, а основной `LICENSE` явно допускает additions только для некоммерческих исследований и запрещает commercial/professional/production use без коммерческой лицензии. Это не проходит жёсткий лицензионный фильтр, поэтому не войдёт в таблицы A/B. |
| `anthropics/skills` — https://github.com/anthropics/skills | Реальный официальный format reference, но видимые production skills сосредоточены на документах и Claude API и не закрывают слоты AIRI. Не включать отдельной находкой. |

## Прямые первоисточники для цитат B

1. SWE-agent default configuration: https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/config/default.yaml
2. Linux log forensics skill: https://raw.githubusercontent.com/mukul975/Anthropic-Cybersecurity-Skills/main/skills/performing-linux-log-forensics-investigation/SKILL.md
3. API injection skill: https://raw.githubusercontent.com/mukul975/Anthropic-Cybersecurity-Skills/main/skills/exploiting-api-injection-vulnerabilities/SKILL.md
4. CTF dispatcher: https://raw.githubusercontent.com/ljagiello/ctf-skills/main/solve-challenge/SKILL.md
5. Agent harness: https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/engineering/agent-harness/skills/agent-harness/SKILL.md

| `alirezarezvani/claude-skills` → `engineering/skills/ship-gate` — https://github.com/alirezarezvani/claude-skills/blob/main/engineering/skills/ship-gate/SKILL.md | MIT. `ship-gate` имеет YAML front matter и порядок: определить релевантный stack → прогнать автоматические проверки в установленном порядке → отделить manual confirmations → выдать CRITICAL/HIGH/ADVISORY verdict. | A: сильный шаблон gate, но его готовые frontend/cloud/database категории в основном лишние. Переписать как `submission_preflight`: build zip → `unzip -t` → распаковать temp copy → проверить root-only `run.sh`, `test -x`, ≤10 MB, no-network/cold-start execution и сохранить отчёт. Адаптация 4–6 ч. |
| `alirezarezvani/claude-skills` → `engineering/skills/pr-review-expert` — https://github.com/alirezarezvani/claude-skills/blob/main/engineering/skills/pr-review-expert/SKILL.md | Структурированный diff workflow: scope creep, blast radius, security patterns (SQLi, auth bypass, secrets), test delta, breaking changes. Однако skill не содержит в front matter явной лицензии; лицензия монорепозитория MIT, но для строгого фильтра это лицензионный риск at-file level. | Не включать как отдельную находку A из-за отсутствия явной лицензии skill. Самая полезная часть — criteria, но её можно составить быстрее самим из требований AIRI и не копировать. |
| `daymade/claude-code-skills` → `llm-eval-harness` — https://github.com/daymade/claude-code-skills/blob/main/llm-eval-harness/SKILL.md | GitHub: 1.3k stars, 215 forks, 703 commits; latest commit 24 августа 2026; repository MIT. Front matter описывает тесты любых LLM за OpenAI-/Anthropic-compatible endpoints: availability, fidelity system/tools/history, speed TTFT/tok/s, concurrency, protocol, regression. В текст skill встроены N=10 sampling, отдельная классификация `no-channel`/`upstream-error`/`empty-content`, проверка доставки system prompt/tool calls и quality regression against accumulated cases. | A: наиболее близкая готовая находка к OpenAI-compatible endpoint validation и токен/временным абляциям. Переписать с `uv`/external dependencies в stdlib Python `urllib` + fixture fake server; добавить required fallback ladder native-tools → JSON schema → JSON object → text and finite retry. Адаптация 8–12 ч. |

## Пробелы после поиска

Не обнаружен готовый свободно лицензированный SKILL.md, который одновременно и буквально проверяет формат AIRI (`.zip ≤10MB`, executable `run.sh` в корне, clean microVM secureintelligent/acp, offline no-install) и пригоден без серьёзной переделки. Не обнаружен отдельный готовый skill с fallback ladder OpenAI tool calling → JSON schema → JSON object → text плюс robust parser для malformed small-model output. Для обоих разумнее написать маленькие project-specific scripts и снабдить их локальными fixture tests.

## Подтверждённые точные даты последних коммитов и лицензии

| Репозиторий | Последний commit (UTC) | Stars при проверке | Лицензия |
|---|---:|---:|---|
| `alirezarezvani/claude-skills` | 2026-08-21 05:23:07Z (`98180daf`) | 24.9k | MIT (raw LICENSE подтверждает unrestricted use/modify/sell при сохранении notice) |
| `daymade/claude-code-skills` | 2026-08-24 (GitHub page; SHA `57864f4`) | 1.3k | MIT (raw LICENSE подтверждает unrestricted use/modify/sell при сохранении notice) |
| `ghostsecurity/skills` | 2026-03-11 12:01:34Z (`3c1875a`) | 399 | Apache-2.0 (в front matter `scan-code`) |
| `mukul975/Anthropic-Cybersecurity-Skills` | 2026-08-24 11:12:25Z (`1b3f6b2`) | 31k | Apache-2.0 (страница и front matter выбранных skills) |
| `ljagiello/ctf-skills` | 2026-08-22 20:31:13Z (`3347ebc`) | 3.1k | MIT (страница репозитория и front matter `solve-challenge`) |
| `SWE-agent/SWE-agent` | дата последнего коммита на странице не была явно извлечена; активен на 25.08.2026, SHA `3ea751c` | 20.1k | MIT (raw LICENSE) |

Для строк, привязанных к конкретному skill в таблице, дата и звёзды относятся к родительскому репозиторию; это будет указано в примечании отчёта.

## Оценка внедрения и приоритетов

| Находка | Категория | Оценка адаптации | Основной KPI | Риск / условие приёма |
|---|---|---:|---|---|
| Agent Harness | A | 6–10 ч | меньше зависаний и ложных «готово», ниже время диагностики | Не переносить shell-exec из plan/state, если файл может быть сформирован задачей; ограничить команды allowlist’ом. |
| LLM Eval Harness | A | 8–12 ч | меньше ошибок protocol/fidelity, токены и время измеряются до переключения стратегии | Убрать `uv`/runtime installs и заменить на stdlib + локальный fake endpoint; качество A/B мерить фиксированным набором задач и seed. |
| Autoresearch `/ar:run` | A | 3–5 ч | меньше регрессий, видимый effect of one factor | Не использовать scheduled loop; собственный TSV/JSONL ledger и locked evaluator. |
| Ship Gate | A | 4–6 ч | предотвращает нулевой балл из-за артефакта/среды, уменьшает последний-day debug time | Переписать почти все checks под AIRI; готовый skill не знает secureintelligent/acp / run.sh / limit 10 MB. |
| Ghost `scan-code` | B | 2–4 ч | больше точных candidate file/vectors при том же token budget | Скрипты loop.sh и весь multi-agent pipeline не переносить. Применять compact sequence и checklist. |
| SWE-agent `default.yaml` | B | 1–2 ч | выше success на security-fix задачах, меньше rewards hacking | Сохранять test integrity, но не догматично запрещать test files: в AIRI задача может требовать test fixture; baseline должен охранять evaluator/integrity checks, а не имя каталога. |
| Anthropic-Cybersecurity-Skills: Linux log forensic | B | 2–3 ч | быстрее локализация IOC/timeline | Не переносить tool-heavy/preinstall steps и фиксированный brute-force threshold без контекста. |
| Anthropic-Cybersecurity-Skills: API injection | B | 2–4 ч | систематичнее SAST/local-service probing | Не переносить полноценный payload corpus и инструменты SQLMap/Burp; использовать diagnostics + доказательство root cause. |
| ctf-skills `solve-challenge` | B | 2–3 ч | меньше дорогих тупиков в CTF/local-service tasks | Изъять CTFd API, WebSearch, installer и outbound assumptions; ограничить recon задачами локального sandbox. |

### Предлагаемый Top-5 порядок

1. **Submission Preflight (своё на основе Ship Gate)**: риск нулевого результата из-за package/runtime формата бинарный и устраняется несколькими проверками; первый полезный эффект за один вечер.
2. **Минимальный loop controller (Agent Harness)**: это фундамент для budgets, terminal stops и separable verification; уменьшает бесконечные попытки и ускоряет анализ таймаутов.
3. **Endpoint contract/eval harness (на основе LLM Eval Harness)**: до реализации task logic нужно эмпирически узнать, работают ли на endpoint system messages, tool schema, history и usage; это защищает от реализации под выдуманный протокол.
4. **Патч playbook (SWE-agent) + audit triage (Ghost)**: покрывает два наиболее близких к SWE/AIRI класса; компактно и сразу закладывает `reproduce → locate → root-cause fix → verify`.
5. **CTF/forensic компактные роутеры (ctf-skills + Anthropic-Cybersecurity-Skills)**: улучшает breadth четырёх классов задач без утяжеления агента; внедрять как поочерёдно под task classifier, не вклеивать все знания в один prompt.

### Вывод о непокрытых слотах

Готового навыка, который буквально закрывает AIRI artifact preflight, clean microVM offline validation или fallback ladder OpenAI-native tools → JSON schema → JSON object → text, не найдено. Здесь «взять skill целиком» хуже, чем собрать маленький project-specific script с фикстурами и branch coverage. Также нет готового skill, который оптимизирует токены именно для малой локальной модели без потери качества; полезный паттерн — locked evaluator + one-factor experiment из Autoresearch, а предметные compressions нужно аблировать самостоятельно.

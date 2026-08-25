# Черновик четырёх playbook'ов и лицензионных notices (выгрузка Manus, запрос M4)

Дата выгрузки: 25.08.2026. Промпт — `docs/prompts/manus-prompts.md`, раздел M4.
Это **сырой ответ Manus без правок**. Наш разбор, перепроверка чисел и лицензий и
решение о том, что из этого идёт в код, — в `docs/research/verdict-playbooks.md`.
Notices в проверенной нами редакции лежат в корневом `THIRD_PARTY_NOTICES.md`.

---

# Playbooks для системного prompt и `THIRD_PARTY_NOTICES.md`

> **Правовая оговорка.** Я не юрист; это рабочая фиксация происхождения и лицензий, а не юридическое заключение. До распространения производной работы её должен проверить квалифицированный юрист.

Все положительные лицензии ниже проверены **25 августа 2026 года** по актуальным репозиторным файлам `LICENSE`, а не по описаниям на GitHub. Для обоих использованных Apache-2.0 репозиториев отдельный файл `NOTICE` в корне не обнаружен: GitHub Contents API вернул HTTP 404.[1] [2]

Токены посчитаны по телу каждого блока (без Markdown-заголовка и ограждения) локальным **GPT-family `cl100k_base`**. Это детерминированный подсчёт с локальной копией официального BPE vocabulary; все playbook’и укладываются в лимит 400 токенов.

## 1. `code-audit`

```text
Картируй стек, входы, конфиги и deps; сохрани component map.
Свяжи HTTP, CLI, файлы и env с handlers; сохрани source map.
Картируй SQL, shell, file, URL, template, auth и secrets; сохрани sink map.
Трассируй source→sink и trust boundaries; получи ranked chains.
Отбери полные chains и critical boundaries; сохрани read queue.
Прочитай calls, validation и config вокруг file; сохрани line evidence.
Проверь identity, authz, encoding, params и allowlist; отметь gaps.
Выбери local safe test или trace; сохрани expected signal.
Выполни test; сохрани output, exit code и effect.
Брось chain, если path closed или два cheap tests пусты; получи next chain.
Подтверди effect independent test, log или trace; сохрани second evidence.
Отдели proven findings от suspicions; сохрани два list.
Для finding сохрани path, source→sink, preconditions, effect и evidence.
Повтори external verification line/effect; сохрани PASS или «не подтверждено».
Заверши с verified report; сохрани status.
```

**Токены:** **305** `cl100k_base`; **15 строк**.

| Источник содержания | Конкретный файл | Лицензия по `LICENSE` | Заимствовано |
|---|---|---|---|
| Ghost Security Skills | [`scan-code/SKILL.md`][3] | [Apache-2.0][4] | Идея и порядок: контекст репозитория → отбор кандидатов → анализ → верификация findings → отчёт. |
| Anthropic Cybersecurity Skills | [`exploiting-api-injection-vulnerabilities/SKILL.md`][5] | [Apache-2.0][6] | Идея карты входов и инъекционно-релевантных стоков. |

Текст, auth/secrets-приоритет, stop rule, локальная граница тестирования, evidence и внешняя проверка сформулированы заново. Дословных фраз, payloads, команд и кода из источников нет.

## 2. `code-fix`

```text
Прочитай contract, исходный diff и limits; сохрани baseline.
Найди code, связанный с symptom; сохрани candidates и lines.
Заморозь tests и evaluator; сохрани diff без их changes.
Создай minimal reproducer; сохрани command и expected failure.
Запусти reproducer до patch; сохрани output, exit code и preconditions.
При пустом result проверь input, version и env; сохрани refined hypothesis.
Брось hypothesis после двух empty reproductions; получи next candidate.
Трассируй data и invariants до cause; сохрани root cause.
Спланируй minimal source patch без смены contract; сохрани scope.
Не меняй и не ослабляй tests, evaluator, expected-answer; сохрани allowed diff.
Внеси patch в cause, не symptom; сохрани small diff.
Повтори reproducer; сохрани исчезновение failure.
Проверь edge cases и security-invariant; сохрани results.
Запусти independent test oracle; сохрани external PASS/FAIL.
Сверь diff с baseline; сохрани отсутствие лишних changes.
При FAIL отбрось hypothesis и локализуй cause; сохрани next step.
Заверши при reproducer и external PASS; сохрани status.
```

**Токены:** **332** `cl100k_base`; **17 строк**.

| Источник содержания | Конкретный файл | Лицензия по `LICENSE` | Заимствовано |
|---|---|---|---|
| SWE-agent | [`config/default.yaml`][7] | [MIT][8] | Порядок: релевантный код → reproducer → минимальная правка исходников → повтор reproducer → крайние случаи; правило не менять тесты. |

Критерий первопричины, security-invariant, внешний oracle, stop rule и запрет подгонки expected-answer сформулированы заново. Дословных фраз из источника нет.

## 3. `forensics`

```text
Сохрани артефакты и хеши; получи manifest.
Инвентаризируй файлы, форматы, хосты, интервалы; получи artifact map.
Проверь журналы и утилиты; получи available list.
Не предполагай journalctl, auditd, ausearch; получи «есть/нет».
Определи timezone, точность и gaps; получи time map.
Нормализуй timestamps с originals; получи rule.
Извлеки time, subject, object, action, raw line; получи events.
Разметь auth, process, network, file, privilege; получи groups.
Построй source-tagged timeline; получи event order.
Извлеки IP, account, hash, domain, path, command; получи IOC.
Коррелируй subject, time, sequence; получи links.
Проверь link в primary artifact; сохрани evidence.
Брось hypothesis без двух sources или при time conflict; получи next hypothesis.
Отдели facts от claims; сохрани alternatives.
Сверь timeline другим parser или artifact; получи confirm/conflict.
Укажи IOC type, value, source, interval; получи list.
Дай claim, evidence, alternatives, confidence; получи conclusion.
Понизь confidence при gaps, conflict, one source; сохрани reason.
Проверь artifact reference каждого claim; получи evidence-check.
```

**Токены:** **342** `cl100k_base`; **19 строк**.

| Источник содержания | Конкретный файл | Лицензия по `LICENSE` | Заимствовано |
|---|---|---|---|
| Anthropic Cybersecurity Skills | [`performing-linux-log-forensics-investigation/SKILL.md`][9] | [Apache-2.0][6] | Идея и порядок инвентаризации логов, разбора authentication/privilege-событий, реконструкции событий и evidence-backed summary. |

Универсальная схема артефактов, нормализация времени, требование проверки реального наличия журналов/утилит, stop rule, confidence и внешний разбор сформулированы заново. Дословных фраз, путей, команд, parser-кода и порогов из источника нет.

## 4. `ctf`

```text
Инвентаризируй files, services, source, hints и commands; получи surface map.
Определи type и role каждого object; получи object table.
Для binary собери strings, headers, arch, protections; получи profile.
Для service проверь ports, protocol, responses, routes; получи snapshot.
Прочитай config, source, comments, metadata; получи hints.
Разметь logic, web, binary, crypto, forensics, misc; получи ranked hypotheses.
Начни с cheapest reversible hypothesis; сохрани expected signal.
Проверь config, local data, input validation, logic bypass; сохрани results.
Сохрани command, input, output попытки; получи journal.
Сравни response с expected signal; получи verdict.
Брось hypothesis после двух cheap empty checks; получи next hypothesis.
Перед complex chain повтори simple-path search; сохрани triage.
При успехе получи artifact, доказывающий mechanism; сохрани evidence.
Проверь artifact повторным run, service response или unique match; получи external confirmation.
Отдели effect от assumptions и decoys; сохрани confirmations.
Зафиксируй steps, commands, inputs, outputs, conditions; получи reproducible write-up.
Повтори проверку с clean state, если возможно; получи final PASS/FAIL.
```

**Токены:** **327** `cl100k_base`; **17 строк**.

| Источник содержания | Конкретный файл | Лицензия по `LICENSE` | Заимствовано |
|---|---|---|---|
| ctf-skills | [`solve-challenge/SKILL.md`][10] | [MIT][11] | Порядок reconnaissance → category → pivot; приоритет простого пути перед сложной цепочкой; проверка результата. |

Локальная граница, stop rule, доказательство механизма, внешняя проверка и clean-state replay сформулированы заново. Дословных фраз, команд, техники по категориям и форматы флагов из источника не использованы.

## Готовый фрагмент `THIRD_PARTY_NOTICES.md`

```markdown
# Third-Party Notices

This file records source provenance for compact, adapted workflow playbooks. The adapted texts are new Russian-language condensations; no source sentence is reproduced verbatim. Keep this file with any distribution containing the adapted playbooks.

## Ghost Security Skills — `plugins/ghost/skills/scan-code/SKILL.md`

- **Source:** https://github.com/ghostsecurity/skills/blob/main/plugins/ghost/skills/scan-code/SKILL.md
- **License:** Apache License 2.0; current repository license file: https://github.com/ghostsecurity/skills/blob/main/LICENSE
- **Copyright notice (verbatim):** “Copyright 2026 Ghost Security”
- **Borrowed:** idea and order of repository context, scan planning, candidate-file nomination, analysis, finding verification, and reporting. No wording copied.
- **NOTICE:** No repository-root `NOTICE` file was found during the 2026-08-25 check (GitHub Contents API: HTTP 404).

## Anthropic Cybersecurity Skills — `skills/exploiting-api-injection-vulnerabilities/SKILL.md`

- **Source:** https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/exploiting-api-injection-vulnerabilities/SKILL.md
- **License:** Apache License 2.0; current repository license file: https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/LICENSE
- **Copyright notice (verbatim):** “Copyright 2026 mukul975”
- **Borrowed:** idea of mapping API input locations and injection-relevant sinks before testing. No wording, payloads, or code copied.
- **NOTICE:** No repository-root `NOTICE` file was found during the 2026-08-25 check (GitHub Contents API: HTTP 404).

## SWE-agent — `config/default.yaml`

- **Source:** https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml
- **License:** MIT License; current repository license file: https://github.com/SWE-agent/SWE-agent/blob/main/LICENSE
- **Copyright notice (verbatim):** “Copyright (c) 2024 John Yang, Carlos E. Jimenez, Alexander Wettig, Shunyu Yao, Karthik Narasimhan, Ofir Press”
- **Borrowed:** order of relevant-code inspection, error reproduction, minimal source fix, rerun, edge-case check, and test-integrity safeguard. No wording copied.

## Anthropic Cybersecurity Skills — `skills/performing-linux-log-forensics-investigation/SKILL.md`

- **Source:** https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/performing-linux-log-forensics-investigation/SKILL.md
- **License:** Apache License 2.0; current repository license file: https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/LICENSE
- **Copyright notice (verbatim):** “Copyright 2026 mukul975”
- **Borrowed:** idea and order of log/artifact inventory, authentication and privilege-event analysis, event reconstruction, and evidence-backed summary. No wording, commands, parser code, or thresholds copied.
- **NOTICE:** No repository-root `NOTICE` file was found during the 2026-08-25 check (GitHub Contents API: HTTP 404).

## ctf-skills — `solve-challenge/SKILL.md`

- **Source:** https://github.com/ljagiello/ctf-skills/blob/main/solve-challenge/SKILL.md
- **License:** MIT License; current repository license file: https://github.com/ljagiello/ctf-skills/blob/main/LICENSE
- **Copyright notice (verbatim):** “Copyright (c) 2026 Lukasz Jagiello”
- **Borrowed:** order of initial reconnaissance, category selection, pivoting, simpler-path preference, and validation of a candidate result. No wording, commands, category catalog, or flag format copied.
```

> При распространении существенных частей Apache-2.0 или MIT-исходников фрагмент provenance **не заменяет** обязательный текст соответствующей лицензии. Для Apache-2.0 также сохраняют применимые notices и repository `NOTICE`, если такой файл присутствует. В проверенных Apache-репозиториях отдельный корневой `NOTICE` не найден.[1] [2]

## Источники, текст из которых брать нельзя

| Источник | Проверенный статус | Правило |
|---|---|---|
| [`anthropics/skills`][12] | В `main` отсутствует `LICENSE`: прямой запрос файла не вернул документ. | Не копируй и не адаптируй текст без отдельного явного разрешения правообладателя. |
| [Alias Robotics CAI — proprietary additions][13] | `LICENSE` разрешает use/copy/modify новых компонентов лишь для **non-commercial research and academic purposes** и прямо запрещает commercial, professional и production use. | Не используй текст proprietary additions в production/коммерческом агенте. Отдельно проверяй конкретный MIT-компонент и его notice. |

## Матрица прав

| Источник | Лицензия по текущему `LICENSE` | Можно ли заимствовать текст? |
|---|---|---|
| Ghost Security Skills — `scan-code` | Apache-2.0 | **Да**, при сохранении применимых copyright/attribution notices, копии Apache-2.0 и `NOTICE`, если он есть. |
| Anthropic Cybersecurity Skills — API Injection | Apache-2.0 | **Да**, на тех же условиях Apache-2.0. |
| SWE-agent — `default.yaml` | MIT | **Да**, при сохранении copyright и permission notice. |
| Anthropic Cybersecurity Skills — Linux Log Forensics | Apache-2.0 | **Да**, на тех же условиях Apache-2.0. |
| ctf-skills — `solve-challenge` | MIT | **Да**, при сохранении copyright и permission notice. |
| `anthropics/skills` | Лицензия отсутствует | **Нет.** Отсутствие лицензии не даёт права копировать или адаптировать текст. |
| Alias Robotics CAI — proprietary additions | Research-Use License | **Нет** для production/коммерческого использования; оцени отдельно конкретный MIT-компонент. |

## References

[1]: https://api.github.com/repos/ghostsecurity/skills/contents/NOTICE "Ghost Security Skills: NOTICE check"
[2]: https://api.github.com/repos/mukul975/Anthropic-Cybersecurity-Skills/contents/NOTICE "Anthropic Cybersecurity Skills: NOTICE check"
[3]: https://github.com/ghostsecurity/skills/blob/main/plugins/ghost/skills/scan-code/SKILL.md "Ghost scan-code SKILL.md"
[4]: https://github.com/ghostsecurity/skills/blob/main/LICENSE "Ghost Security Skills LICENSE"
[5]: https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/exploiting-api-injection-vulnerabilities/SKILL.md "API Injection Vulnerabilities SKILL.md"
[6]: https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/LICENSE "Anthropic Cybersecurity Skills LICENSE"
[7]: https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml "SWE-agent default.yaml"
[8]: https://github.com/SWE-agent/SWE-agent/blob/main/LICENSE "SWE-agent LICENSE"
[9]: https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/performing-linux-log-forensics-investigation/SKILL.md "Linux Log Forensics SKILL.md"
[10]: https://github.com/ljagiello/ctf-skills/blob/main/solve-challenge/SKILL.md "CTF Challenge Solver SKILL.md"
[11]: https://github.com/ljagiello/ctf-skills/blob/main/LICENSE "ctf-skills LICENSE"
[12]: https://github.com/anthropics/skills "anthropics/skills repository"
[13]: https://github.com/aliasrobotics/CAI/blob/archive/LICENSE "CAI LICENSE"

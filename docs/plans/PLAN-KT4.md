# Детальный план до КТ №4

Окно: 24.08.2026 после 13:00 МСК → 31.08.2026 12:00 МСК.
Внутренний freeze: **30.08 в 18:00 МСК**. Выбранная стратегия и обоснование —
`../strategy-audit.md`.

## Критический путь

```text
S0 source snapshot
 ├─ R0 target runtime probe ─┐
 └─ H0 evaluation harness ──┼─ B0 measured example baseline
                            │
B0 ─┬─ C1 budget/controller ├─ I0 integrated hybrid agent
    ├─ T1 compact ACI ──────┤
    └─ V1 verifier/recovery ┘
                              ↓
                 M0 mutations + fault injection
                              ↓
                    A0 one-factor ablations
                              ↓
                P0 clean archive + rollback candidate
                              ↓
                         KT4 submission
```

Нельзя начинать prompt-оптимизацию до B0. Нельзя принимать архитектурное усложнение
до M0. Нельзя собирать финальный zip из dirty worktree.

## Общие правила исполнения

- Перед задачей занять строку в `HANDOFF.md`: владелец, ветка, файлы, проверка.
- Перед правкой общего контракта, после долгого прогона и перед commit/merge/push:
  `git fetch --all --prune`, `git status -sb`, просмотр двух сторон divergence.
- Один логический результат — один commit; зелёный законченный commit сразу push,
  если он не содержит секретов и не ждёт обязательного парного ревью.
- Число без commit/model/image/command не считается результатом.
- В production запрещены имена public tasks и их ответы.

## Ресурсное допущение

Полный параллельный план требует минимум трёх активных владельцев:

1. runtime/packaging;
2. agent/tools/prompt;
3. eval/verifier/security fixtures.

При 4–5 участниках controller, tools и release/docs можно развести отдельно. Если
активных разработчиков двое, один берёт runtime+eval, второй agent+tools; M0.3 и
глубокие A0 сокращаются. Если фактически работает один человек, честный scope КТ №4:
S0/R0/H0/B0 → deadline + bounded shell + verifier reserve → G1/G2/G6. Строить полный
mutation suite и fallback protocol в одиночку за неделю опаснее, чем отправить
простой устойчивый кандидат.

Количество доступных владельцев и назначение трёх обязательных потоков фиксируются
в `HANDOFF.md` не позже конца 24 августа. Неназначенная строка не считается планом.

## Work breakdown

| ID | Задача | Владелец-поток | Depends | Результат | Acceptance check | Commit prefix |
|---|---|---|---|---|---|---|
| S0.1 | импортировать upstream snapshot | runtime | — | `agent/`, `local_task/`, upstream SHA | tree совпадает с `71f9081`; чужой код отдельным commit | `infra:` |
| S0.2 | отделить fixture от task packages | eval | S0.1 | manifest шести tasks | каждый manifest entry имеет `task.toml` | `eval:` |
| S0.3 | проверить два zip layout | runtime | S0.1 | решение root vs wrapper dir | Harbor install smoke обоих, decision записан | `pack:` |
| R0.1 | найти машину с Docker | runtime | — | owner + версии Docker/OS | `docker version`, место на диске | `infra:` |
| R0.2 | pull target image | runtime | R0.1 | tag + immutable digest | `docker image inspect` | `infra:` |
| R0.3 | inventory CLI/packages | runtime | R0.2 | machine-readable manifest | probe запускается offline | `infra:` |
| R0.4 | endpoint capability probe | prompt/eval | R0.2 | tool calling, usage, params | минимальный tool call ×3 | `eval:` |
| H0.1 | reproducible run config | eval | S0.1,R0.2 | одна команда полного public run | повтор из clean checkout | `eval:` |
| H0.2 | metrics extractor | eval | H0.1 | per-task 0/1, tokens, seconds, calls | fixture log даёт ожидаемую таблицу | `eval:` |
| H0.3 | failure classifier | eval | H0.2 | startup/packaging/endpoint/tool/reasoning/verification/timeout | каждый 0 имеет ровно один primary mode | `eval:` |
| B0.1 | example baseline ×3 | eval | H0.1–H0.3,R0.4 | E001 | все шесть tasks, raw log hashes | `eval:` |
| B0.2 | профиль траекторий | eval | B0.1 | wasted calls/output/hangs | top-3 failure causes с числами | `eval:` |
| C1.1 | monotonic deadline | agent | B0.2 | internal clock + finalize reserve | fake clock unit tests | `agent:` |
| C1.2 | step/call budgets | agent | C1.1 | finite state transitions | loop terminates on synthetic repetition | `agent:` |
| C1.3 | repeat detector | agent | C1.2 | block identical failed actions | unit test: third repeat rejected/replanned | `agent:` |
| C1.4 | compact state summary | agent | C1.2 | bounded observation history | size cap test preserves last error | `agent:` |
| T1.1 | locate/list/search/read-range | tools | B0.2 | compact typed observations | large JSONL test finds late sentinel | `tools:` |
| T1.2 | bounded shell | tools | B0.2 | timeout, exit, truncated output | sleeping child killed; no orphan | `tools:` |
| T1.3 | exact write + diff edit | tools | T1.1 | create/replace/patch | exact bytes, syntax-error rollback policy | `tools:` |
| T1.4 | redaction | tools | T1.1,T1.2 | keys and secret-looking output masked | canary never appears in logs | `tools:` |
| V1.1 | extract deliverable contract | verifier | B0.2 | paths/schema/prohibitions/tests brief | six instructions reviewed, no task-name rule | `agent:` |
| V1.2 | verifier ladder | verifier | T1.2,T1.3,V1.1 | exact/schema/targeted/regression/service checks | synthetic fixture per verifier type | `eval:` |
| V1.3 | one recovery pass | verifier | C1.1,V1.2 | concise failure → repair → recheck | injected first failure recovers within reserve | `agent:` |
| P1.1 | base system prompt | prompt | B0.2,C1,T1,V1 | concise loop/playbook/stop policy | snapshot + token count | `prompt:` |
| P1.2 | capability soft routing | prompt | P1.1 | artifact/audit/repair/forensics/general hints | paraphrase set; wrong class keeps all tools | `prompt:` |
| I0.1 | integrate runtime/loop/tools/verifier | integration | C1,T1,V1,P1 | candidate agent | unit + contract suite | `agent:` |
| I0.2 | public run ×3 | eval | I0.1 | E002 vs E001 | no solved regression; failures explained | `eval:` |
| M0.1 | public mutations | eval | S0.2 | ≥18 variants: names/paths/values/wording | production has no fixture identifiers | `eval:` |
| M0.2 | fault injection | eval | I0.1 | hang, empty output, huge output, tool error, endpoint retry, near deadline | no uncontrolled hang/crash | `eval:` |
| M0.3 | cross-capability mini-suite | eval/security | I0.1 | at least audit/fix/forensics + one CTF-like local case | independent verifier per case | `eval:` |
| A0.1 | ACI ablation | eval | M0 | compact tools vs raw bash | same suite/model/budget | `eval:` |
| A0.2 | prompt/budget ablation | eval | M0 | max steps/output/reserve variants | one factor per run | `eval:` |
| A0.3 | decide native tools fallback | architecture | R0.4,M0 | keep native or add text protocol | decision backed by ×3 probe | `docs:` |
| P0.1 | deterministic zip builder | pack | I0.1 | archive + manifest | ≤10 MB; allowed files only | `pack:` |
| P0.2 | unpacked offline gate | pack | P0.1 | clean smoke/full run | actual archive, network disabled | `pack:` |
| P0.3 | release evidence | docs/release | P0.2 | SHA-256, size, commit, metrics | fields complete in experiments | `docs:` |
| P0.4 | tag + submit + verify upload | release | P0.3 | KT4 candidate and rollback tag | downloaded/uploaded artifact hash matches | `pack:` |

`C1`, `T1`, `V1` означают все строки соответствующего блока.

## Дневной порядок

### 24 августа — контракт и инфраструктура

1. S0.1–S0.3.
2. R0.1–R0.3 на машине с Docker.
3. H0.1 skeleton.
4. В конце дня: G1 package smoke хотя бы на example agent.

Если Docker-машина не найдена до 20:00, это красный блокер команды: остальная
разработка идёт, но submission confidence остаётся R0.

### 25 августа — baseline, не новые идеи

1. R0.4 и H0.1–H0.3.
2. B0.1: три полных прогона.
3. B0.2: top failure modes.
4. Только после таблицы распределить C1/T1/V1/P1.

### 26 августа — надёжный controller и ACI

1. Параллельно C1, T1, V1.1.
2. Unit/fault tests в каждом commit.
3. Интеграционный merge не позже 20:00.
4. Короткий public smoke после merge.

### 27 августа — verifier и первый hybrid candidate

1. V1.2–V1.3, P1, I0.1.
2. I0.2: `6 tasks × 3 repeats`.
3. Сравнить результаты КТ №3, если опубликованы, но не менять всё по одному score.

### 28 августа — обобщение

1. M0.1 mutations.
2. M0.2 fault injection.
3. M0.3 минимум один CTF-like case, которого нет в public наборе.
4. Любой новый ноль получает regression fixture до исправления.

### 29 августа — абляции и выбор кандидата

1. A0.1–A0.3.
2. Никаких двух изменений в одном эксперименте.
3. Выбрать основной и rollback candidate.
4. Полный target-image прогон обоих.

### 30 августа — release, freeze 18:00

1. P0.1–P0.3 до 15:00.
2. Независимый участник повторяет build из чистого clone.
3. Отправить до 18:00, записать полученный/downloaded hash.
4. После 18:00 — только доказанный submission blocker.

### 31 августа — контроль, не разработка

1. До 10:00 проверить, что платформа показывает правильный последний submission.
2. Сверить hash/tag/commit.
3. Не заменять зелёный кандидат непроверенной утренней правкой.

## Acceptance gates

| Gate | Условие | Если красный |
|---|---|---|
| G0 Source | upstream SHA и шесть tasks зафиксированы | эксперименты недействительны |
| G1 Contract | archive layout принят Harbor, `run.sh` executable | стоп всей feature-работы |
| G2 Baseline | E001: 6 tasks ×3 с полными метриками | сначала чинить harness |
| G3 Hybrid | E002 не хуже E001 по solved count | откатить причинный блок |
| G4 Robust | ≥80% mutations; 0 uncontrolled hangs/startup/package failures | чинить failure modes, не tokens |
| G5 Generality | suite содержит не-public CTF-like + audit/fix/forensics | нельзя заявлять обобщение |
| G6 Release | clean offline unpacked zip, ≤10 MB, hash повторяем | submission запрещён |

Порог 80% — внутренний go/no-go, не прогноз closed score. Public `6/6` обязателен,
но недостаточен.

## Что режем при отставании

Режем в таком порядке:

1. native-tool fallback, если R0.4 зелёный ×3;
2. дополнительные playbooks;
3. глубокие token ablations;
4. вторую реализацию patch tool;
5. расширение suite сверх минимального G5.

Не режем: G1, deadline/per-tool timeout, verifier reserve, public baseline,
fault injection hangs, clean unpacked archive и rollback candidate.

## Данные для решений после КТ №4

- solved count и failure classes по закрытой КТ;
- разница public/mutation/closed;
- tokens/time по успешным и провальным trajectories;
- самый частый последний неуспешный action;
- доля задач, где verifier успел запуститься;
- доля recovery passes, которые превратили 0 в 1.

По ним строится план КТ №5; текущий план не продлевается механически.

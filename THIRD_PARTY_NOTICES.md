# Third-Party Notices

This file records the provenance of compact workflow playbooks that were condensed
from openly licensed sources. The adapted texts are new condensations written in
Russian; no sentence, command, payload or code fragment of a source is reproduced
verbatim. Keep this file with any distribution that contains the adapted playbooks.

Every license below was verified on 2026-08-25 against the repository `LICENSE` file
itself, not against a third-party summary. The verification commands and their raw
results are recorded in `docs/research/verdict-playbooks.md`.

This is a record of provenance, not legal advice.

## Ghost Security Skills — `plugins/ghost/skills/scan-code/SKILL.md`

- **Source:** https://github.com/ghostsecurity/skills/blob/main/plugins/ghost/skills/scan-code/SKILL.md
- **License:** Apache License 2.0 — https://github.com/ghostsecurity/skills/blob/main/LICENSE
- **Copyright notice (verbatim):** "Copyright 2026 Ghost Security"
- **Borrowed:** the idea and the order of repository context, scan planning,
  candidate-file nomination, analysis, finding verification and reporting.
  No wording copied.
- **NOTICE:** no repository-root `NOTICE` file existed at the 2026-08-25 check
  (raw.githubusercontent.com returned HTTP 404).

## Anthropic Cybersecurity Skills — `skills/exploiting-api-injection-vulnerabilities/SKILL.md`

- **Source:** https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/exploiting-api-injection-vulnerabilities/SKILL.md
- **License:** Apache License 2.0 — https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/LICENSE
- **Copyright notice (verbatim):** "Copyright 2026 mukul975"
- **Borrowed:** the idea of mapping API input locations and injection-relevant sinks
  before testing. No wording, payloads or code copied.
- **NOTICE:** no repository-root `NOTICE` file existed at the 2026-08-25 check
  (raw.githubusercontent.com returned HTTP 404).

## Anthropic Cybersecurity Skills — `skills/performing-linux-log-forensics-investigation/SKILL.md`

- **Source:** https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/skills/performing-linux-log-forensics-investigation/SKILL.md
- **License:** Apache License 2.0 — https://github.com/mukul975/Anthropic-Cybersecurity-Skills/blob/main/LICENSE
- **Copyright notice (verbatim):** "Copyright 2026 mukul975"
- **Borrowed:** the idea and the order of log/artifact inventory, authentication and
  privilege-event analysis, event reconstruction and evidence-backed summary.
  No wording, commands, parser code or thresholds copied.
- **NOTICE:** see the entry above; the same repository, the same 404 result.

## SWE-agent — `config/default.yaml`

- **Source:** https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml
- **License:** MIT License — https://github.com/SWE-agent/SWE-agent/blob/main/LICENSE
- **Copyright notice (verbatim):** "Copyright (c) 2024 John Yang, Carlos E. Jimenez,
  Alexander Wettig, Shunyu Yao, Karthik Narasimhan, Ofir Press"
- **Borrowed:** the order of relevant-code inspection, error reproduction, minimal
  source fix, rerun and edge-case check, and the rule that tests must not be edited
  or weakened. No wording copied.

## ctf-skills — `solve-challenge/SKILL.md`

- **Source:** https://github.com/ljagiello/ctf-skills/blob/main/solve-challenge/SKILL.md
- **License:** MIT License — https://github.com/ljagiello/ctf-skills/blob/main/LICENSE
- **Copyright notice (verbatim):** "Copyright (c) 2026 Lukasz Jagiello"
- **Borrowed:** the order of initial reconnaissance, category selection, pivoting,
  the preference for the simpler path, and validation of a candidate result.
  No wording, commands, category catalogue or flag format copied.

## Sources whose text must not be reused

| Source | Verified status (2026-08-25) | Rule |
|---|---|---|
| [anthropics/skills](https://github.com/anthropics/skills) | No license: `GET /repos/anthropics/skills/license` returned HTTP 404 and no `LICENSE` file is present on `main`. | Do not copy or adapt its text without explicit permission from the rights holder. Absence of a license grants nothing. |
| [Alias Robotics CAI — proprietary additions](https://github.com/aliasrobotics/CAI/blob/archive/LICENSE) | Its license permits use, copying and modification of the new components for non-commercial research and academic purposes only, and forbids commercial, professional and production use. | Do not use the proprietary-additions text in a production or commercial agent. Any specific MIT-licensed component must be assessed separately together with its own notice. |

## Distribution note

Reproducing substantial parts of an Apache-2.0 or MIT source requires shipping the
full license text as well; a provenance record alone does not satisfy that. Only the
order of steps was reused here, so no source license text is bundled. If any source
wording is ever copied into this project, add the corresponding license text next to
this file, and add both files to `INCLUDE_FILES` in `scripts/build_submission.py` so
that they travel with the submission archive.

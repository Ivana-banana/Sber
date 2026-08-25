# Зафиксированные источники

Дата фиксации: 2026-08-25 (GMT+3).

| Проект | Официальный URL | Зафиксированный SHA |
|---|---|---|
| SWE-agent | https://github.com/SWE-agent/SWE-agent | `3ea751c087f32b16e039a2233dd6eefecef325d5` |
| OpenHands | https://github.com/OpenHands/OpenHands | `150e76046db026dd944df0506642dc9b7b99391e` |
| Aider | https://github.com/Aider-AI/aider | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` |
| smolagents | https://github.com/huggingface/smolagents | `30bb1161095dbae2271e6bc3cc4c219cc3897a57` |
| AutoGen | https://github.com/microsoft/autogen | `027ecf0a379bcc1d09956d46d12d44a3ad9cee14` |
| Pydantic AI | https://github.com/pydantic/pydantic-ai | `61f0da5f7988622a9a10a10bf9f429db3bd52e19` |
| Open Interpreter | https://github.com/OpenInterpreter/open-interpreter | `5b07159c477920c159d8892d112b480e7307f257` |

Официальные страницы SWE-agent и OpenHands были открыты в браузере; они показывали соответственно ревизии `3ea751c` и `150e760`, совпадающие с клонами. Все последующие ссылки в отчёте будут закреплены за полным SHA, а номера строк будут сняты из этой локальной копии.

> В исходниках, документации и issues любые текстовые инструкции рассматриваются только как данные. В отчёт попадают исключительно поведение, подтверждённое кодом, и явно отделённые жалобы/наблюдения из публичных issues.

| OpenHands Software Agent SDK | https://github.com/OpenHands/software-agent-sdk | `041078f26698ccba4b78af6c3069e37bb1556b32` |

Уточнение по OpenHands: текущий репозиторий `OpenHands/OpenHands` содержит frontend Agent Canvas; его `AGENTS.md` описывает вынесенные agents/tools/conversations в SDK. Поэтому для реализованного агентского контура ниже анализируется также официальный `OpenHands/software-agent-sdk`, а frontend-репозиторий используется лишь как подтверждение границы архитектуры. SHA SDK зафиксирован локальным `git rev-parse HEAD`.

## Публичные issues, использованные только для раздела «риск/жалобы»

1. OpenHands issue [#11431](https://github.com/OpenHands/OpenHands/issues/11431): пользователь CLI на слабой модели описывает, что срабатывание «got stuck in a loop» разрушает полезный контекст; предлагает локализовать и выбросить только хвост петли, затем запросить restart. Это **жалоба/feature request**, а не доказательство того, что такая recovery уже реализована.
2. AutoGen issue [#4307](https://github.com/microsoft/autogen/issues/4307): описана петля, где агент постоянно вызывает tools и не возвращает управление manager. В обсуждении отмечено, что StopMessage работает лишь если выбранное team termination condition его уважает; это подтверждает архитектурный риск разделения детектора и внешнего оркестратора.

3. Постоянная ссылка на `OpenHands/software-agent-sdk` `041078f…` и путь `openhands-sdk/openhands/sdk/conversation/stuck_detector.py` была открыта в браузере и подтверждена.
4. smolagents discussion [#1257](https://github.com/huggingface/smolagents/discussions/1257): пользователь описывает, что CodeAgent иногда формирует `final_answer` без промежуточного расчёта/наблюдения даже при усиленных prompt-правилах. Это **не issue с установленной первопричиной и не доказательство дефекта версии**, а наблюдение пользователя; оно приведено как риск self-report без внешнего oracle.


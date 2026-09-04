# Четыре потерянных входных файла public fixture

05.09.2026 чистый worktree от `447a0a2` выявил отсутствие четырёх файлов,
которые `local_task/incident-log-forensics/environment/Dockerfile` явно COPY в image.
Импорт `c8fabca` объявляет snapshot upstream `b95c62f`, но глобальное `*.log` в
`.gitignore` исключило эти входные данные из Git. В исходном рабочем каталоге они
есть; это синтетические журналы задания, не логи наших прогонов и не секреты.

Восстановлены точные байты из исходного каталога, без редактирования. Сетевой fetch
upstream для повторной аттестации содержимого не делался: provenance унаследован
от локального snapshot, а не заново подтверждён по upstream. Полные SHA-256:

```
fe6a68e5be3bcfaa4751b9a11c595d8d626d543977de697f8aecdba731145034  auth.log
77a449c706c1bac17bd2b0c14e0c27b5f0ba9205d771644479be9725c1ffd3e8  edge_decisions.log
59d0ba3898e57cb645fa256a1e487df1b3f8c56c356edfd94dd92b7c1997ac58  edge_decisions_fragment.log
f0820a8cc0c55e3213210c74e72c3cd73bb505253ecc8d7e54b2923a6af358a1  proxy_access.log
```

Проверка: отсутствие в `git ls-tree -r 447a0a2`, наличие и совпадение SHA-256 в
`git archive` нового дерева, ровно те же файлы в Docker COPY. Всего 4899 байт.
Независимый AFK-критик просмотрел все четыре файла и подтвердил их роль и отсутствие
боевых credentials. Добавлены явными путями, глобальный ignore не ослаблен.
Архив агента, инструкции, verifier и runtime не менялись.

В E049 exact copies были добавлены в worktree до старта forensic-задачи; поэтому
набор данных совпадает с E048. Исправление отслеживания в Git не является фактором
решаемости. Проверка Docker build/run будет дана завершённым E049, не заранее.

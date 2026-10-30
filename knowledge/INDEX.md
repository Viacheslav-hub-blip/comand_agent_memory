# Индекс предметных знаний: проект «Север» (заявки, решения, платежи, антифрод)

Учебный синтетический проект команды «Север». Данные и организация вымышлены. Ниже — назначение материалов и проверенные внутренние пути.

## wiki/

| Файл | Назначение |
|---|---|
| [wiki/project-north.md](wiki/project-north.md) | Границы проекта, каталоги входов/выходов, форматы данных, известные артефакты сценариев C01/C02/C05/C06, организационные ограничения |
| [wiki/data-model.md](wiki/data-model.md) | Модель данных: `applications`, `decision_events`, `payments`, `fraud_labels`, `rule_hits`; гранулярность и связи |
| [wiki/historical-snapshot.md](wiki/historical-snapshot.md) | Временные оси, срез знания на дату `T`, тай-брейк, режим пересчёта прошлого, границы `DISTINCT` и кратность |
| [wiki/approval-metrics.md](wiki/approval-metrics.md) | `approval_rate` v1 (архив) и v2 (действует), `final_decision_approval_rate`, требования к публикации долей |
| [wiki/money-flows.md](wiki/money-flows.md) | Деньги: `disbursement`, `refund`, чистая выдача по `booked_at`; почему метрики не тождественны |
| [wiki/fraud-evaluation.md](wiki/fraud-evaluation.md) | Оценка антифрода: срез меток, `unknown`, TP/FP, precision/recall/coverage, статус SLA |
| [wiki/legacy-approval.md](wiki/legacy-approval.md) | Статус и дефекты `legacy_approval.sql`: почему его результат непригоден как `approval_rate` |
| [wiki/unknowns-and-source-status.md](wiki/unknowns-and-source-status.md) | Неизвестность против отрицательного класса, горизонт наблюдения, ревизии заключений, статус источника |
| [wiki/environment-and-artifacts.md](wiki/environment-and-artifacts.md) | Наблюдавшееся окружение исполнения, запись артефактов, соглашения по SQL |

## cases/

| Файл | Назначение |
|---|---|
| [cases/historical-state-two-times.md](cases/historical-state-two-times.md) | Восстановление состояния заявки на `T` по `occurred_at` и `loaded_at`; контрольные случаи A006/A001/A007; артефакты C02 |
| [cases/as-of-t-and-recalc.md](cases/as-of-t-and-recalc.md) | Два режима на одну границу бизнес-времени: diff, единственное расхождение A006, метрики в обоих режимах |
| [cases/report-mismatch-diagnosis.md](cases/report-mismatch-diagnosis.md) | Три гипотезы расхождения отчётов, атрибуция по сущностям, разделение 830 и 240 минут |
| [cases/legacy-aggregate-audit.md](cases/legacy-aggregate-audit.md) | Объединённый случай: аудит legacy, разложение остатка 85 000 → 27 000 по именованным причинам, измерение fan-out |
| [cases/definition-versions.md](cases/definition-versions.md) | Две версии определения, разделение знаменателей, переключение версии параметром |
| [cases/decisions-vs-money.md](cases/decisions-vs-money.md) | Раздельность решения и движения денег: выдача 47 000 ₽, возвраты 23 000 ₽, чистая 24 000 ₽ |
| [cases/distinct-limits-and-fanout.md](cases/distinct-limits-and-fanout.md) | Границы `DISTINCT` и декартово размножение при соединении с историей |
| [cases/filter-sensitivity-coincidence.md](cases/filter-sensitivity-coincidence.md) | Совпадение ответа не доказывает корректность среза: 12 / 11 / 11 строк |
| [cases/cardinality-negative-control.md](cases/cardinality-negative-control.md) | Проверка кардинальности с негативным контролем: пять CHECK, ожидаемый FAIL на наивном соединении |
| [cases/missing-row-is-unknown.md](cases/missing-row-is-unknown.md) | Объединённый случай: отсутствие строки и нулевой срез меток — неизвестность, не `legitimate` |
| [cases/verification-and-reuse.md](cases/verification-and-reuse.md) | Проверка, что режим и версия применены фактически, и параметризованный прогон на другой когорте |
| [cases/deadline-obligation.md](cases/deadline-obligation.md) | Срок по данным без утверждённого SLA: D+3 против D+5, база дней, вопросы владельцу |
| [cases/artifact-write-and-verify.md](cases/artifact-write-and-verify.md) | Артефакты в чужой файловой системе: усечение записи, CRLF/LF, сверка исходников |

## skills/

| Файл | Назначение |
|---|---|
| [skills/restore-as-of-state/SKILL.md](skills/restore-as-of-state/SKILL.md) | Восстановление состояния сущности на дату знания `T` и пересчёт того же бизнес-времени; проверенный SQL снимка — в `skills/metric-recalc-definition-versions/references/north-snapshot-sql.md` |
| [skills/metric-recalc-definition-versions/SKILL.md](skills/metric-recalc-definition-versions/SKILL.md) | Пересчёт бизнес-метрики по версиям определения на историческом срезе |
| [skills/audit-legacy-query/SKILL.md](skills/audit-legacy-query/SKILL.md) | Аудит унаследованного запроса и сверка отчётного агрегата: объяснимый мостик и разложение остатка; проверенное разложение — в `references/north-legacy-diff.md` |
| [skills/diagnose-report-mismatch/SKILL.md](skills/diagnose-report-mismatch/SKILL.md) | Диагностика расхождения исторического отчёта и пересчёта: три гипотезы, атрибуция по сущностям |
| [skills/fraud-rule-evaluation/SKILL.md](skills/fraud-rule-evaluation/SKILL.md) | Оценка и сравнение правил антифрода на дату знания: TP/FP/unknown, precision, recall, coverage |
| [skills/assess-deadline-obligation/SKILL.md](skills/assess-deadline-obligation/SKILL.md) | Оценка обязательства по срокам: статус источников, хвост распределения, безопасная формулировка |

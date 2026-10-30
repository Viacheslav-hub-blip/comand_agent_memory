# Проект «Север»: границы, данные, артефакты

Учебный синтетический проект команды «Север»: заявки, решения, платежи, антифрод. Данные и организация вымышлены, сеть и реальные СУБД не используются. Снимок малый (8 заявок): доли и оценки на нём не обосновывают производственные решения.

## Расположение и форматы

- Корень проекта: `/synthetic_team`.
- `inputs/` — CSV-снимки, только чтение; кодировка UTF-8, разделитель — запятая, суммы в целых рублях.
- `documents/` — определения и словарь данных (`data_dictionary.md`, `reporting_v1.md`, `reporting_v2.md`, `pipeline_notes.md`).
- `sql/` — схема и запросы (`schema.sql`, `legacy_approval.sql`, скрипты расчёта и сверки).
- `outputs/<ID сценария>/` — результаты; отдельный каталог на сценарий или когорту, не смешивать.
- Все времена UTC в формате `YYYY-MM-DD HH:MM:SS`; верхняя граница любого интервала не включается.
- Расчёт на CSV: при необходимости создаётся отдельная локальная SQLite по `schema.sql`, CSV загружаются стандартным Python без сетевых зависимостей.

Доступность каталогов зависит от прогона и проверяется заново: в одних прогонах присутствует только `outputs/`, в других доступны и CSV-снимки. Отсутствующий каталог — соглашение о размещении, а не готовый файл; историческая ссылка на артефакт описывает прошлую реализацию, а не гарантию наличия файла. Наблюдения по окружению — в [environment-and-artifacts.md](environment-and-artifacts.md).

## Известные артефакты сценариев

| Сценарий | Каталог | Содержимое по итогам работы |
|---|---|---|
| C01 — сверка «одобрения vs выдача» | `outputs/C01/` | `report.md`, `reconciliation_template.sql` (параметры `t_from`, `t_to`, `t_as_of`, `version`, `money_from`, `money_to`), `run_reconciliation.py`, локальная `sqlite`, `results.csv`, `residual_rows.csv`, `residual_summary.csv` |
| C02 — снимок состояния заявок на `T` | `outputs/C02/` | `findings.md`, `snapshot_application_state.sql`, `application_state_snapshot.csv` |
| C05 — пересчёт по версиям и проверка | `outputs/C05/`, `sql/C05_approval_metrics.py`, `sql/C05_verify.py` | метрики, снимок, `verification.txt` |
| C06 — диагностика расхождения отчётов | `outputs/C06/` | `DIAGNOSTICS.md`, `checks.sql`, `run_checks.py`, `verify_sql.py`, `results.txt`, `results.csv` |
| Пересчёт `approval_rate` на 2026-10-03 | `outputs/approval_rate_2026-10-03/` | результат пересчёта когорты 1–2 октября |

## Организационные границы

- Утверждённого SLA разметки фрода нет: заметки «D+3» и «D+5» в `pipeline_notes.md` — неутверждённые оценки, не правило и не срок. См. [unknowns-and-source-status.md](unknowns-and-source-status.md).
- Дата среза задаётся сценарием, а не SLA.
- Исходные `inputs/`, `sql/`, `documents/` после расчёта должны оставаться неизменными; сверка SHA256 до/после — приемлемая проверка.

См. [data-model.md](data-model.md), [approval-metrics.md](approval-metrics.md).

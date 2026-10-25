# Исторический срез

Проект «Север». Время отчётов — UTC.
Событие видно на T при occurred_at < T и loaded_at < T.
Из видимых событий выбирается последнее по occurred_at DESC, event_id DESC.

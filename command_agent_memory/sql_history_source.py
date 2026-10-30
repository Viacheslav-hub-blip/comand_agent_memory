"""История из SQL через драйвер Python DB-API без зависимости ядра от конкретной БД.

Класс SQLSource наследует группировку сессий RecordSource и переопределяет
records: вход — параметры драйвера, выход — поток строк SELECT как словарей.
Функция json_value переводит значения SQL в представление JSON.
"""
from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
import importlib
import json
import os
from uuid import UUID

from command_agent_memory.conversation_history import RecordSource


def json_value(value):
    """Принимает значение SQL; возвращает JSON-совместимое представление либо TypeError.

    Даты сохраняются в ISO 8601, Decimal и UUID — строками без потери точности.
    Для бинарных значений сохраняется размер; неизвестные типы не маскируются.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[бинарные данные: {len(value)} байт]"
    raise TypeError(f"Неподдерживаемый тип SQL: {type(value).__name__}")


class SQLSource(RecordSource):
    """Принимает настройки DB-API и базовый каталог; предоставляет сессии и события SQL."""

    def records(self) -> Iterator[dict]:
        """Без входов выдаёт строки SELECT; закрывает курсор и откатывает транзакцию чтения.

        driver — импортируемый DB-API модуль; dsn_env — переменная окружения с DSN.
        Формат parameters и connect_kwargs определяется выбранным драйвером.
        Учётная запись БД должна иметь только права чтения.
        """
        driver = self.options.get("driver")
        variable = self.options.get("dsn_env")
        query = self.options.get("query")
        kwargs = self.options.get("connect_kwargs", {})
        if not isinstance(driver, str) or not driver or not isinstance(variable, str) or not variable:
            raise ValueError("Для sql нужны driver и dsn_env.")
        if not isinstance(query, str) or not query.split() or query.split()[0].upper() != "SELECT":
            raise ValueError("Для sql нужен запрос SELECT с явным порядком строк.")
        if not isinstance(kwargs, dict):
            raise ValueError("connect_kwargs должен быть объектом.")
        dsn = os.environ.get(variable)
        if not dsn:
            raise ValueError(f"Не задана переменная подключения {variable}.")
        connection = importlib.import_module(driver).connect(dsn, **kwargs)
        try:
            cursor = connection.cursor()
            try:
                if "parameters" in self.options:
                    cursor.execute(query, self.options["parameters"])
                else:
                    cursor.execute(query)
                if cursor.description is None:
                    raise ValueError("SQL-запрос не вернул столбцы.")
                columns = [item[0] for item in cursor.description]
                if len(set(columns)) != len(columns):
                    raise ValueError("Имена столбцов SQL должны быть уникальны; используйте AS.")
                for row in cursor:
                    yield json.loads(json.dumps(dict(zip(columns, row)), default=json_value))
            finally:
                cursor.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

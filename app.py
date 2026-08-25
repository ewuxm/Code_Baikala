import asyncio
import asyncpg
import requests
import json
import logging
import os
import re
import sys
import time
import html
from getpass import getpass
from typing import Optional

# ============================================================
# CONFIG
# ============================================================

YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "b1gp3mc7fu31gs7lgm0u").strip()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()

# Актуальный REST endpoint Yandex AI Studio Text Generation API.
YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MODEL = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"

DB_HOST = os.getenv("DB_HOST", "185.241.193.203")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "vesna-db4")
DB_USER = os.getenv("DB_USER", "vdb4_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

HOST = "127.0.0.1"
PORT = 8000

STATEMENT_TIMEOUT_MS = 5000
MAX_ROWS = 100
MAX_LLM_ROWS = 50
MAX_QUESTION_LENGTH = 1000
MAX_SQL_REPAIR_ATTEMPTS = 2

# Разрешенные таблицы/views из вашей схемы.
ALLOWED_RELATIONS = {
    "faculties",
    "departments",
    "study_programs",
    "courses",
    "groups",
    "students",
    "teachers",
    "course_assignments",
    "grades",
    "schedule",
    "applicants",
    "admission_statistics",
    "academic_statistics",
    "v_students_by_faculty",
    "v_simple_faculty_stats",
    "v_faculty_search",
    "v_faculty_statistics",
    "v_students_anonymized",
    "v_teachers_anonymized",
}

# Нельзя выдавать идентификаторы/персональные данные студентов.
FORBIDDEN_STUDENT_COLUMNS = {
    "student_code",
}

FORBIDDEN_SQL_PATTERNS = [
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bDROP\b",
    r"\bALTER\b",
    r"\bTRUNCATE\b",
    r"\bCREATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"\bCOPY\b",
    r"\bCALL\b",
    r"\bDO\b",
    r"\bVACUUM\b",
    r"\bANALYZE\b",
    r"\bEXECUTE\b",
    r"\bPREPARE\b",
    r"\bSELECT\s+INTO\b",
    r"\bFOR\s+UPDATE\b",
    r"\bFOR\s+SHARE\b",
    r"\bPG_SLEEP\b",
    r"\bPG_TERMINATE_BACKEND\b",
    r"\bPG_CANCEL_BACKEND\b",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("university_ai")

pool: Optional[asyncpg.Pool] = None
SCHEMA_CACHE: Optional[str] = None


# ============================================================
# CREDENTIALS
# ============================================================

def ensure_yandex_key():
    """
    Не падаем с RuntimeError, если переменная окружения не задана.
    Для локального запуска можно ввести ключ один раз при старте.
    Старый ключ, который был опубликован в чате, использовать нельзя:
    его нужно отозвать и создать новый.
    """
    global YANDEX_API_KEY

    if YANDEX_API_KEY:
        return

    print()
    print("=" * 64)
    print("Не найден YANDEX_API_KEY.")
    print("Введите НОВЫЙ API-ключ Yandex Cloud.")
    print("Он не будет печататься на экране.")
    print("=" * 64)

    YANDEX_API_KEY = getpass("Новый API-ключ: ").strip()

    if not YANDEX_API_KEY:
        raise RuntimeError(
            "API-ключ Yandex Cloud не введен."
        )


# ============================================================
# DATABASE
# ============================================================

async def init_db_pool():
    global pool

    try:
        pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            min_size=1,
            max_size=10,
            command_timeout=15,
        )
    except asyncpg.InvalidPasswordError:
        print()
        print("PostgreSQL отклонил пароль пользователя.")
        entered = getpass("Введите пароль PostgreSQL: ")
        pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=entered,
            min_size=1,
            max_size=10,
            command_timeout=15,
        )

    logger.info("Пул PostgreSQL успешно создан")


async def close_db_pool():
    global pool
    if pool:
        await pool.close()
        pool = None


# ============================================================
# SCHEMA
# ============================================================

async def get_db_schema() -> str:
    global SCHEMA_CACHE

    if SCHEMA_CACHE is not None:
        return SCHEMA_CACHE

    async with pool.acquire() as conn:
        relations = await conn.fetch(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            ORDER BY table_name
            """,
            list(ALLOWED_RELATIONS),
        )

        parts = []

        for rel in relations:
            name = rel["table_name"]

            columns = await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = $1
                ORDER BY ordinal_position
                """,
                name,
            )

            cols = ", ".join(
                f"{c['column_name']} ({c['data_type']})"
                for c in columns
            )

            parts.append(f"- {name}: {cols}")

        foreign_keys = await conn.fetch(
            """
            SELECT
                tc.table_name,
                kcu.column_name,
                ccu.table_name AS foreign_table,
                ccu.column_name AS foreign_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = ANY($1::text[])
            ORDER BY tc.table_name, kcu.column_name
            """,
            list(ALLOWED_RELATIONS),
        )

        fk_text = "\n".join(
            f"- {x['table_name']}.{x['column_name']} -> "
            f"{x['foreign_table']}.{x['foreign_column']}"
            for x in foreign_keys
        )

        SCHEMA_CACHE = (
            "РАЗРЕШЕННЫЕ ТАБЛИЦЫ И ПРЕДСТАВЛЕНИЯ:\n"
            + "\n".join(parts)
            + "\n\nВНЕШНИЕ КЛЮЧИ:\n"
            + (fk_text or "- явных связей не найдено")
        )

        logger.info("Схема БД загружена и закэширована")
        return SCHEMA_CACHE


# ============================================================
# YANDEXGPT
# ============================================================

async def call_yandex(
    messages: list,
    temperature: float = 0.0,
    max_tokens: int = 700,
) -> Optional[str]:
    """
    Вызов YandexGPT без aiohttp.
    requests запускается через asyncio.to_thread(), поэтому
    основной event loop не блокируется.
    """

    ensure_yandex_key()

    payload = {
        "modelUri": YANDEX_MODEL,
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": max_tokens,
        },
        "messages": messages,
    }

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }

    def request_sync():
        try:
            session = requests.Session()

            # Если в Windows выставлен SOCKS-прокси, pip/requests
            # может пытаться использовать его. Для прямого вызова
            # Yandex Cloud отключаем автоматические proxy из окружения.
            session.trust_env = False

            response = session.post(
                YANDEX_URL,
                headers=headers,
                json=payload,
                timeout=30,
            )

            if response.status_code != 200:
                logger.error(
                    "YandexGPT HTTP %s: %s",
                    response.status_code,
                    response.text[:2000],
                )
                return None

            data = response.json()

            return (
                data["result"]["alternatives"][0]
                ["message"]["text"]
                .strip()
            )

        except requests.Timeout:
            logger.error("Таймаут YandexGPT")
            return None

        except requests.RequestException as exc:
            logger.error("Ошибка соединения с YandexGPT: %s", exc)
            return None

        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.error("Некорректный ответ YandexGPT: %s", exc)
            return None

    return await asyncio.to_thread(request_sync)


# ============================================================
# SQL CLEANING / VALIDATION
# ============================================================

def clean_sql(text: str) -> str:
    text = re.sub(
        r"```(?:sql)?\s*(.*?)\s*```",
        r"\1",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"^\s*(SQL|SQL-запрос)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # SQL-комментарии модели нам не нужны.
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--.*?$", " ", text, flags=re.MULTILINE)

    text = " ".join(text.split())
    text = text.rstrip(";").strip()

    return text


def validate_sql(sql: str) -> tuple[bool, str]:
    sql = sql.strip()

    if not sql:
        return False, "Пустой SQL."

    # Только один SELECT.
    if not re.match(r"^\s*SELECT\b", sql, flags=re.IGNORECASE):
        return False, "Разрешены только SELECT."

    # Несколько операторов запрещены.
    if ";" in sql:
        return False, "Несколько SQL-операторов запрещены."

    for pattern in FORBIDDEN_SQL_PATTERNS:
        if re.search(pattern, sql, flags=re.IGNORECASE):
            return False, f"Запрещенная конструкция: {pattern}"

    if re.search(
        r"\b(pg_catalog|information_schema|pg_toast)\.",
        sql,
        flags=re.IGNORECASE,
    ):
        return False, "Системные схемы недоступны."

    # Проверяем FROM/JOIN.
    relations = re.findall(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        sql,
        flags=re.IGNORECASE,
    )

    for relation in relations:
        if relation.lower() not in ALLOWED_RELATIONS:
            return False, (
                f"Таблица/view '{relation}' "
                f"не разрешена."
            )

    # CTE: имена CTE не должны маскировать реальные запрещенные
    # таблицы. Разрешаем только SELECT-CTE, а FROM/JOIN все равно
    # проходят whitelist выше.

    # Нельзя делать SELECT * из students.
    if re.search(r"\bstudents\b", sql, flags=re.IGNORECASE):
        if re.search(
            r"SELECT\s+\*\s+FROM\s+students\b",
            sql,
            flags=re.IGNORECASE,
        ):
            return False, "Нельзя выводить все поля students."

        for column in FORBIDDEN_STUDENT_COLUMNS:
            if re.search(
                rf"\b{column}\b",
                sql,
                flags=re.IGNORECASE,
            ):
                # COUNT(student_code) не нужен и тоже не разрешаем.
                return False, (
                    f"Нельзя использовать персональный идентификатор "
                    f"студента: {column}"
                )

    return True, "OK"


def add_limit(sql: str) -> str:
    if re.search(r"\bLIMIT\s+\d+\b", sql, flags=re.IGNORECASE):
        return sql

    return f"{sql} LIMIT {MAX_ROWS}"


# ============================================================
# SQL GENERATION
# ============================================================

SQL_SYSTEM_PROMPT = """
Ты — эксперт по PostgreSQL и Text-to-SQL для информационной системы университета.

Твоя задача: превратить вопрос пользователя в ОДИН корректный SELECT PostgreSQL.

КРИТИЧЕСКИЕ ПРАВИЛА:

1. Используй только таблицы/views из переданной схемы.
2. Разрешен только SELECT.
3. Никаких INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE.
4. Никаких нескольких SQL-операторов.
5. Не придумывай таблицы или колонки.
6. Не выводи персональные данные студентов.
7. Не выводи student_code.
8. Для вопросов о количестве студентов используй COUNT(*) либо
   готовые агрегаты соответствующих views.
9. "обучается", "учится", "сейчас обучается" = status = 'active'.
10. "всего студентов" без слова "обучается" = общее количество записей
    студентов, если вопрос явно не ограничивает статус.
11. Если вопрос про факультет, предпочитай специальные views:
    v_students_by_faculty,
    v_simple_faculty_stats,
    v_faculty_search,
    v_faculty_statistics.
12. Для факультета Лингвистика учитывай формулировки:
    "лингвистический факультет",
    "лингвистическом факультете",
    "факультет лингвистики",
    "лингвистика".
13. Не подменяй "Лингвистика" другим факультетом.
14. Если вопрос про декана — используй dean_name из faculties.
15. В teachers НЕТ teacher_name. Там есть teacher_code, position, degree.
    Не придумывай ФИО преподавателей.
16. Если вопрос требует список студентов, не показывай ФИО/ID студентов.
    Используй агрегаты или обезличенные views.
17. Для одного числового ответа обязательно дай понятный alias:
    student_count, active_students, faculty_count и т.п.
18. Если пользователь просит количество по факультету, желательно вернуть
    название факультета вместе с количеством.
19. SQL должен быть максимально простым и читаемым.
20. Верни ТОЛЬКО SQL, без Markdown и без объяснений.

ПРИМЕР 1:
Вопрос: "сколько студентов обучается на лингвистическом факультете?"
Правильная логика:
- найти факультет Лингвистика;
- посчитать только active;
- не считать выпускников/отчисленных;
- использовать v_students_by_faculty, если его структура подходит.

ПРИМЕР 2:
Вопрос: "сколько всего студентов?"
Правильная логика:
SELECT COUNT(*) AS student_count
FROM students

ПРИМЕР 3:
Вопрос: "сколько сейчас обучается студентов?"
Правильная логика:
SELECT COUNT(*) AS active_students
FROM students
WHERE status = 'active'
"""


async def generate_sql(question: str) -> Optional[str]:
    schema = await get_db_schema()

    prompt = (
        SQL_SYSTEM_PROMPT
        + "\n\n"
        + schema
        + "\n\nВОПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        + question
        + "\n\nТОЛЬКО SQL:"
    )

    answer = await call_yandex(
        [
            {"role": "system", "text": SQL_SYSTEM_PROMPT},
            {"role": "user", "text": prompt},
        ],
        temperature=0.0,
        max_tokens=500,
    )

    if not answer:
        return None

    sql = clean_sql(answer)

    valid, reason = validate_sql(sql)

    if not valid:
        logger.warning(
            "SQL отклонен: %s | %s",
            reason,
            sql,
        )
        return None

    return add_limit(sql)


async def repair_sql(
    question: str,
    bad_sql: str,
    db_error: str,
) -> Optional[str]:
    """
    Если SQL логически не подошел к реальной схеме,
    YandexGPT получает ошибку PostgreSQL и исправляет запрос.
    """

    schema = await get_db_schema()

    prompt = f"""
Исправь SQL-запрос PostgreSQL.

ВОПРОС:
{question}

СХЕМА:
{schema}

ПРЕДЫДУЩИЙ SQL:
{bad_sql}

ОШИБКА POSTGRESQL:
{db_error}

Правила:
- только один SELECT;
- только разрешенные таблицы/views;
- не использовать персональные идентификаторы студентов;
- не придумывать колонки;
- для "обучается" использовать status = 'active';
- вернуть только исправленный SQL;
"""

    answer = await call_yandex(
        [{"role": "user", "text": prompt}],
        temperature=0.0,
        max_tokens=500,
    )

    if not answer:
        return None

    sql = clean_sql(answer)

    valid, reason = validate_sql(sql)

    if not valid:
        logger.warning(
            "Исправленный SQL отклонен: %s | %s",
            reason,
            sql,
        )
        return None

    return add_limit(sql)


# ============================================================
# SQL EXECUTION
# ============================================================

async def execute_sql(sql: str):
    valid, reason = validate_sql(sql)

    if not valid:
        return None, reason

    started = time.perf_counter()

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # Даже при ошибке/подмене SQL транзакция read-only.
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute(
                    f"SET LOCAL statement_timeout = "
                    f"'{STATEMENT_TIMEOUT_MS}ms'"
                )

                rows = await conn.fetch(sql)

            elapsed_ms = int(
                (time.perf_counter() - started) * 1000
            )

            logger.info(
                "SQL выполнен: %d ms, строк=%d",
                elapsed_ms,
                len(rows),
            )

            return rows, None

        except asyncpg.exceptions.QueryCanceledError:
            return None, (
                "Запрос выполнялся слишком долго "
                "и был автоматически остановлен."
            )

        except asyncpg.PostgresError as exc:
            logger.warning("PostgreSQL: %s", exc)
            return None, str(exc)

        except Exception as exc:
            logger.exception("Неожиданная ошибка БД")
            return None, str(exc)


# ============================================================
# RESULT FORMATTING
# ============================================================

def serialize_rows(rows) -> list[dict]:
    result = []

    for row in rows[:MAX_LLM_ROWS]:
        item = {}

        for key, value in dict(row).items():
            if hasattr(value, "isoformat"):
                value = value.isoformat()

            item[key] = value

        result.append(item)

    return result


def russian_number_word(n: int) -> str:
    n = abs(int(n))
    last_two = n % 100
    last = n % 10

    if 11 <= last_two <= 14:
        return "студентов"

    if last == 1:
        return "студент"

    if 2 <= last <= 4:
        return "студента"

    return "студентов"


def deterministic_answer(question: str, data: list[dict]) -> Optional[str]:
    """
    Для самых важных количественных вопросов ответ строится
    без дополнительной генерации, чтобы число нельзя было исказить.
    """

    if not data or len(data) != 1:
        return None

    row = data[0]
    q = question.lower()

    # Факультет + active_students.
    if "active_students" in row:
        value = row["active_students"]

        try:
            value_int = int(value)
        except (TypeError, ValueError):
            return None

        faculty = row.get("faculty_name")

        if faculty:
            return (
                f"На факультете «{faculty}» обучается "
                f"{value_int} {russian_number_word(value_int)}."
            )

        # Самый частый пример из задания.
        if "лингв" in q:
            return (
                f"На факультете «Лингвистика» обучается "
                f"{value_int} {russian_number_word(value_int)}."
            )

        return (
            f"Сейчас обучается "
            f"{value_int} {russian_number_word(value_int)}."
        )

    # Общее количество студентов.
    if "student_count" in row:
        try:
            value_int = int(row["student_count"])
        except (TypeError, ValueError):
            return None

        return (
            f"Всего в базе данных {value_int} "
            f"{russian_number_word(value_int)}."
        )

    return None


async def generate_human_answer(
    question: str,
    sql: str,
    rows,
) -> str:

    data = serialize_rows(rows)

    if not data:
        return "По вашему запросу данных не найдено."

    exact = deterministic_answer(question, data)
    if exact:
        return exact

    result_json = json.dumps(
        data,
        ensure_ascii=False,
        default=str,
    )

    prompt = f"""
Ты — русскоязычный ассистент университета.

Ответь человеку на вопрос, используя ТОЛЬКО результат БД.

ВОПРОС:
{question}

РЕЗУЛЬТАТ БД:
{result_json}

Правила:
- только русский язык;
- не показывай SQL;
- не показывай технические названия колонок без необходимости;
- не придумывай значения;
- не меняй числа;
- короткий естественный ответ;
- если это количество студентов, скажи, что именно посчитано;
- используй формулировки "обучается", "всего", "на факультете" по смыслу вопроса;
- не говори "модель", "нейросеть", "SQL-запрос вернул";
- не выводи персональные данные студентов.

Ответ:
"""

    answer = await call_yandex(
        [{"role": "user", "text": prompt}],
        temperature=0.1,
        max_tokens=250,
    )

    if answer:
        return answer.strip()

    # Резервный вариант.
    first = data[0]

    if len(first) == 1:
        return str(next(iter(first.values())))

    return "; ".join(
        f"{k}: {v}"
        for k, v in first.items()
    )


# ============================================================
# LOGGING TO query_logs
# ============================================================

async def log_query(
    question: str,
    sql: Optional[str],
    status: str,
    rows_returned: int = 0,
    error_message: Optional[str] = None,
    response_time_ms: Optional[int] = None,
):
    """
    В вашей SQL-схеме предусмотрена query_logs.
    Если таблица временно недоступна, основной ответ пользователю
    все равно не ломается.
    """

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO query_logs (
                    user_id,
                    user_role,
                    question,
                    generated_sql,
                    executed_sql,
                    response_time_ms,
                    rows_returned,
                    status,
                    error_message
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9
                )
                """,
                "local_user",
                "user",
                question,
                sql,
                sql,
                response_time_ms,
                rows_returned,
                status,
                error_message,
            )
    except Exception as exc:
        logger.warning(
            "query_logs не записан: %s",
            exc,
        )


# ============================================================
# MAIN PIPELINE
# ============================================================

async def process_question(question: str) -> dict:
    question = question.strip()

    if not question:
        return {
            "ok": False,
            "error": "Введите вопрос.",
        }

    if len(question) > MAX_QUESTION_LENGTH:
        return {
            "ok": False,
            "error": (
                f"Вопрос слишком длинный. "
                f"Максимум {MAX_QUESTION_LENGTH} символов."
            ),
        }

    started = time.perf_counter()

    try:
        logger.info("Новый вопрос: %s", question)

        sql = await generate_sql(question)

        if not sql:
            await log_query(
                question,
                None,
                "sql_generation_error",
            )
            return {
                "ok": False,
                "error": (
                    "Не удалось безопасно сформировать SQL. "
                    "Попробуйте сформулировать вопрос иначе."
                ),
            }

        last_error = None
        rows = None

        for attempt in range(MAX_SQL_REPAIR_ATTEMPTS + 1):
            rows, last_error = await execute_sql(sql)

            if last_error is None:
                break

            if attempt >= MAX_SQL_REPAIR_ATTEMPTS:
                break

            logger.warning(
                "Попытка исправления SQL %d: %s",
                attempt + 1,
                last_error,
            )

            repaired = await repair_sql(
                question,
                sql,
                last_error,
            )

            if not repaired:
                break

            sql = repaired

        if last_error is not None or rows is None:
            elapsed = int(
                (time.perf_counter() - started) * 1000
            )

            await log_query(
                question,
                sql,
                "execution_error",
                error_message=last_error,
                response_time_ms=elapsed,
            )

            return {
                "ok": False,
                "sql": sql,
                "error": (
                    "Не удалось выполнить запрос к базе данных. "
                    f"Причина: {last_error}"
                ),
            }

        answer = await generate_human_answer(
            question,
            sql,
            rows,
        )

        elapsed = int(
            (time.perf_counter() - started) * 1000
        )

        data = serialize_rows(rows)

        await log_query(
            question,
            sql,
            "success",
            rows_returned=len(rows),
            response_time_ms=elapsed,
        )

        return {
            "ok": True,
            "answer": answer,
            "sql": sql,
            "rows": data,
            "row_count": len(rows),
            "response_time_ms": elapsed,
        }

    except Exception as exc:
        logger.exception("Ошибка обработки вопроса")

        elapsed = int(
            (time.perf_counter() - started) * 1000
        )

        await log_query(
            question,
            None,
            "internal_error",
            error_message=str(exc),
            response_time_ms=elapsed,
        )

        return {
            "ok": False,
            "error": (
                "Произошла внутренняя ошибка. "
                f"{exc}"
            ),
        }


# ============================================================
# WEB UI
# ============================================================

HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>University AI Assistant</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f4f6f8;
    color: #202124;
}
.header {
    background: #ffffff;
    border-bottom: 1px solid #ddd;
    padding: 18px 24px;
    position: sticky;
    top: 0;
    z-index: 2;
}
.header h1 { margin: 0 0 5px; font-size: 22px; }
.header p { margin: 0; color: #666; }
.container {
    max-width: 1000px;
    margin: 0 auto;
    padding: 24px;
}
.chat {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-height: 500px;
}
.message {
    max-width: 85%;
    padding: 14px 16px;
    border-radius: 14px;
    white-space: pre-wrap;
    line-height: 1.45;
}
.user {
    align-self: flex-end;
    background: #dcecff;
}
.assistant {
    align-self: flex-start;
    background: #ffffff;
    border: 1px solid #e1e1e1;
}
.meta {
    margin-top: 10px;
    color: #777;
    font-size: 12px;
}
.details {
    margin-top: 12px;
}
details {
    margin-top: 10px;
}
summary {
    cursor: pointer;
    color: #555;
}
pre {
    background: #f1f3f4;
    padding: 12px;
    border-radius: 8px;
    overflow-x: auto;
    font-size: 12px;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin-top: 10px;
    background: white;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px;
    text-align: left;
}
.composer {
    position: sticky;
    bottom: 0;
    background: #f4f6f8;
    padding: 14px 0 0;
}
.row {
    display: flex;
    gap: 10px;
}
textarea {
    flex: 1;
    resize: vertical;
    min-height: 60px;
    max-height: 180px;
    padding: 12px;
    border: 1px solid #ccc;
    border-radius: 10px;
    font: inherit;
}
button {
    border: 0;
    border-radius: 10px;
    padding: 0 22px;
    font-size: 15px;
    cursor: pointer;
    background: #222;
    color: white;
}
button:disabled {
    opacity: .5;
    cursor: wait;
}
.example {
    margin-top: 8px;
    color: #777;
    font-size: 13px;
}
.error {
    color: #a40000;
}
.loading {
    color: #777;
}
</style>
</head>
<body>
<div class="header">
    <h1>University AI Assistant</h1>
    <p>YandexGPT → Text-to-SQL → PostgreSQL → ответ на русском</p>
</div>

<div class="container">
    <div id="chat" class="chat">
        <div class="message assistant">
            Здравствуйте! Я могу отвечать на вопросы по базе университета.
            Например: «сколько студентов обучается на лингвистическом факультете?»
        </div>
    </div>

    <div class="composer">
        <div class="row">
            <textarea id="question"
                placeholder="Введите вопрос о студентах, факультетах, преподавателях..."
                maxlength="1000"></textarea>
            <button id="send">Отправить</button>
        </div>
        <div class="example">
            Примеры: «сколько всего студентов», «сколько студентов обучается
            на лингвистическом факультете», «кто декан факультета Лингвистика»
        </div>
    </div>
</div>

<script>
const chat = document.getElementById("chat");
const question = document.getElementById("question");
const send = document.getElementById("send");

function addMessage(text, cls) {
    const div = document.createElement("div");
    div.className = "message " + cls;
    div.textContent = text;
    chat.appendChild(div);
    div.scrollIntoView({behavior: "smooth", block: "end"});
    return div;
}

function addResult(result) {
    const div = document.createElement("div");
    div.className = "message assistant";

    const answer = document.createElement("div");
    answer.textContent = result.answer || "";
    div.appendChild(answer);

    if (result.sql) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Показать SQL-запрос";
        details.appendChild(summary);

        const pre = document.createElement("pre");
        pre.textContent = result.sql;
        details.appendChild(pre);
        div.appendChild(details);
    }

    if (Array.isArray(result.rows) && result.rows.length) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Показать результат БД";
        details.appendChild(summary);

        const table = document.createElement("table");
        const keys = Object.keys(result.rows[0]);

        const trHead = document.createElement("tr");
        keys.forEach(k => {
            const th = document.createElement("th");
            th.textContent = k;
            trHead.appendChild(th);
        });
        table.appendChild(trHead);

        result.rows.forEach(row => {
            const tr = document.createElement("tr");
            keys.forEach(k => {
                const td = document.createElement("td");
                td.textContent = row[k] ?? "";
                tr.appendChild(td);
            });
            table.appendChild(tr);
        });

        details.appendChild(table);
        div.appendChild(details);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent =
        "Время обработки: " + (result.response_time_ms ?? "-") + " мс";
    div.appendChild(meta);

    chat.appendChild(div);
    div.scrollIntoView({behavior: "smooth", block: "end"});
}

async function sendQuestion() {
    const text = question.value.trim();
    if (!text) return;

    addMessage(text, "user");
    question.value = "";
    send.disabled = true;

    const loading = addMessage("Обрабатываю запрос…", "assistant loading");

    try {
        const response = await fetch("/api/ask", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({question: text})
        });

        const result = await response.json();
        loading.remove();

        if (!result.ok) {
            addMessage(result.error || "Неизвестная ошибка.", "assistant error");
        } else {
            addResult(result);
        }
    } catch (error) {
        loading.remove();
        addMessage(
            "Не удалось связаться с сервером: " + error.message,
            "assistant error"
        );
    } finally {
        send.disabled = false;
        question.focus();
    }
}

send.addEventListener("click", sendQuestion);

question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendQuestion();
    }
});
</script>
</body>
</html>
"""


async def http_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
):
    reasons = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }

    reason = reasons.get(status, "OK")

    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )

    writer.write(header.encode("utf-8") + body)
    await writer.drain()
    writer.close()

    try:
        await writer.wait_closed()
    except Exception:
        pass


async def http_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        header_bytes = await reader.readuntil(b"\r\n\r\n")
        header_text = header_bytes.decode("iso-8859-1")

        first_line = header_text.split("\r\n", 1)[0]
        parts = first_line.split()

        if len(parts) != 3:
            await http_response(
                writer, 400, "text/plain; charset=utf-8",
                b"Bad Request",
            )
            return

        method, path, _ = parts

        headers = {}
        for line in header_text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        body = b""

        if method == "POST":
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                length = 0

            if length > 100_000:
                await http_response(
                    writer, 400, "text/plain; charset=utf-8",
                    b"Request too large",
                )
                return

            body = await reader.readexactly(length)

        if method == "GET" and path == "/":
            await http_response(
                writer,
                200,
                "text/html; charset=utf-8",
                HTML_PAGE.encode("utf-8"),
            )
            return

        if method == "GET" and path == "/api/health":
            response = json.dumps(
                {"ok": True, "service": "university-ai-assistant"},
                ensure_ascii=False,
            ).encode("utf-8")

            await http_response(
                writer,
                200,
                "application/json; charset=utf-8",
                response,
            )
            return

        if method == "POST" and path == "/api/ask":
            try:
                payload = json.loads(body.decode("utf-8"))
                question = str(payload.get("question", ""))

                result = await process_question(question)

                response = json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")

                await http_response(
                    writer,
                    200 if result.get("ok") else 400,
                    "application/json; charset=utf-8",
                    response,
                )
                return

            except Exception as exc:
                logger.exception("Ошибка /api/ask")

                response = json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

                await http_response(
                    writer,
                    500,
                    "application/json; charset=utf-8",
                    response,
                )
                return

        status = 405 if method not in {"GET", "POST"} else 404

        await http_response(
            writer,
            status,
            "text/plain; charset=utf-8",
            b"Not Found",
        )

    except (asyncio.IncompleteReadError, ConnectionError):
        pass

    except Exception:
        logger.exception("Ошибка HTTP-соединения")

    finally:
        if not writer.is_closing():
            writer.close()


# ============================================================
# CONSOLE MODE
# ============================================================

async def console_mode():
    print("=" * 64)
    print("UNIVERSITY AI ASSISTANT")
    print("YandexGPT → Text-to-SQL → PostgreSQL → Ответ")
    print("=" * 64)

    ensure_yandex_key()
    await init_db_pool()

    print()
    print("Готово. Введите вопрос.")
    print("Для выхода: exit")
    print("-" * 64)

    try:
        while True:
            question = await asyncio.to_thread(
                input,
                "\nВведите вопрос: ",
            )

            if question.strip().lower() in {
                "exit",
                "quit",
                "выход",
            }:
                break

            result = await process_question(question)

            if not result["ok"]:
                print("\nОШИБКА:")
                print(result["error"])
                continue

            print("\nSQL:")
            print(result["sql"])

            print("\nРезультат БД:")
            if result["rows"]:
                for row in result["rows"]:
                    print(row)
            else:
                print("(пустой результат)")

            print("\nОТВЕТ:")
            print(result["answer"])

    finally:
        await close_db_pool()


# ============================================================
# WEB MODE
# ============================================================

async def web_mode():
    print("=" * 64)
    print("UNIVERSITY AI ASSISTANT")
    print("Web chat")
    print("=" * 64)

    ensure_yandex_key()
    await init_db_pool()

    server = await asyncio.start_server(
        http_client,
        HOST,
        PORT,
    )

    print()
    print(f"Чат запущен: http://{HOST}:{PORT}")
    print("Откройте эту ссылку в браузере.")
    print("Для остановки нажмите Ctrl+C.")
    print("-" * 64)

    try:
        async with server:
            await server.serve_forever()
    finally:
        await close_db_pool()


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    try:
        if "--console" in sys.argv:
            asyncio.run(console_mode())
        else:
            asyncio.run(web_mode())
    except KeyboardInterrupt:
        print("\nПрограмма остановлена.")
    except Exception as exc:
        print()
        print("=" * 64)
        print("ОШИБКА ЗАПУСКА")
        print("=" * 64)
        print(exc)


if __name__ == "__main__":
    main()
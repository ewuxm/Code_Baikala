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
import hashlib
import hmac
from getpass import getpass
from typing import Optional

# ============================================================
# CONFIG
# ============================================================

YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()

# Актуальный REST endpoint Yandex AI Studio Text Generation API.
YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MODEL = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "university")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

HOST = "127.0.0.1"
PORT = 8000

STATEMENT_TIMEOUT_MS = 5000
MAX_ROWS = 100
MAX_LLM_ROWS = 50
MAX_QUESTION_LENGTH = 1000
MAX_SQL_REPAIR_ATTEMPTS = 2

# ============================================================
# USER ROLES / ACCESS POLICY
# ============================================================

ROLE_COOKIE = "university_role"
ROLE_SECRET = os.getenv("ROLE_SECRET", "change-me-in-production").encode("utf-8")

if not YANDEX_FOLDER_ID:
    print("Внимание: YANDEX_FOLDER_ID не задан. Укажите его в .env или переменных окружения.")

ROLE_LABELS = {
    "applicant": "Абитуриент",
    "student": "Студент",
    "teacher": "Преподаватель",
}

# Внимание: поскольку пользователь выбирает роль без пароля,
# это режим доступа, а не подтверждение личности. Для production
# роль нужно связывать с реальным аккаунтом/SSO/LDAP.
ROLE_ALLOWED_RELATIONS = {
    "applicant": {
        "faculties", "departments", "study_programs",
        "admission_statistics",
        "v_faculty_search", "v_faculty_statistics",
        "v_simple_faculty_stats",
    },
    "student": {
        # Студент получает весь открытый слой абитуриента.
        "faculties", "departments", "study_programs",
        "admission_statistics",
        # Учебная информация.
        "courses", "groups", "course_assignments", "schedule",
        "grades", "academic_statistics",
        # Преподаватели: ФИО и профессиональная информация доступны студенту.
        "teachers",
        # Статистические/обезличенные представления.
        "v_students_by_faculty", "v_simple_faculty_stats",
        "v_faculty_search", "v_faculty_statistics",
        "v_students_anonymized", "v_teachers_anonymized",
    },
    "teacher": set(),
}

def sign_role(role: str) -> str:
    signature = hmac.new(
        ROLE_SECRET, role.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{role}.{signature}"

def verify_role(value: str) -> Optional[str]:
    try:
        role, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    if role not in ROLE_LABELS:
        return None
    expected = hmac.new(
        ROLE_SECRET, role.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return role if hmac.compare_digest(signature, expected) else None

def get_role_from_headers(headers: dict) -> Optional[str]:
    cookie = headers.get("cookie", "")
    for item in cookie.split(";"):
        item = item.strip()
        if item.startswith(ROLE_COOKIE + "="):
            return verify_role(item.split("=", 1)[1])
    return None

def role_relations(role: str) -> set[str]:
    return ROLE_ALLOWED_RELATIONS.get(role, set())

def role_description(role: str) -> str:
    return {
        "applicant": (
            "Роль: АБИТУРИЕНТ. Доступны только факультеты, направления, "
            "поступление и открытая статистика. Нельзя получать сведения "
            "о студентах, оценках, средних баллах, расписании и преподавателях."
        ),
        "student": (
            "Роль: СТУДЕНТ. Доступны факультеты, направления и сведения о поступлении, "
            "учебная информация, расписание, оценки, академическая статистика и "
            "сведения о преподавателях. Нельзя выдавать персональные данные других студентов."
        ),
        "teacher": (
            "Роль: ПРЕПОДАВАТЕЛЬ. Доступны все relations, разрешенные "
            "приложением, включая сведения о студентах, оценках и академической "
            "статистике. Изменение БД по-прежнему запрещено."
        ),
    }[role]

def validate_role_question(role: str, question: str) -> tuple[bool, str]:
    q = question.lower()

    if role == "applicant":
        blocked = (
            "средн", "оцен", "балл", "grade", "grades",
            "студент", "преподавател", "расписан", "академическ",
            "успеваем",
        )
        if any(x in q for x in blocked):
            return False, (
                "В роли «Абитуриент» этот тип информации недоступен. "
                "Можно спрашивать о факультетах, направлениях и поступлении."
            )

    if role == "student":
        # Не блокируем слова «ФИО/фамилия/имя» вообще: студент имеет право
        # спрашивать ФИО преподавателей. Ограничение персональных данных
        # студентов дополнительно обеспечивается validate_sql().
        blocked = (
            "student_code", "идентификатор студента", "телефон студента",
            "email студента", "почта студента", "паспорт студента",
            "адрес студента",
        )
        if any(x in q for x in blocked):
            return False, (
                "В роли «Студент» персональные данные других студентов недоступны."
            )

    return True, "OK"

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

ROLE_ALLOWED_RELATIONS["teacher"] = set(ALLOWED_RELATIONS)

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
# CANONICAL UNIVERSITY TERMS
# ============================================================

# ВАЖНО: это не просто подсказка для LLM.
# Этот словарь используется ДО выполнения SQL и принудительно
# приводит разговорные/склоненные названия факультетов к тем
# значениям, которые реально лежат в faculties.name.
FACULTY_CANONICAL = {
    "технологический факультет": "Технологический факультет",
    "технологического факультета": "Технологический факультет",
    "технологическом факультете": "Технологический факультет",
    "технологический": "Технологический факультет",
    "технологического": "Технологический факультет",
    "технологическом": "Технологический факультет",
    "техфак": "Технологический факультет",
    "ит": "Технологический факультет",
    "информатика": "Технологический факультет",

    "экономический факультет": "Экономический факультет",
    "экономического факультета": "Экономический факультет",
    "экономическом факультете": "Экономический факультет",
    "экономический": "Экономический факультет",
    "экономического": "Экономический факультет",
    "экономическом": "Экономический факультет",
    "экономика": "Экономический факультет",
    "эконом": "Экономический факультет",

    "математический факультет": "Математический факультет",
    "математического факультета": "Математический факультет",
    "математическом факультете": "Математический факультет",
    "математический": "Математический факультет",
    "математического": "Математический факультет",
    "математическом": "Математический факультет",
    "матфак": "Математический факультет",

    "юридический факультет": "Юридический факультет",
    "юридического факультета": "Юридический факультет",
    "юридическом факультете": "Юридический факультет",
    "юридический": "Юридический факультет",
    "юридического": "Юридический факультет",
    "юридическом": "Юридический факультет",
    "юриспруденция": "Юридический факультет",
    "юрист": "Юридический факультет",
    "право": "Юридический факультет",
    "юрфак": "Юридический факультет",

    "лингвистический факультет": "Лингвистический факультет",
    "лингвистического факультета": "Лингвистический факультет",
    "лингвистическом факультете": "Лингвистический факультет",
    "лингвистическому факультету": "Лингвистический факультет",
    "лингвистический": "Лингвистический факультет",
    "лингвистического": "Лингвистический факультет",
    "лингвистическом": "Лингвистический факультет",
    "лингвистика": "Лингвистический факультет",
    "лингвистики": "Лингвистический факультет",
    "лингвистике": "Лингвистический факультет",
    "факультет лингвистики": "Лингвистический факультет",
    "факультета лингвистики": "Лингвистический факультет",
    "филология": "Лингвистический факультет",
    "филологический": "Лингвистический факультет",
    "иностранные языки": "Лингвистический факультет",
}


def normalize_question_entities(question: str) -> str:
    """
    Нормализует сущности в вопросе пользователя ДО LLM.

    Это улучшает генерацию SQL, но не является единственной защитой:
    canonicalize_sql_literals() ниже дополнительно исправляет уже
    сгенерированный SQL.
    """
    result = question

    # Сначала длинные фразы, чтобы не ломать их частичной заменой.
    aliases = sorted(
        FACULTY_CANONICAL.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    for alias, canonical in aliases:
        result = re.sub(
            rf"(?<![А-Яа-яЁёA-Za-z]){re.escape(alias)}(?![А-Яа-яЁёA-Za-z])",
            canonical,
            result,
            flags=re.IGNORECASE,
        )

    return result


def canonicalize_sql_literals(sql: str) -> str:
    """
    КРИТИЧЕСКИЙ слой.

    Если LLM уже успела сгенерировать:
        faculty_name = 'Лингвистика'

    превращаем это в:
        faculty_name = 'Лингвистический факультет'

    Здесь мы намеренно не заменяем все строки подряд: слово
    "Лингвистика" является легальным названием направления
    study_programs.name, поэтому глобальная замена была бы ошибкой.
    """

    aliases = sorted(
        FACULTY_CANONICAL.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    # 1. Фильтрация по faculty_name в views.
    def replace_faculty_name(match):
        prefix = match.group(1)
        value = match.group(2)
        canonical = FACULTY_CANONICAL.get(value.strip().lower())
        if canonical:
            return f"{prefix}'{canonical}'"
        return match.group(0)

    sql = re.sub(
        r"(\bfaculty_name\s*=\s*)'([^']+)'",
        replace_faculty_name,
        sql,
        flags=re.IGNORECASE,
    )

    # 2. Фильтрация faculties.name.
    # Не трогаем study_programs.name, потому что там "Лингвистика"
    # является настоящим значением.
    def replace_faculties_name(match):
        prefix = match.group(1)
        value = match.group(2)
        canonical = FACULTY_CANONICAL.get(value.strip().lower())
        if canonical:
            return f"{prefix}'{canonical}'"
        return match.group(0)

    sql = re.sub(
        r"(\bfaculties\s*\.\s*name\s*=\s*)'([^']+)'",
        replace_faculties_name,
        sql,
        flags=re.IGNORECASE,
    )

    # 3. Прямой FROM faculties + WHERE name = ...
    # Безопасно обрабатываем только простой SELECT из faculties.
    if re.search(
        r"\bFROM\s+faculties\b",
        sql,
        flags=re.IGNORECASE,
    ):
        def replace_plain_name(match):
            prefix = match.group(1)
            value = match.group(2)
            canonical = FACULTY_CANONICAL.get(value.strip().lower())
            if canonical:
                return f"{prefix}'{canonical}'"
            return match.group(0)

        sql = re.sub(
            r"(\bname\s*=\s*)'([^']+)'",
            replace_plain_name,
            sql,
            flags=re.IGNORECASE,
        )

    return sql


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


def validate_sql(sql: str, role: str = "teacher") -> tuple[bool, str]:
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

    if role not in ROLE_LABELS:
        return False, "Неизвестная роль пользователя."

    allowed_relations = role_relations(role)
    for relation in relations:
        if relation.lower() not in allowed_relations:
            return False, (
                f"В роли «{ROLE_LABELS[role]}» таблица/view "
                f"'{relation}' недоступна."
            )

    # CTE: имена CTE не должны маскировать реальные запрещенные
    # таблицы. Разрешаем только SELECT-CTE, а FROM/JOIN все равно
    # проходят whitelist выше.

    # Нельзя делать SELECT * из students.
    if role != "teacher" and re.search(r"\bstudents\b", sql, flags=re.IGNORECASE):
        if re.search(
            r"SELECT\s+\*\s+FROM\s+students\b",
            sql,
            flags=re.IGNORECASE,
        ):
            return False, "Нельзя выводить все поля students."

        # Персональные поля студентов запрещены для applicant/student.
        # ФИО преподавателей при этом разрешены.
        forbidden_student_personal_columns = {
            "student_code", "last_name", "first_name", "middle_name",
            "phone", "email", "passport", "address",
        }
        student_aliases = re.findall(
            r"\b(?:FROM|JOIN)\s+students(?:\s+AS)?\s+([a-zA-Z_]\w*)",
            sql,
            flags=re.IGNORECASE,
        )
        for column in forbidden_student_personal_columns:
            # Квалифицированные обращения: s.last_name / students.last_name.
            if re.search(rf"\bstudents\s*\.\s*{column}\b", sql, flags=re.IGNORECASE):
                return False, (
                    f"Нельзя выводить персональные данные студента: {column}"
                )
            for alias in student_aliases:
                if re.search(rf"\b{re.escape(alias)}\s*\.\s*{column}\b", sql, flags=re.IGNORECASE):
                    return False, (
                        f"Нельзя выводить персональные данные студента: {column}"
                    )

        # Если students используется без alias и колонка не квалифицирована,
        # запрещаем персональные поля, чтобы не было обхода через SELECT last_name.
        if re.search(r"\b(?:FROM|JOIN)\s+students\b(?!\s+(?:AS\s+)?[a-zA-Z_]\w*)", sql, flags=re.IGNORECASE):
            for column in forbidden_student_personal_columns:
                if re.search(rf"(?<![.\w]){column}\b", sql, flags=re.IGNORECASE):
                    return False, (
                        f"Нельзя выводить персональные данные студента: {column}"
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
6. Для ролей applicant/student не выводи персональные данные студентов; для teacher это разрешено.
7. Для ролей applicant/student не выводи student_code; для teacher это разрешено.
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
15. В teachers есть ФИО преподавателя: last_name, first_name, middle_name,
    а также teacher_code, position, degree и hire_year. Для student и teacher
    ФИО преподавателей разрешены. Не путай teachers с v_teachers_anonymized.
16. Если вопрос требует список студентов, для applicant/student не показывай
    ФИО, student_code и другие идентификаторы студентов. Используй агрегаты
    или обезличенные views. Для вопросов о преподавателях студент может получать ФИО.
17. Для одного числового ответа обязательно дай понятный alias:
    student_count, active_students, faculty_count и т.п.
18. Если пользователь просит количество по факультету, желательно вернуть
    название факультета вместе с количеством.
19. SQL должен быть максимально простым и читаемым.
20. Если студент спрашивает о преподавателях, используй таблицу teachers и JOIN departments/faculties при необходимости.
21. Если студент спрашивает о поступлении, используй те же relations, что доступны абитуриенту: faculties, departments, study_programs, admission_statistics и связанные открытые views.
22. Верни ТОЛЬКО SQL, без Markdown и без объяснений.

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


async def generate_sql(question: str, role: str) -> Optional[str]:
    full_schema = await get_db_schema()
    allowed = role_relations(role)
    schema_lines = []
    for line in full_schema.splitlines():
        if line.startswith("- "):
            rel = line[2:].split(":", 1)[0].strip()
            if rel not in allowed:
                continue
        schema_lines.append(line)
    schema = "\n".join(schema_lines)

    normalized_question = normalize_question_entities(question)

    prompt = (
        SQL_SYSTEM_PROMPT
        + "\n\n"
        + role_description(role)
        + "\nРазрешенные relations: "
        + ", ".join(sorted(allowed))
        + "\n\n"
        + schema
        + "\n\nВАЖНО: перед генерацией SQL используй канонические названия "
        + "факультетов из вопроса. Например: «лингвистика», «лингвистики», "
        + "«факультет лингвистики» -> «Лингвистический факультет»; "
        + "«экономика», «экономический» -> «Экономический факультет». "
        + "Если фильтруешь v_students_by_faculty, используй точное значение "
        + "faculty_name из БД, а не разговорный синоним.\n\n"
        + "ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        + normalized_question
        + "\n\nТОЛЬКО SQL:"
    )

    answer = await call_yandex(
        [
            {"role": "system", "text": SQL_SYSTEM_PROMPT + "\n\n" + role_description(role)},
            {"role": "user", "text": prompt},
        ],
        temperature=0.0,
        max_tokens=500,
    )

    if not answer:
        return None

    sql = clean_sql(answer)

    # LLM может проигнорировать подсказку. Исправляем значения сущностей
    # детерминированно до validate_sql() и до выполнения.
    sql = canonicalize_sql_literals(sql)

    valid, reason = validate_sql(sql, role)

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
    role: str,
) -> Optional[str]:
    """
    Если SQL логически не подошел к реальной схеме,
    YandexGPT получает ошибку PostgreSQL и исправляет запрос.
    """

    schema = await get_db_schema()

    prompt = f"""
Исправь SQL-запрос PostgreSQL.

{role_description(role)}
Разрешенные relations: {', '.join(sorted(role_relations(role)))}

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
- для applicant/student не использовать персональные идентификаторы студентов; для teacher это разрешено;
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
    sql = canonicalize_sql_literals(sql)

    valid, reason = validate_sql(sql, role)

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

async def execute_sql(sql: str, role: str):
    sql = canonicalize_sql_literals(clean_sql(sql))

    valid, reason = validate_sql(sql, role)

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
    role: str,
) -> str:

    data = serialize_rows(rows)

    if not data:
        return "Извините, по вашему запросу не удалось найти данные. Пожалуйста, задайте другой вопрос."

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

{role_description(role)}

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
    role: str = "user",
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
                role,
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

async def process_question(question: str, role: str) -> dict:
    question = question.strip()

    if role not in ROLE_LABELS:
        return {"ok": False, "error": "Сначала выберите роль пользователя."}

    allowed, role_error = validate_role_question(role, question)
    if not allowed:
        return {"ok": False, "error": role_error}

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

        sql = await generate_sql(question, role)

        if not sql:
            await log_query(
                question,
                None,
                "sql_generation_error",
                role=role,
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
            rows, last_error = await execute_sql(sql, role)

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
                role,
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
                role=role,
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
            role,
        )

        elapsed = int(
            (time.perf_counter() - started) * 1000
        )

        data = serialize_rows(rows)

        await log_query(
            question,
            sql,
            "success",
            role=role,
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
            role=role,
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


NO_DATA_MESSAGE = "Извините, по вашему запросу не удалось найти данные. Пожалуйста, задайте другой вопрос."

# ============================================================
# WEB UI
# ============================================================

HTML_PAGE = r"""


<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Код Байкала AI</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
.role-screen{position:fixed;inset:0;z-index:50;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 50% 20%,rgba(124,77,255,.22),transparent 34%),rgba(8,6,15,.94);backdrop-filter:blur(18px)}.role-card{width:min(760px,100%);padding:38px;border:1px solid rgba(209,196,233,.16);border-radius:30px;background:linear-gradient(145deg,rgba(37,21,60,.97),rgba(13,11,26,.97));box-shadow:0 30px 100px rgba(0,0,0,.6);text-align:center}.role-logo{width:76px;height:76px;margin:0 auto 18px;border-radius:24px;display:grid;place-items:center;font-size:34px;background:linear-gradient(145deg,#4a2c7a,#7c4dff 55%,#e040fb);box-shadow:0 15px 45px rgba(124,77,255,.32)}.role-card h1{margin:0;font-size:29px}.role-card p{color:var(--muted);font-size:12px;line-height:1.6;margin:10px auto 24px;max-width:560px}.roles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.role-btn{border:1px solid var(--border);border-radius:18px;background:rgba(255,255,255,.035);color:#f4eefb;padding:20px 14px;cursor:pointer;text-align:left;transition:.22s}.role-btn:hover{transform:translateY(-3px);border-color:rgba(179,136,255,.42);background:linear-gradient(145deg,rgba(124,77,255,.15),rgba(224,64,251,.08));box-shadow:0 15px 35px rgba(0,0,0,.25)}.role-btn b{display:block;font-size:14px}.role-btn span{display:block;margin-top:7px;color:#9488a4;font-size:10px;line-height:1.45}.role-note{margin-top:18px;color:#6f647b;font-size:9px;line-height:1.5}@media(max-width:650px){.roles{grid-template-columns:1fr}.role-card{padding:28px 18px}}:root{--bg:#0d0b1a;--bg2:#1a0f2e;--panel:rgba(28,16,48,.78);--panel2:rgba(255,255,255,.045);--violet:#7c4dff;--neon:#e040fb;--lav:#d1c4e9;--text:#faf7ff;--muted:#9f93b1;--border:rgba(209,196,233,.13);--shadow:0 30px 90px rgba(0,0,0,.55)}
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0}body{min-height:100vh;overflow:hidden;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 8% 8%,rgba(124,77,255,.28),transparent 27%),radial-gradient(circle at 92% 12%,rgba(224,64,251,.19),transparent 24%),linear-gradient(135deg,#0d0b1a,#1a0f2e 55%,#0d0b1a)}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.2;background-image:linear-gradient(rgba(209,196,233,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(209,196,233,.045) 1px,transparent 1px);background-size:44px 44px;mask-image:radial-gradient(ellipse at center,black 0%,transparent 76%)}
.orb{position:fixed;border-radius:50%;pointer-events:none;filter:blur(10px);z-index:0;animation:float 10s ease-in-out infinite}.orb.one{width:320px;height:320px;left:-120px;top:-100px;background:rgba(124,77,255,.2);box-shadow:0 0 130px rgba(124,77,255,.2)}.orb.two{width:230px;height:230px;right:-70px;top:25%;background:rgba(224,64,251,.14);animation-delay:-3s}.orb.three{width:360px;height:360px;left:45%;bottom:-260px;background:rgba(74,44,122,.2);animation-delay:-6s}@keyframes float{0%,100%{transform:translate3d(0,0,0)}50%{transform:translate3d(16px,-20px,0)}}
.app-shell{position:relative;z-index:2;width:min(1280px,calc(100vw - 48px));height:min(840px,calc(100vh - 48px));margin:24px auto;display:grid;grid-template-columns:270px minmax(0,1fr);gap:18px}
.sidebar{display:flex;flex-direction:column;padding:24px;border:1px solid var(--border);border-radius:28px;background:linear-gradient(160deg,rgba(37,21,60,.84),rgba(13,11,26,.78));backdrop-filter:blur(24px);box-shadow:var(--shadow);overflow:hidden}.brand{display:flex;align-items:center;gap:12px;margin-bottom:34px}.brand-logo{width:50px;height:50px;border-radius:16px;display:grid;place-items:center;font-size:25px;background:linear-gradient(145deg,#4a2c7a,#7c4dff 55%,#e040fb);box-shadow:0 10px 30px rgba(124,77,255,.32)}.brand strong{display:block;font-size:16px}.brand span{display:block;margin-top:4px;font-size:9px;color:var(--muted);letter-spacing:.4px}.side-title{text-transform:uppercase;letter-spacing:1.6px;font-size:9px;color:#887b9b;margin-bottom:12px}.side-card{padding:14px;margin-bottom:9px;border:1px solid rgba(209,196,233,.09);border-radius:16px;background:rgba(255,255,255,.035);transition:.22s}.side-card:hover{transform:translateY(-2px);background:rgba(124,77,255,.08);border-color:rgba(179,136,255,.24)}.side-card b{display:block;font-size:11px;margin-bottom:5px}.side-card span{display:block;color:#8e829f;font-size:9px;line-height:1.45}.side-footer{margin-top:auto;padding-top:18px;border-top:1px solid rgba(209,196,233,.08);font-size:9px;line-height:1.6;color:#746981}.side-footer b{color:#bdaed0;font-weight:500}
.widget{min-width:0;min-height:0;height:100%;display:flex;flex-direction:column;overflow:hidden;border:1px solid rgba(209,196,233,.16);border-radius:28px;background:linear-gradient(145deg,rgba(29,16,48,.9),rgba(13,11,26,.9));backdrop-filter:blur(26px);box-shadow:var(--shadow),0 0 80px rgba(124,77,255,.1)}
.header{padding:19px 24px;border-bottom:1px solid var(--border);background:rgba(20,12,36,.56);display:flex;align-items:center;justify-content:space-between;gap:16px}.identity{display:flex;align-items:center;gap:12px;min-width:0}.logo{width:44px;height:44px;flex:0 0 44px;border-radius:14px;display:grid;place-items:center;font-size:22px;background:linear-gradient(145deg,#4a2c7a,#7c4dff 55%,#e040fb);box-shadow:0 8px 26px rgba(124,77,255,.35);border:1px solid rgba(255,255,255,.12)}.name{font-size:17px;font-weight:800;letter-spacing:-.3px}.subtitle{margin-top:4px;font-size:10px;color:var(--muted)}.header-actions{display:flex;align-items:center;gap:9px}.status{display:flex;align-items:center;gap:6px;padding:7px 10px;border-radius:999px;font-size:9px;color:#c4b9cf;background:rgba(255,255,255,.035);border:1px solid var(--border)}.status-dot{width:7px;height:7px;border-radius:50%;background:#69e7a4;box-shadow:0 0 10px #69e7a4}.icon-btn{width:34px;height:34px;border-radius:11px;border:1px solid var(--border);background:rgba(255,255,255,.035);color:#d2c6dd;cursor:pointer;transition:.2s}.icon-btn:hover{transform:translateY(-2px);background:rgba(124,77,255,.16);border-color:rgba(179,136,255,.3)}
.chat{flex:1;min-height:0;overflow-y:auto;padding:30px 44px 24px;scroll-behavior:smooth}.chat::-webkit-scrollbar{width:6px}.chat::-webkit-scrollbar-thumb{background:rgba(209,196,233,.16);border-radius:20px}.welcome{text-align:center;padding:45px 20px 30px;animation:rise .45s ease}.welcome-logo{width:72px;height:72px;margin:0 auto 16px;border-radius:23px;display:grid;place-items:center;background:linear-gradient(145deg,rgba(124,77,255,.22),rgba(224,64,251,.1));border:1px solid rgba(209,196,233,.14);box-shadow:inset 0 1px rgba(255,255,255,.08),0 18px 50px rgba(124,77,255,.14)}.welcome-logo span{font-size:32px;background:linear-gradient(135deg,#fff,#b388ff,#e040fb);-webkit-background-clip:text;background-clip:text;color:transparent}.welcome h1{font-size:29px;letter-spacing:-.8px;margin:0}.welcome p{font-size:13px;line-height:1.6;color:var(--muted);max-width:600px;margin:9px auto 23px}.chips{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;max-width:860px;margin:auto}.chip{border:1px solid var(--border);border-radius:16px;background:rgba(255,255,255,.035);color:#e1d9e9;padding:14px 12px;cursor:pointer;text-align:left;transition:.22s;font:600 11px/1.4 Inter,system-ui}.chip:hover{transform:translateY(-3px);border-color:rgba(179,136,255,.32);background:linear-gradient(145deg,rgba(124,77,255,.14),rgba(224,64,251,.07));box-shadow:0 12px 30px rgba(0,0,0,.2)}.chip span{display:block;color:#8f83a0;font-size:9px;margin-top:5px;font-weight:400}.row{display:flex;margin:10px 0;animation:rise .25s ease}.row.user{justify-content:flex-end}.bubble{max-width:min(78%,760px);padding:14px 17px;font-size:13px;line-height:1.6;word-break:break-word}.bubble.user{color:#fff;background:linear-gradient(135deg,#5b32b2,#7c4dff 58%,#9b55e8);border:1px solid rgba(255,255,255,.13);border-radius:18px 18px 5px 18px;box-shadow:0 10px 28px rgba(74,44,122,.3)}.bubble.bot{background:linear-gradient(145deg,rgba(52,31,78,.68),rgba(28,17,45,.76));border:1px solid var(--border);border-radius:18px 18px 18px 5px;color:#f1ebf8;box-shadow:0 10px 28px rgba(0,0,0,.18);backdrop-filter:blur(15px)}.bubble.error{border-color:rgba(255,100,145,.25);background:rgba(87,25,50,.3);color:#ffdce6}.meta{margin-top:9px;font-size:10px;color:#756b82}.loading-dots{display:inline-flex;gap:4px;margin-left:5px;vertical-align:middle}.loading-dots i{width:5px;height:5px;border-radius:50%;background:#b388ff;animation:pulse 1.1s infinite}.loading-dots i:nth-child(2){animation-delay:.15s}.loading-dots i:nth-child(3){animation-delay:.3s}@keyframes pulse{0%,80%,100%{opacity:.2;transform:scale(.75)}40%{opacity:1;transform:scale(1)}}@keyframes rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
.details{margin-top:11px;border-top:1px solid rgba(209,196,233,.08);padding-top:9px}.details summary{cursor:pointer;color:#bda7df;font-size:10px;list-style:none}.details summary::-webkit-details-marker{display:none}.details summary:before{content:"＋ ";color:#e040fb}.details[open] summary:before{content:"－ "}pre{margin:8px 0 0;background:#090611;border:1px solid rgba(209,196,233,.08);border-radius:10px;padding:12px;overflow:auto;color:#d7c8e7;font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.table-wrap{overflow:auto;margin-top:8px;border:1px solid var(--border);border-radius:10px}table{width:100%;border-collapse:collapse;min-width:320px}th,td{padding:9px 10px;border-bottom:1px solid rgba(209,196,233,.06);font-size:10px;text-align:left;white-space:nowrap}th{background:rgba(124,77,255,.08);color:#cbb8e2}td{color:#d9d0e2}
.composer{padding:15px 24px 18px;border-top:1px solid var(--border);background:rgba(13,11,26,.76);backdrop-filter:blur(18px)}.input-box{display:flex;align-items:flex-end;gap:9px;padding:7px;border-radius:18px;border:1px solid rgba(209,196,233,.14);background:rgba(255,255,255,.045);transition:.2s}.input-box:focus-within{border-color:rgba(179,136,255,.42);box-shadow:0 0 0 3px rgba(124,77,255,.08),0 0 28px rgba(124,77,255,.08)}textarea{width:100%;min-height:48px;max-height:130px;resize:none;border:0;outline:0;background:transparent;color:var(--text);font:13px/1.5 Inter,system-ui;padding:9px 10px}textarea::placeholder{color:#746a80}.send{width:46px;height:46px;flex:0 0 46px;border:0;border-radius:14px;color:#fff;cursor:pointer;font-size:16px;background:linear-gradient(135deg,#6d3de0,#7c4dff 48%,#e040fb);box-shadow:0 9px 24px rgba(124,77,255,.34);transition:.2s}.send:hover{transform:translateY(-2px) scale(1.02);box-shadow:0 12px 30px rgba(224,64,251,.25)}.send:active{transform:scale(.95)}.send:disabled{opacity:.45;cursor:wait}.composer-bottom{display:flex;justify-content:space-between;padding:8px 3px 0;color:#675d72;font-size:9px}.safe{color:#9182a2}.safe b{color:#b9a4cf;font-weight:500}
@media(max-width:950px){.app-shell{width:calc(100vw - 28px);height:calc(100vh - 28px);grid-template-columns:1fr}.sidebar{display:none}.chat{padding:24px}.chips{grid-template-columns:1fr 1fr}}
@media(max-width:600px){body{overflow:hidden}.app-shell{width:100vw;height:100vh;margin:0}.widget{border-radius:0;border:0}.header{padding:16px}.chat{padding:18px 14px}.composer{padding:10px 12px 12px}.welcome{padding:28px 8px 20px}.welcome h1{font-size:22px}.welcome p{font-size:11px}.chips{grid-template-columns:1fr 1fr}.bubble{max-width:88%;font-size:11px}}
</style>
</head>
<body>
<div id="roleScreen" class="role-screen">
  <div class="role-card">
    <div class="role-logo">🧠</div>
    <h1>Кто вы?</h1>
    <p>Выберите роль, под которой хотите работать с университетским AI-помощником.</p>
    <div class="roles">
      <button class="role-btn" data-role="applicant"><b>🎓 Абитуриент</b><span>Факультеты, направления, поступление и открытая статистика.</span></button>
      <button class="role-btn" data-role="student"><b>📚 Студент</b><span>Учебная информация и доступная агрегированная статистика.</span></button>
      <button class="role-btn" data-role="teacher"><b>👨‍🏫 Преподаватель</b><span>Полный доступ к данным, разрешенным приложением.</span></button>
    </div>
    <div class="role-note">Без пароля это выбор режима доступа, а не подтверждение личности. Для production роль нужно связывать с аккаунтом.</div>
  </div>
</div>
<div class="orb one"></div><div class="orb two"></div><div class="orb three"></div>
<div class="app-shell">
<aside class="sidebar">
  <div class="brand"><div class="brand-logo">🧠</div><div><strong>Код Байкала AI</strong><span>UNIVERSITY INTELLIGENCE</span></div></div>
  <div class="side-title">Возможности</div>
  <div class="side-card"><b>◉ Студенты</b><span>Количество обучающихся и общая статистика</span></div>
  <div class="side-card"><b>◇ Факультеты</b><span>Факультеты, направления и деканы</span></div>
  <div class="side-card"><b>✧ Преподаватели</b><span>Информация о преподавателях университета</span></div>
  <div class="side-card"><b>⌁ Безопасный доступ</b><span>Запросы выполняются только в режиме чтения</span></div>
  <div class="side-footer"><b>AI-помощник университета</b><br>Задавайте вопросы обычным языком — система найдёт данные в PostgreSQL и сформирует понятный ответ.</div>
</aside>
<section class="widget" aria-label="Чат-бот Код Байкала AI">
<header class="header"><div class="identity"><div class="logo">🧠</div><div><div class="name">Код Байкала AI</div><div class="subtitle">Интеллектуальный помощник университета</div></div></div><div class="header-actions"><div class="status" id="roleBadge">роль не выбрана</div><div class="status"><span class="status-dot"></span>онлайн</div><button class="icon-btn" id="switchRole" title="Сменить роль">⇄</button><button class="icon-btn" id="clear" title="Новый чат" aria-label="Новый чат">↻</button></div></header>
<main id="chat" class="chat"><div id="welcome" class="welcome"><div class="welcome-logo"><span>✦</span></div><h1>Чем могу помочь?</h1><p>Задайте вопрос обычным языком — я найду информацию в базе университета и отвечу понятно и по-русски.</p><div class="chips"><button class="chip" data-q="Сколько всего студентов?">◉ Студенты<span>Общая статистика</span></button><button class="chip" data-q="Сколько студентов обучается на лингвистическом факультете?">⌁ Факультеты<span>Количество студентов</span></button><button class="chip" data-q="Кто декан факультета лингвистики?">◇ Деканы<span>Руководители факультетов</span></button><button class="chip" data-q="Назови ФИО 10 преподавателей">✧ Преподаватели<span>Список преподавателей</span></button></div></div></main>
<footer class="composer"><div class="input-box"><textarea id="question" maxlength="1000" placeholder="Напишите вопрос о студентах, факультетах, преподавателях…"></textarea><button id="send" class="send" title="Отправить" aria-label="Отправить">➤</button></div><div class="composer-bottom"><span>Enter — отправить · Shift + Enter — перенос</span><span class="safe">✦ <b>Данные защищены</b> · <span id="counter">0/1000</span></span></div></footer>
</section></div>
<script>
const chat=document.getElementById("chat"),question=document.getElementById("question"),send=document.getElementById("send"),counter=document.getElementById("counter"),clearBtn=document.getElementById("clear");
const roleScreen=document.getElementById("roleScreen");
const roleBadge=document.getElementById("roleBadge");
const switchRoleBtn=document.getElementById("switchRole");
const roleNames={applicant:"Абитуриент",student:"Студент",teacher:"Преподаватель"};
let currentRole=null;

async function selectRole(role){
  try{
    const response=await fetch("/api/role",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role})});
    const result=await response.json();
    if(!result.ok){alert(result.error||"Не удалось выбрать роль.");return;}
    currentRole=role;
    roleBadge.textContent="Роль · "+roleNames[role];
    roleScreen.style.display="none";
    question.focus();
  }catch(e){alert("Не удалось установить роль. Попробуйте ещё раз.");}
}
async function loadRole(){
  try{
    const response=await fetch("/api/role");
    const result=await response.json();
    if(result.ok&&result.role){
      currentRole=result.role;
      roleBadge.textContent="Роль · "+roleNames[result.role];
      roleScreen.style.display="none";
    }else roleScreen.style.display="grid";
  }catch(e){roleScreen.style.display="grid";}
}
switchRoleBtn.addEventListener("click",()=>{currentRole=null;roleScreen.style.display="grid";});
document.querySelectorAll(".role-btn").forEach(btn=>btn.addEventListener("click",()=>selectRole(btn.dataset.role)));

const welcomeHTML=document.getElementById("welcome")?.outerHTML||"";
function scrollBottom(){requestAnimationFrame(()=>{chat.scrollTop=chat.scrollHeight})}
function addMessage(text,type){const row=document.createElement("div");row.className="row "+type;const bubble=document.createElement("div");bubble.className="bubble "+(type==="error"?"error":type);bubble.textContent=text;row.appendChild(bubble);chat.appendChild(row);scrollBottom();return row}
function addLoading(){const row=document.createElement("div");row.className="row assistant";row.innerHTML='<div class="bubble bot">Ищу ответ<span class="loading-dots"><i></i><i></i><i></i></span></div>';chat.appendChild(row);scrollBottom();return row}
function addResult(result){const row=document.createElement("div");row.className="row assistant";const bubble=document.createElement("div");bubble.className="bubble bot";const answer=document.createElement("div");answer.textContent=result.answer||"";bubble.appendChild(answer);if(result.sql){const d=document.createElement("details");d.className="details";const s=document.createElement("summary");s.textContent="Показать SQL-запрос";d.appendChild(s);const pre=document.createElement("pre");pre.textContent=result.sql;d.appendChild(pre);bubble.appendChild(d)}if(Array.isArray(result.rows)&&result.rows.length){const d=document.createElement("details");d.className="details";const s=document.createElement("summary");s.textContent="Показать результат БД";d.appendChild(s);const wrap=document.createElement("div");wrap.className="table-wrap";const table=document.createElement("table");const keys=Object.keys(result.rows[0]);const head=document.createElement("tr");keys.forEach(k=>{const th=document.createElement("th");th.textContent=k;head.appendChild(th)});table.appendChild(head);result.rows.forEach(r=>{const tr=document.createElement("tr");keys.forEach(k=>{const td=document.createElement("td");td.textContent=r[k]??"";tr.appendChild(td)});table.appendChild(tr)});wrap.appendChild(table);d.appendChild(wrap);bubble.appendChild(d)}if(result.response_time_ms!==undefined){const meta=document.createElement("div");meta.className="meta";meta.textContent="Время обработки · "+result.response_time_ms+" мс";bubble.appendChild(meta)}row.appendChild(bubble);chat.appendChild(row);scrollBottom()}
async function sendQuestion(chipText=null){const text=(chipText??question.value).trim();if(!text)return;document.getElementById("welcome")?.remove();addMessage(text,"user");question.value="";counter.textContent="0/1000";send.disabled=true;const loading=addLoading();try{const response=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:text})});const result=await response.json();loading.remove();if(!result.ok)addMessage(result.error||"Вопрос некорректен. Пожалуйста, сформулируйте вопрос по данным университета.","error");else addResult(result)}catch(error){loading.remove();addMessage("Не удалось связаться с сервером. Попробуйте ещё раз.","error")}finally{send.disabled=false;question.focus()}}
function clearChat(){chat.innerHTML=welcomeHTML;document.querySelectorAll(".chip").forEach(c=>c.addEventListener("click",()=>sendQuestion(c.dataset.q)));loadRole();question.value="";counter.textContent="0/1000";question.focus()}
send.addEventListener("click",()=>sendQuestion());clearBtn.addEventListener("click",clearChat);question.addEventListener("input",()=>counter.textContent=question.value.length+"/1000");question.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendQuestion()}});document.querySelectorAll(".chip").forEach(c=>c.addEventListener("click",()=>sendQuestion(c.dataset.q)));
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


        if method == "GET" and path == "/api/role":
            role = get_role_from_headers(headers)
            response = json.dumps(
                {"ok": bool(role), "role": role},
                ensure_ascii=False,
            ).encode("utf-8")
            await http_response(
                writer, 200, "application/json; charset=utf-8", response
            )
            return

        if method == "POST" and path == "/api/role":
            try:
                payload = json.loads(body.decode("utf-8"))
                role = str(payload.get("role", "")).strip().lower()
                if role not in ROLE_LABELS:
                    response = json.dumps(
                        {"ok": False, "error": "Неизвестная роль."},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    await http_response(
                        writer, 400, "application/json; charset=utf-8", response
                    )
                    return

                cookie = sign_role(role)
                response = json.dumps(
                    {"ok": True, "role": role, "label": ROLE_LABELS[role]},
                    ensure_ascii=False,
                ).encode("utf-8")
                headers_out = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    + f"Content-Length: {len(response)}\r\n".encode("ascii")
                    + f"Set-Cookie: {ROLE_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                )
                writer.write(headers_out + response)
                await writer.drain()
                writer.close()
                return
            except Exception as exc:
                response = json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                ).encode("utf-8")
                await http_response(
                    writer, 400, "application/json; charset=utf-8", response
                )
                return

        if method == "POST" and path == "/api/ask":
            try:
                payload = json.loads(body.decode("utf-8"))
                question = str(payload.get("question", ""))

                role = get_role_from_headers(headers)
                if not role:
                    result = {"ok": False, "error": "Сначала выберите роль пользователя."}
                else:
                    result = await process_question(question, role)

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

            result = await process_question(question, "teacher")

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

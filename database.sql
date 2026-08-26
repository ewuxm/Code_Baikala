-- ============================================================
-- БАЗА ДАННЫХ УНИВЕРСИТЕТА
-- PostgreSQL
-- Полный связный скрипт создания и заполнения
-- ============================================================

BEGIN;

-- ============================================================
-- 1. УДАЛЕНИЕ ПРЕДЫДУЩИХ ОБЪЕКТОВ
-- ============================================================

DROP VIEW IF EXISTS v_faculty_statistics CASCADE;
DROP VIEW IF EXISTS v_students_by_faculty CASCADE;
DROP VIEW IF EXISTS v_students_anonymized CASCADE;
DROP VIEW IF EXISTS v_teachers_anonymized CASCADE;
DROP VIEW IF EXISTS v_simple_faculty_stats CASCADE;
DROP VIEW IF EXISTS v_faculty_search CASCADE;

DROP TABLE IF EXISTS
    query_logs,
    academic_statistics,
    admission_statistics,
    schedule,
    grades,
    course_assignments,
    applicants,
    employees,
    teachers,
    students,
    groups,
    courses,
    study_programs,
    departments,
    faculties
CASCADE;

-- ============================================================
-- 2. СПРАВОЧНЫЕ ТАБЛИЦЫ
-- ============================================================

CREATE TABLE faculties (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    dean_full_name VARCHAR(200) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE departments (
    id SERIAL PRIMARY KEY,
    faculty_id INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    head_full_name VARCHAR(200) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (faculty_id, name)
);

CREATE TABLE study_programs (
    id SERIAL PRIMARY KEY,
    faculty_id INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    code VARCHAR(20) NOT NULL UNIQUE,
    level VARCHAR(50) NOT NULL CHECK (
        level IN ('Бакалавриат', 'Магистратура', 'Специалитет')
    ),
    duration_years INTEGER NOT NULL CHECK (duration_years > 0),
    total_budget_places INTEGER NOT NULL DEFAULT 0 CHECK (total_budget_places >= 0),
    total_paid_places INTEGER NOT NULL DEFAULT 0 CHECK (total_paid_places >= 0),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE courses (
    id SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    code VARCHAR(20) NOT NULL UNIQUE,
    credits INTEGER NOT NULL DEFAULT 3 CHECK (credits > 0),
    semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 12),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE groups (
    id SERIAL PRIMARY KEY,
    study_program_id INTEGER NOT NULL REFERENCES study_programs(id) ON DELETE CASCADE,
    name VARCHAR(50) NOT NULL UNIQUE,
    year_of_admission INTEGER NOT NULL CHECK (year_of_admission BETWEEN 2020 AND 2035),
    current_semester INTEGER NOT NULL DEFAULT 1 CHECK (current_semester BETWEEN 1 AND 12),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 3. ОСНОВНЫЕ ТАБЛИЦЫ
-- ============================================================

CREATE TABLE students (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    faculty_id INTEGER NOT NULL REFERENCES faculties(id),
    student_code VARCHAR(20) NOT NULL UNIQUE,

    last_name VARCHAR(100) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    middle_name VARCHAR(100),

    enrollment_year INTEGER NOT NULL CHECK (enrollment_year BETWEEN 2020 AND 2035),

    status VARCHAR(50) NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'academic_leave', 'expelled', 'graduated')
    ),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_students_faculty_id ON students(faculty_id);
CREATE INDEX idx_students_group_id ON students(group_id);
CREATE INDEX idx_students_status ON students(status);
CREATE INDEX idx_students_full_name
    ON students(last_name, first_name, middle_name);

CREATE TABLE teachers (
    id SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    teacher_code VARCHAR(20) NOT NULL UNIQUE,

    last_name VARCHAR(100) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    middle_name VARCHAR(100),

    hire_year INTEGER NOT NULL CHECK (hire_year BETWEEN 1980 AND 2035),
    position VARCHAR(100) NOT NULL,
    degree VARCHAR(100),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_teachers_department_id ON teachers(department_id);
CREATE INDEX idx_teachers_full_name
    ON teachers(last_name, first_name, middle_name);

-- Прочие сотрудники университета, не обязательно преподаватели
CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    faculty_id INTEGER REFERENCES faculties(id) ON DELETE SET NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    employee_code VARCHAR(20) NOT NULL UNIQUE,

    last_name VARCHAR(100) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    middle_name VARCHAR(100),

    position VARCHAR(150) NOT NULL,
    hire_year INTEGER NOT NULL CHECK (hire_year BETWEEN 1980 AND 2035),
    employment_status VARCHAR(30) NOT NULL DEFAULT 'active' CHECK (
        employment_status IN ('active', 'dismissed', 'academic_leave')
    ),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_employees_faculty_id ON employees(faculty_id);
CREATE INDEX idx_employees_department_id ON employees(department_id);

CREATE TABLE applicants (
    id SERIAL PRIMARY KEY,
    applicant_code VARCHAR(20) NOT NULL UNIQUE,
    study_program_id INTEGER NOT NULL REFERENCES study_programs(id) ON DELETE CASCADE,

    last_name VARCHAR(100) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    middle_name VARCHAR(100),

    exam_score INTEGER NOT NULL CHECK (exam_score BETWEEN 0 AND 100),

    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'admitted', 'rejected', 'waiting')
    ),

    is_budget BOOLEAN NOT NULL DEFAULT FALSE,
    enrollment_year INTEGER NOT NULL CHECK (enrollment_year BETWEEN 2020 AND 2035),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_applicants_program ON applicants(study_program_id);
CREATE INDEX idx_applicants_year ON applicants(enrollment_year);

-- ============================================================
-- 4. УЧЕБНЫЙ ПРОЦЕСС
-- ============================================================

CREATE TABLE course_assignments (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE RESTRICT,

    semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 12),
    hours_per_week INTEGER NOT NULL DEFAULT 4 CHECK (hours_per_week > 0),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (group_id, course_id, semester)
);

CREATE INDEX idx_course_assignments_group ON course_assignments(group_id);
CREATE INDEX idx_course_assignments_course ON course_assignments(course_id);
CREATE INDEX idx_course_assignments_teacher ON course_assignments(teacher_id);

CREATE TABLE grades (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    course_assignment_id INTEGER NOT NULL REFERENCES course_assignments(id) ON DELETE CASCADE,

    grade INTEGER NOT NULL CHECK (grade BETWEEN 2 AND 5),
    semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 12),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (student_id, course_assignment_id)
);

CREATE INDEX idx_grades_student_id ON grades(student_id);
CREATE INDEX idx_grades_assignment_id ON grades(course_assignment_id);
CREATE INDEX idx_grades_semester ON grades(semester);

CREATE TABLE schedule (
    id SERIAL PRIMARY KEY,
    course_assignment_id INTEGER NOT NULL REFERENCES course_assignments(id) ON DELETE CASCADE,

    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    room VARCHAR(50) NOT NULL,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK (end_time > start_time)
);

CREATE INDEX idx_schedule_assignment ON schedule(course_assignment_id);

-- ============================================================
-- 5. СТАТИСТИКА И ЛОГИ
-- ============================================================

CREATE TABLE admission_statistics (
    id SERIAL PRIMARY KEY,
    study_program_id INTEGER NOT NULL REFERENCES study_programs(id) ON DELETE CASCADE,

    year INTEGER NOT NULL CHECK (year BETWEEN 2020 AND 2035),
    applicants_count INTEGER NOT NULL DEFAULT 0 CHECK (applicants_count >= 0),
    admitted_count INTEGER NOT NULL DEFAULT 0 CHECK (admitted_count >= 0),
    avg_exam_score NUMERIC(5,2) CHECK (avg_exam_score BETWEEN 0 AND 100),
    budget_places_filled INTEGER NOT NULL DEFAULT 0 CHECK (budget_places_filled >= 0),
    paid_places_filled INTEGER NOT NULL DEFAULT 0 CHECK (paid_places_filled >= 0),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK (admitted_count <= applicants_count),
    UNIQUE (study_program_id, year)
);

CREATE INDEX idx_admission_statistics_year ON admission_statistics(year);

CREATE TABLE academic_statistics (
    id SERIAL PRIMARY KEY,
    faculty_id INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,

    academic_year INTEGER NOT NULL CHECK (academic_year BETWEEN 2020 AND 2035),
    semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 12),

    total_students INTEGER NOT NULL DEFAULT 0 CHECK (total_students >= 0),
    avg_gpa NUMERIC(3,2) CHECK (avg_gpa BETWEEN 2.00 AND 5.00),
    students_with_debt INTEGER NOT NULL DEFAULT 0 CHECK (students_with_debt >= 0),
    expelled_count INTEGER NOT NULL DEFAULT 0 CHECK (expelled_count >= 0),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (faculty_id, academic_year, semester)
);

CREATE INDEX idx_academic_statistics_year ON academic_statistics(academic_year);
CREATE INDEX idx_academic_statistics_semester ON academic_statistics(semester);

CREATE TABLE query_logs (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100),
    user_role VARCHAR(50),

    question TEXT NOT NULL,
    generated_sql TEXT,
    executed_sql TEXT,

    response_time_ms INTEGER CHECK (response_time_ms >= 0),
    rows_returned INTEGER CHECK (rows_returned >= 0),

    status VARCHAR(50) NOT NULL DEFAULT 'success',
    error_message TEXT,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_query_logs_user_id ON query_logs(user_id);
CREATE INDEX idx_query_logs_created_at ON query_logs(created_at);

-- ============================================================
-- 6. ЗАПОЛНЕНИЕ ФАКУЛЬТЕТОВ
-- ============================================================

INSERT INTO faculties (name, dean_full_name) VALUES
    ('Технологический факультет', 'Иванов Иван Иванович'),
    ('Экономический факультет', 'Петров Петр Петрович'),
    ('Математический факультет', 'Сидоров Сидор Сидорович'),
    ('Юридический факультет', 'Смирнова Анна Викторовна'),
    ('Лингвистический факультет', 'Козлова Елена Дмитриевна');

INSERT INTO departments (faculty_id, name, head_full_name) VALUES
    (1, 'Кафедра программной инженерии', 'Смирнов Алексей Викторович'),
    (1, 'Кафедра информационной безопасности', 'Козлов Дмитрий Сергеевич'),
    (1, 'Кафедра искусственного интеллекта', 'Новиков Игорь Павлович'),

    (2, 'Кафедра экономики и управления', 'Новикова Ольга Ивановна'),
    (2, 'Кафедра финансов и кредита', 'Морозов Александр Петрович'),
    (2, 'Кафедра маркетинга', 'Волкова Екатерина Сергеевна'),

    (3, 'Кафедра высшей математики', 'Васильев Игорь Николаевич'),
    (3, 'Кафедра физики', 'Попов Андрей Викторович'),
    (3, 'Кафедра химии', 'Михайлова Татьяна Владимировна'),

    (4, 'Кафедра гражданского права', 'Федоров Алексей Иванович'),
    (4, 'Кафедра уголовного права', 'Медведева Ольга Петровна'),
    (4, 'Кафедра теории государства и права', 'Алексеев Владимир Сергеевич'),

    (5, 'Кафедра английской филологии', 'Лебедева Ирина Николаевна'),
    (5, 'Кафедра немецкой филологии', 'Соколов Андрей Александрович'),
    (5, 'Кафедра русского языка', 'Григорьева Мария Ивановна');

-- ============================================================
-- 7. НАПРАВЛЕНИЯ ПОДГОТОВКИ
-- ============================================================

INSERT INTO study_programs (
    faculty_id,
    name,
    code,
    level,
    duration_years,
    total_budget_places,
    total_paid_places
) VALUES
    (1, 'Информационные системы и технологии', '09.03.02', 'Бакалавриат', 4, 30, 20),
    (1, 'Прикладная информатика', '09.03.03', 'Бакалавриат', 4, 25, 15),
    (1, 'Информационная безопасность', '10.03.01', 'Бакалавриат', 4, 20, 10),
    (1, 'Искусственный интеллект', '09.03.04', 'Бакалавриат', 4, 20, 10),
    (1, 'Информатика и вычислительная техника', '09.04.01', 'Магистратура', 2, 15, 15),

    (2, 'Экономика', '38.03.01', 'Бакалавриат', 4, 35, 25),
    (2, 'Менеджмент', '38.03.02', 'Бакалавриат', 4, 25, 20),
    (2, 'Финансы и кредит', '38.03.03', 'Бакалавриат', 4, 20, 15),

    (3, 'Математика и компьютерные науки', '01.03.02', 'Бакалавриат', 4, 20, 10),
    (3, 'Физика', '03.03.02', 'Бакалавриат', 4, 15, 10),
    (3, 'Химия', '04.03.01', 'Бакалавриат', 4, 15, 5),

    (4, 'Юриспруденция', '40.03.01', 'Бакалавриат', 4, 30, 20),
    (4, 'Правовое обеспечение', '40.03.02', 'Бакалавриат', 4, 20, 15),

    (5, 'Лингвистика', '45.03.02', 'Бакалавриат', 4, 20, 15),
    (5, 'Перевод и переводоведение', '45.03.03', 'Бакалавриат', 4, 15, 10);

-- ============================================================
-- 8. ДИСЦИПЛИНЫ
-- ============================================================

INSERT INTO courses (department_id, name, code, credits, semester) VALUES
    (1, 'Программирование на Python', 'CS101', 4, 1),
    (1, 'Базы данных', 'CS102', 3, 2),
    (1, 'Веб-разработка', 'CS103', 3, 2),

    (2, 'Информационная безопасность', 'CS201', 4, 3),
    (2, 'Криптография', 'CS202', 3, 4),

    (3, 'Машинное обучение', 'CS301', 4, 5),
    (3, 'Нейронные сети', 'CS302', 3, 6),

    (4, 'Экономическая теория', 'EC101', 3, 1),
    (4, 'Макроэкономика', 'EC102', 3, 2),

    (5, 'Финансы', 'EC201', 3, 3),
    (5, 'Инвестиции', 'EC202', 3, 4),

    (6, 'Маркетинг', 'EC301', 3, 5),
    (6, 'Управление проектами', 'EC302', 3, 6),

    (7, 'Высшая математика', 'MA101', 4, 1),
    (7, 'Линейная алгебра', 'MA102', 3, 2),

    (8, 'Физика', 'PH101', 4, 1),
    (8, 'Квантовая физика', 'PH201', 3, 3),

    (9, 'Химия', 'CH101', 3, 1),
    (9, 'Органическая химия', 'CH201', 3, 3),

    (10, 'Гражданское право', 'LA101', 4, 1),
    (10, 'Семейное право', 'LA102', 3, 2),

    (11, 'Уголовное право', 'LA201', 4, 3),
    (11, 'Криминалистика', 'LA202', 3, 4),

    (12, 'Теория государства и права', 'LA301', 3, 5),

    (13, 'Английский язык', 'LN101', 4, 1),
    (13, 'Лингвистика', 'LN102', 3, 2),

    (14, 'Немецкий язык', 'LN201', 3, 3),
    (14, 'Страноведение', 'LN202', 3, 4),

    (15, 'Русский язык', 'LN301', 3, 5),
    (15, 'Стилистика', 'LN302', 3, 6);

-- ============================================================
-- 9. ГРУППЫ
-- ============================================================

INSERT INTO groups (
    study_program_id,
    name,
    year_of_admission,
    current_semester
) VALUES
    (1, 'ИС-1011', 2023, 6),
    (1, 'ИС-1012', 2024, 4),
    (2, 'ПИ-1011', 2023, 6),
    (3, 'ИБ-1011', 2024, 4),
    (4, 'АИ-1011', 2023, 6),

    (6, 'Э-1011', 2023, 6),
    (7, 'М-1011', 2024, 4),
    (8, 'ФК-1011', 2023, 6),

    (9, 'МК-1011', 2023, 6),
    (10, 'Ф-1011', 2024, 4),
    (11, 'Х-1011', 2024, 4),

    (12, 'Ю-1011', 2023, 6),
    (13, 'ПО-1011', 2024, 4),

    (14, 'Л-1011', 2023, 6),
    (15, 'ПП-1011', 2024, 4);

-- ============================================================
-- 10. ПРЕПОДАВАТЕЛИ С ФИО
-- ============================================================

INSERT INTO teachers (
    department_id,
    teacher_code,
    last_name,
    first_name,
    middle_name,
    hire_year,
    position,
    degree
) VALUES
    (1, 'TCH001', 'Орлов', 'Сергей', 'Андреевич', 2010, 'Профессор', 'Доктор технических наук'),
    (1, 'TCH002', 'Белова', 'Марина', 'Сергеевна', 2012, 'Доцент', 'Кандидат технических наук'),

    (2, 'TCH003', 'Захаров', 'Павел', 'Игоревич', 2011, 'Профессор', 'Доктор технических наук'),
    (2, 'TCH004', 'Романова', 'Ольга', 'Викторовна', 2014, 'Доцент', 'Кандидат технических наук'),

    (3, 'TCH005', 'Никитин', 'Дмитрий', 'Олегович', 2013, 'Профессор', 'Доктор технических наук'),
    (3, 'TCH006', 'Егорова', 'Наталья', 'Александровна', 2015, 'Доцент', 'Кандидат технических наук'),

    (4, 'TCH007', 'Кузнецов', 'Владимир', 'Иванович', 2009, 'Профессор', 'Доктор экономических наук'),
    (4, 'TCH008', 'Семенова', 'Ирина', 'Петровна', 2011, 'Доцент', 'Кандидат экономических наук'),

    (5, 'TCH009', 'Мельников', 'Александр', 'Владимирович', 2010, 'Профессор', 'Доктор экономических наук'),
    (5, 'TCH010', 'Гаврилова', 'Елена', 'Михайловна', 2013, 'Доцент', 'Кандидат экономических наук'),

    (6, 'TCH011', 'Фролов', 'Виктор', 'Алексеевич', 2012, 'Профессор', 'Доктор экономических наук'),
    (6, 'TCH012', 'Савельева', 'Людмила', 'Игоревна', 2014, 'Доцент', 'Кандидат экономических наук'),

    (7, 'TCH013', 'Андреев', 'Михаил', 'Васильевич', 2008, 'Профессор', 'Доктор физико-математических наук'),
    (7, 'TCH014', 'Павлова', 'Татьяна', 'Андреевна', 2010, 'Доцент', 'Кандидат физико-математических наук'),

    (8, 'TCH015', 'Соловьев', 'Николай', 'Петрович', 2009, 'Профессор', 'Доктор физико-математических наук'),
    (8, 'TCH016', 'Климова', 'Вера', 'Сергеевна', 2012, 'Доцент', 'Кандидат физико-математических наук'),

    (9, 'TCH017', 'Тихонов', 'Евгений', 'Александрович', 2011, 'Профессор', 'Доктор химических наук'),
    (9, 'TCH018', 'Назарова', 'Алла', 'Викторовна', 2014, 'Доцент', 'Кандидат химических наук'),

    (10, 'TCH019', 'Власов', 'Антон', 'Михайлович', 2010, 'Профессор', 'Доктор юридических наук'),
    (10, 'TCH020', 'Кириллова', 'Екатерина', 'Олеговна', 2012, 'Доцент', 'Кандидат юридических наук'),

    (11, 'TCH021', 'Борисов', 'Роман', 'Сергеевич', 2011, 'Профессор', 'Доктор юридических наук'),
    (11, 'TCH022', 'Данилова', 'Светлана', 'Ивановна', 2013, 'Доцент', 'Кандидат юридических наук'),

    (12, 'TCH023', 'Максимов', 'Артур', 'Викторович', 2009, 'Профессор', 'Доктор юридических наук'),
    (12, 'TCH024', 'Калинина', 'Юлия', 'Павловна', 2012, 'Доцент', 'Кандидат юридических наук'),

    (13, 'TCH025', 'Воробьев', 'Константин', 'Александрович', 2010, 'Профессор', 'Доктор филологических наук'),
    (13, 'TCH026', 'Маслова', 'Ирина', 'Владимировна', 2013, 'Доцент', 'Кандидат филологических наук'),

    (14, 'TCH027', 'Филиппов', 'Алексей', 'Николаевич', 2011, 'Профессор', 'Доктор филологических наук'),
    (14, 'TCH028', 'Жукова', 'Мария', 'Олеговна', 2014, 'Доцент', 'Кандидат филологических наук'),

    (15, 'TCH029', 'Громов', 'Валерий', 'Сергеевич', 2010, 'Профессор', 'Доктор филологических наук'),
    (15, 'TCH030', 'Зайцева', 'Анна', 'Петровна', 2012, 'Доцент', 'Кандидат филологических наук');

-- ============================================================
-- 11. СОТРУДНИКИ С ФИО
-- ============================================================

INSERT INTO employees (
    faculty_id,
    department_id,
    employee_code,
    last_name,
    first_name,
    middle_name,
    position,
    hire_year,
    employment_status
) VALUES
    (1, NULL, 'EMP001', 'Власова', 'Оксана', 'Игоревна', 'Специалист деканата', 2019, 'active'),
    (1, 1, 'EMP002', 'Котов', 'Алексей', 'Владимирович', 'Лаборант кафедры', 2021, 'active'),
    (2, NULL, 'EMP003', 'Морозова', 'Нина', 'Сергеевна', 'Специалист учебного отдела', 2018, 'active'),
    (3, 7, 'EMP004', 'Ларионов', 'Петр', 'Андреевич', 'Инженер лаборатории', 2020, 'active'),
    (4, NULL, 'EMP005', 'Беляева', 'Ольга', 'Николаевна', 'Секретарь факультета', 2022, 'active'),
    (5, 13, 'EMP006', 'Рыбаков', 'Илья', 'Сергеевич', 'Технический специалист', 2024, 'active');

-- ============================================================
-- 12. СТУДЕНТЫ С ФИО
-- ============================================================

INSERT INTO students (
    group_id,
    faculty_id,
    student_code,
    last_name,
    first_name,
    middle_name,
    enrollment_year,
    status
) VALUES
    (1, 1, 'STU-0001', 'Иванов', 'Алексей', 'Сергеевич', 2023, 'active'),
    (1, 1, 'STU-0002', 'Петрова', 'Мария', 'Андреевна', 2023, 'active'),
    (1, 1, 'STU-0003', 'Соколов', 'Иван', 'Дмитриевич', 2023, 'active'),

    (2, 1, 'STU-0004', 'Васильева', 'Анна', 'Олеговна', 2024, 'active'),
    (3, 1, 'STU-0005', 'Кузнецов', 'Артем', 'Игоревич', 2023, 'active'),
    (4, 1, 'STU-0006', 'Смирнова', 'Дарья', 'Викторовна', 2024, 'academic_leave'),
    (5, 1, 'STU-0007', 'Морозов', 'Кирилл', 'Алексеевич', 2023, 'active'),

    (6, 2, 'STU-0008', 'Орлова', 'Екатерина', 'Сергеевна', 2023, 'active'),
    (6, 2, 'STU-0009', 'Федоров', 'Максим', 'Павлович', 2023, 'active'),
    (7, 2, 'STU-0010', 'Николаева', 'Софья', 'Ильинична', 2024, 'active'),
    (8, 2, 'STU-0011', 'Алексеев', 'Денис', 'Романович', 2023, 'graduated'),

    (9, 3, 'STU-0012', 'Захарова', 'Полина', 'Евгеньевна', 2023, 'active'),
    (9, 3, 'STU-0013', 'Белов', 'Антон', 'Сергеевич', 2023, 'active'),
    (10, 3, 'STU-0014', 'Калинин', 'Егор', 'Владимирович', 2024, 'active'),
    (11, 3, 'STU-0015', 'Михайлова', 'Виктория', 'Андреевна', 2024, 'active'),

    (12, 4, 'STU-0016', 'Павлов', 'Дмитрий', 'Алексеевич', 2023, 'active'),
    (12, 4, 'STU-0017', 'Романова', 'Алина', 'Сергеевна', 2023, 'active'),
    (13, 4, 'STU-0018', 'Григорьев', 'Никита', 'Олегович', 2024, 'expelled'),

    (14, 5, 'STU-0019', 'Лебедева', 'Полина', 'Ивановна', 2023, 'active'),
    (14, 5, 'STU-0020', 'Козлов', 'Андрей', 'Петрович', 2023, 'active'),
    (15, 5, 'STU-0021', 'Волкова', 'Елена', 'Александровна', 2024, 'active');

-- ============================================================
-- 13. УЧЕБНЫЕ НАЗНАЧЕНИЯ
-- ============================================================

INSERT INTO course_assignments (
    group_id,
    course_id,
    teacher_id,
    semester,
    hours_per_week
) VALUES
    (1, 1, 1, 1, 4),
    (1, 2, 2, 2, 4),
    (1, 3, 1, 2, 3),
    (1, 6, 5, 5, 4),
    (1, 7, 6, 6, 3),

    (2, 1, 2, 1, 4),
    (2, 2, 1, 2, 4),

    (3, 1, 1, 1, 4),
    (3, 2, 2, 2, 4),

    (4, 4, 3, 3, 4),
    (4, 5, 4, 4, 3),

    (5, 6, 5, 5, 4),
    (5, 7, 6, 6, 3),

    (6, 8, 7, 1, 3),
    (6, 9, 8, 2, 3),

    (7, 8, 7, 1, 3),
    (7, 12, 11, 5, 3),

    (8, 10, 9, 3, 3),
    (8, 11, 10, 4, 3),

    (9, 14, 13, 1, 4),
    (9, 15, 14, 2, 3),

    (10, 16, 15, 1, 4),
    (10, 17, 16, 3, 3),

    (11, 18, 17, 1, 3),
    (11, 19, 18, 3, 3),

    (12, 20, 19, 1, 4),
    (12, 21, 20, 2, 3),

    (13, 22, 21, 3, 4),
    (13, 24, 23, 5, 3),

    (14, 25, 25, 1, 4),
    (14, 26, 26, 2, 3),

    (15, 27, 27, 3, 3),
    (15, 28, 28, 4, 3);

-- ============================================================
-- 14. ОЦЕНКИ
-- ============================================================

INSERT INTO grades (
    student_id,
    course_assignment_id,
    grade,
    semester
) VALUES
    (1, 1, 5, 1),
    (1, 2, 4, 2),
    (1, 4, 5, 5),

    (2, 1, 4, 1),
    (2, 2, 5, 2),
    (2, 5, 4, 6),

    (3, 1, 3, 1),
    (3, 2, 4, 2),

    (4, 6, 5, 1),
    (4, 7, 4, 2),

    (5, 8, 5, 1),
    (5, 9, 4, 2),

    (7, 12, 5, 5),
    (7, 13, 5, 6),

    (8, 14, 4, 1),
    (8, 15, 5, 2),

    (9, 14, 3, 1),
    (9, 15, 4, 2),

    (10, 16, 5, 1),
    (10, 17, 4, 5),

    (11, 18, 4, 3),
    (11, 19, 5, 4),

    (12, 20, 5, 1),
    (12, 21, 4, 2),

    (13, 20, 4, 1),
    (13, 21, 3, 2),

    (14, 22, 5, 3),
    (14, 23, 4, 3),

    (16, 26, 5, 1),
    (16, 27, 4, 2),

    (17, 26, 4, 1),
    (17, 27, 5, 2),

    (19, 30, 5, 1),
    (19, 31, 4, 2),

    (20, 30, 4, 1),
    (20, 31, 5, 2),

    (21, 32, 5, 3),
    (21, 33, 4, 4);

-- ============================================================
-- 15. РАСПИСАНИЕ
-- ============================================================

INSERT INTO schedule (
    course_assignment_id,
    day_of_week,
    start_time,
    end_time,
    room
) VALUES
    (1, 1, '09:00', '10:30', '101'),
    (1, 3, '09:00', '10:30', '101'),

    (2, 2, '10:45', '12:15', '202'),
    (2, 4, '10:45', '12:15', '202'),

    (4, 1, '12:30', '14:00', '305'),
    (4, 3, '12:30', '14:00', '305'),

    (5, 2, '14:15', '15:45', '306'),
    (5, 4, '14:15', '15:45', '306'),

    (14, 1, '09:00', '10:30', '401'),
    (14, 3, '09:00', '10:30', '401'),

    (20, 2, '10:45', '12:15', '501'),
    (20, 4, '10:45', '12:15', '501'),

    (26, 1, '12:30', '14:00', '601'),
    (26, 3, '12:30', '14:00', '601'),

    (30, 2, '14:15', '15:45', '701'),
    (30, 4, '14:15', '15:45', '701');

-- ============================================================
-- 16. АБИТУРИЕНТЫ
-- Самый поздний год приёма: 2026
-- ============================================================

INSERT INTO applicants (
    applicant_code,
    study_program_id,
    last_name,
    first_name,
    middle_name,
    exam_score,
    status,
    is_budget,
    enrollment_year
) VALUES
    ('APL-0001', 1, 'Андреев', 'Михаил', 'Сергеевич', 92, 'admitted', TRUE, 2026),
    ('APL-0002', 1, 'Фомина', 'Анна', 'Викторовна', 88, 'waiting', TRUE, 2026),
    ('APL-0003', 2, 'Поляков', 'Даниил', 'Игоревич', 81, 'pending', FALSE, 2026),
    ('APL-0004', 4, 'Воронова', 'Марина', 'Олеговна', 95, 'admitted', TRUE, 2026),
    ('APL-0005', 6, 'Крылов', 'Павел', 'Андреевич', 78, 'admitted', FALSE, 2026),
    ('APL-0006', 9, 'Сафонова', 'Елизавета', 'Сергеевна', 91, 'admitted', TRUE, 2026),
    ('APL-0007', 12, 'Демидов', 'Илья', 'Алексеевич', 84, 'pending', FALSE, 2026),
    ('APL-0008', 14, 'Котова', 'Арина', 'Павловна', 89, 'admitted', TRUE, 2026);

-- ============================================================
-- 17. СТАТИСТИКА ПОСТУПЛЕНИЯ
-- ============================================================

INSERT INTO admission_statistics (
    study_program_id,
    year,
    applicants_count,
    admitted_count,
    avg_exam_score,
    budget_places_filled,
    paid_places_filled
) VALUES
    (1, 2024, 145, 48, 81.60, 30, 18),
    (1, 2025, 160, 50, 83.20, 30, 20),
    (1, 2026, 172, 52, 85.10, 30, 20),

    (2, 2024, 110, 38, 77.40, 25, 13),
    (2, 2025, 125, 40, 79.10, 25, 15),
    (2, 2026, 131, 40, 80.30, 25, 15),

    (4, 2024, 135, 30, 82.50, 20, 10),
    (4, 2025, 148, 30, 84.00, 20, 10),
    (4, 2026, 162, 30, 86.25, 20, 10),

    (6, 2024, 118, 55, 74.40, 35, 20),
    (6, 2025, 126, 58, 76.80, 35, 23),
    (6, 2026, 139, 60, 78.35, 35, 25),

    (9, 2024, 82, 27, 80.20, 20, 7),
    (9, 2025, 95, 28, 81.90, 20, 8),
    (9, 2026, 101, 30, 83.40, 20, 10),

    (12, 2024, 180, 48, 79.00, 30, 18),
    (12, 2025, 195, 50, 81.50, 30, 20),
    (12, 2026, 205, 50, 82.75, 30, 20),

    (14, 2024, 76, 28, 84.30, 20, 8),
    (14, 2025, 88, 30, 85.10, 20, 10),
    (14, 2026, 94, 32, 86.90, 20, 12);

-- ============================================================
-- 18. СТАТИСТИКА УСПЕВАЕМОСТИ
-- Самый поздний учебный год: 2025
-- ============================================================

INSERT INTO academic_statistics (
    faculty_id,
    academic_year,
    semester,
    total_students,
    avg_gpa,
    students_with_debt,
    expelled_count
) VALUES
    (1, 2024, 4, 220, 4.12, 14, 3),
    (1, 2025, 6, 235, 4.18, 11, 2),

    (2, 2024, 4, 180, 3.86, 19, 4),
    (2, 2025, 6, 190, 3.91, 16, 3),

    (3, 2024, 4, 145, 4.05, 10, 2),
    (3, 2025, 6, 150, 4.11, 8, 1),

    (4, 2024, 4, 175, 3.95, 15, 3),
    (4, 2025, 6, 185, 4.02, 12, 2),

    (5, 2024, 4, 125, 4.20, 7, 1),
    (5, 2025, 6, 130, 4.24, 5, 1);

-- ============================================================
-- 19. ПРЕДСТАВЛЕНИЯ
-- ============================================================

CREATE VIEW v_students_anonymized AS
SELECT
    s.id,
    s.student_code,
    s.group_id,
    g.name AS group_name,
    s.faculty_id,
    f.name AS faculty_name,
    sp.name AS program_name,
    s.enrollment_year,
    s.status
FROM students s
JOIN groups g ON g.id = s.group_id
JOIN study_programs sp ON sp.id = g.study_program_id
JOIN faculties f ON f.id = s.faculty_id;

CREATE VIEW v_teachers_anonymized AS
SELECT
    t.id,
    t.teacher_code,
    d.name AS department_name,
    f.name AS faculty_name,
    t.position,
    t.degree,
    t.hire_year
FROM teachers t
JOIN departments d ON d.id = t.department_id
JOIN faculties f ON f.id = d.faculty_id;

CREATE VIEW v_students_by_faculty AS
SELECT
    f.id AS faculty_id,
    f.name AS faculty_name,
    LOWER(f.name) AS faculty_name_lower,
    f.dean_full_name,

    COUNT(s.id) AS total_students,
    COUNT(*) FILTER (WHERE s.status = 'active') AS active_students,
    COUNT(*) FILTER (WHERE s.status = 'graduated') AS graduated_students,
    COUNT(*) FILTER (WHERE s.status = 'academic_leave') AS academic_leave_students,
    COUNT(*) FILTER (WHERE s.status = 'expelled') AS expelled_students
FROM faculties f
LEFT JOIN students s ON s.faculty_id = f.id
GROUP BY f.id, f.name, f.dean_full_name
ORDER BY f.name;

CREATE VIEW v_simple_faculty_stats AS
SELECT
    LOWER(f.name) AS faculty_name,
    COUNT(s.id) AS student_count,
    COUNT(*) FILTER (WHERE s.status = 'active') AS active_count,
    COUNT(*) FILTER (WHERE s.status = 'graduated') AS graduated_count
FROM faculties f
LEFT JOIN students s ON s.faculty_id = f.id
GROUP BY f.id, f.name
ORDER BY f.name;

CREATE VIEW v_faculty_search AS
SELECT
    f.id,
    f.name AS faculty_name,
    LOWER(f.name) AS search_name,

    CASE
        WHEN f.id = 1 THEN
            'технологический факультет,технологический,техфак,ит,информатика,инфотех,информационные технологии'
        WHEN f.id = 2 THEN
            'экономический факультет,экономика,экономический,эконом'
        WHEN f.id = 3 THEN
            'математический факультет,математика,матфак,физика,химия,естественные науки'
        WHEN f.id = 4 THEN
            'юридический факультет,юриспруденция,юридический,право,юрист'
        WHEN f.id = 5 THEN
            'лингвистический факультет,лингвистика,лингвистический,филология,иностранные языки'
    END AS synonyms,

    COUNT(s.id) AS students_count
FROM faculties f
LEFT JOIN students s ON s.faculty_id = f.id
GROUP BY f.id, f.name
ORDER BY f.name;

CREATE VIEW v_faculty_statistics AS
SELECT
    f.id AS faculty_id,
    f.name AS faculty_name,
    LOWER(f.name) AS faculty_name_lower,
    f.dean_full_name,

    COUNT(DISTINCT d.id) AS departments_count,
    COUNT(DISTINCT sp.id) AS programs_count,
    COUNT(DISTINCT s.id) AS students_count,
    COUNT(DISTINCT t.id) AS teachers_count,
    COUNT(DISTINCT e.id) AS employees_count,

    (
        SELECT COUNT(*)
        FROM course_assignments ca
        JOIN groups g ON g.id = ca.group_id
        JOIN study_programs sp2 ON sp2.id = g.study_program_id
        WHERE sp2.faculty_id = f.id
    ) AS course_assignments_count

FROM faculties f
LEFT JOIN departments d ON d.faculty_id = f.id
LEFT JOIN study_programs sp ON sp.faculty_id = f.id
LEFT JOIN students s ON s.faculty_id = f.id
LEFT JOIN teachers t ON t.department_id = d.id
LEFT JOIN employees e ON e.faculty_id = f.id

GROUP BY f.id, f.name, f.dean_full_name
ORDER BY f.name;

-- ============================================================
-- 20. КОММЕНТАРИИ ДЛЯ LLM
-- ============================================================

COMMENT ON VIEW v_students_by_faculty IS
'Статистика студентов по факультетам. Для поиска используйте faculty_name_lower. Пример: WHERE faculty_name_lower = ''технологический факультет''.';

COMMENT ON VIEW v_simple_faculty_stats IS
'Упрощённая статистика студентов по факультетам. Поле faculty_name хранится в нижнем регистре.';

COMMENT ON VIEW v_faculty_search IS
'Поиск факультета по полному названию, сокращению или синониму. Пример: WHERE synonyms LIKE ''%математика%''';

COMMENT ON VIEW v_students_anonymized IS
'Обезличенное представление студентов: не содержит ФИО.';

COMMENT ON VIEW v_teachers_anonymized IS
'Обезличенное представление преподавателей: не содержит ФИО.';

-- ============================================================
-- 21. ПРОВЕРОЧНЫЕ ЗАПРОСЫ
-- ============================================================

-- ФИО студентов
SELECT
    student_code,
    last_name,
    first_name,
    middle_name,
    enrollment_year,
    status
FROM students
ORDER BY last_name, first_name;

-- ФИО преподавателей
SELECT
    teacher_code,
    last_name,
    first_name,
    middle_name,
    position,
    degree,
    hire_year
FROM teachers
ORDER BY last_name, first_name;

-- ФИО сотрудников
SELECT
    employee_code,
    last_name,
    first_name,
    middle_name,
    position,
    hire_year,
    employment_status
FROM employees
ORDER BY last_name, first_name;

-- Последний год среди абитуриентов
SELECT MAX(enrollment_year) AS latest_applicant_year
FROM applicants;

-- Последний год статистики поступления
SELECT MAX(year) AS latest_admission_statistics_year
FROM admission_statistics;

-- Последний учебный год статистики успеваемости
SELECT MAX(academic_year) AS latest_academic_statistics_year
FROM academic_statistics;

-- Самые новые записи по времени создания
SELECT
    'students' AS table_name,
    MAX(created_at) AS latest_created_at
FROM students

UNION ALL

SELECT
    'teachers' AS table_name,
    MAX(created_at) AS latest_created_at
FROM teachers

UNION ALL

SELECT
    'employees' AS table_name,
    MAX(created_at) AS latest_created_at
FROM employees

UNION ALL

SELECT
    'applicants' AS table_name,
    MAX(created_at) AS latest_created_at
FROM applicants

UNION ALL

SELECT
    'admission_statistics' AS table_name,
    MAX(created_at) AS latest_created_at
FROM admission_statistics

ORDER BY latest_created_at DESC;

COMMIT;

-- ============================================================
-- КОНЕЦ СКРИПТА
-- ============================================================
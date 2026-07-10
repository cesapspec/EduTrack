from flask import Flask, flash, render_template, request, redirect, url_for, session, Response, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import mysql.connector
import os
import subprocess
from datetime import datetime
import csv
import io
import getpass
import tempfile
import secrets
import keyring
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives import hmac as crypto_hmac, hashes

KEYRING_SERVICE = "edutrack_backup"
KEYRING_USERNAME = "backup_encryption_key"


def _derive_keys(passphrase: bytes, salt: bytes):
    """Derive a 32-byte AES key and a separate 32-byte HMAC key from the
    passphrase + salt using scrypt. Two distinct derived keys (not the same
    bytes reused for both) so a weakness in one primitive can't cascade
    into the other -- standard encrypt-then-MAC hygiene."""
    kdf = Scrypt(salt=salt, length=64, n=2**14, r=8, p=1)
    derived = kdf.derive(passphrase)
    return derived[:32], derived[32:]

# Backup configuration
MYSQLDUMP_PATH = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe"
BACKUP_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

def create_secure_option_file():
    """
    Writes a MySQL option file containing the DB password for mysqldump
    to read via --defaults-extra-file, then locks its permissions down
    via icacls so only the account running this process can read it.
    (os.chmod is a no-op for real access control on Windows — it only
    toggles the read-only attribute, not NTFS ACLs.)
    Caller is responsible for deleting the returned path.
    """
    fd, path = tempfile.mkstemp(prefix="mysqldump_opts_", suffix=".cnf", dir=BACKUP_FOLDER)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("[client]\n")
            f.write(f"password={os.getenv('DB_PASSWORD')}\n")

        # /inheritance:r strips whatever permissions this file would
        # otherwise inherit from the backups/ folder (e.g. a "Users"
        # group with read access). /grant:r replaces the ACL entirely
        # with just this one entry.
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            check=True, capture_output=True
        )
    except Exception:
        os.remove(path)
        raise
    return path

def run_daily_backup():
    today_str = datetime.now().strftime("%Y-%m-%d")
    backup_filename = f"school_db_backup_{today_str}.sql.enc"
    backup_path = os.path.join(BACKUP_FOLDER, backup_filename)

    if os.path.exists(backup_path):
        print(f"Backup already exists for today: {backup_filename}")
        return

    os.makedirs(BACKUP_FOLDER, exist_ok=True)

    passphrase = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not passphrase:
        print("ERROR: no backup passphrase in Windows Credential Manager. "
              "Run setup_backup_encryption.py once, then retry.")
        return
    passphrase = passphrase.encode("utf-8")

    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)  # CTR initial counter block
    aes_key, hmac_key = _derive_keys(passphrase, salt)

    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(nonce)).encryptor()
    mac = crypto_hmac.HMAC(hmac_key, hashes.SHA256())

    proc = None
    try:
        with open(backup_path, "wb") as out:
            # Header: salt + nonce. Not secret on their own -- only the
            # passphrase is -- but needed by the restore script to
            # re-derive the same keys.
            out.write(salt)
            out.write(nonce)

            proc = subprocess.Popen([
                MYSQLDUMP_PATH,
                "--no-tablespaces",
                "-h", os.getenv("DB_HOST"),
                "-u", os.getenv("DB_USERNAME"),
                f"-p{os.getenv('DB_PASSWORD')}",  # TODO: swap for your defaults-extra-file approach
                os.getenv("DB_DATABASE")
            ], stdout=subprocess.PIPE)

            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                ct_chunk = encryptor.update(chunk)
                mac.update(ct_chunk)
                out.write(ct_chunk)

            proc.stdout.close()
            proc.wait()
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, MYSQLDUMP_PATH)

            out.write(encryptor.finalize())
            out.write(mac.finalize())  # 32-byte tag appended at the end

        print(f"Encrypted backup created successfully: {backup_filename}")
    except subprocess.CalledProcessError as e:
        if os.path.exists(backup_path):
            os.remove(backup_path)  # don't leave a partial/corrupt backup behind
        print(f"Backup failed: {e}")

def cleanup_old_backups(days_to_keep=14):
    if not os.path.exists(BACKUP_FOLDER):
        return

    now = datetime.now()
    deleted_count = 0

    for filename in os.listdir(BACKUP_FOLDER):
        if filename.startswith("school_db_backup_") and filename.endswith(".sql.enc"):
            file_path = os.path.join(BACKUP_FOLDER, filename)
            file_age_days = (now - datetime.fromtimestamp(os.path.getmtime(file_path))).days

            if file_age_days > days_to_keep:
                os.remove(file_path)
                deleted_count += 1

    if deleted_count > 0:
        print(f"Deleted {deleted_count} backup(s) older than {days_to_keep} days.")

def roles_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                flash("You don't have permission to access that page.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapped
    return decorator

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

csrf = CSRFProtect(app)

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash("Your session expired or the form was resubmitted invalidly. Please try again.", "error")
    return redirect(request.referrer or url_for("dashboard"))

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."

# Login manager user class
class User(UserMixin):
    def __init__(self, user_id, username, role, student_id=None, teacher_id=None, must_change_password=False):
        self.id = user_id
        self.username = username
        self.role = role
        self.student_id = student_id
        self.teacher_id = teacher_id
        self.must_change_password = bool(must_change_password)

@login_manager.user_loader
def load_user(user_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    if user:
        return User(user["user_id"], user["username"], user["role"],
                    user.get("student_id"), user.get("teacher_id"),
                    user.get("must_change_password", 0))
    return None

# Connect to MySQL database
def get_conn():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USERNAME'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_DATABASE'),
        auth_plugin='mysql_native_password'
    )

# Any authenticated user with a pending forced password reset gets redirected
# to /account/password no matter what URL they request, until they set a new
# password. This has to run globally (not just as a per-route check) or a
# flagged user could simply avoid the routes that check for it.
PASSWORD_CHANGE_EXEMPT_ENDPOINTS = {"account_password", "logout", "static"}

@app.before_request
def enforce_password_change():
    if current_user.is_authenticated and getattr(current_user, "must_change_password", False):
        if request.endpoint not in PASSWORD_CHANGE_EXEMPT_ENDPOINTS:
            flash("You must change your password before continuing.", "info")
            return redirect(url_for("account_password"))

# Routes
# Login and Logout
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        conn = get_conn()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            if check_password_hash(user["password_hash"], password):
                login_user(User(user["user_id"], user["username"], user["role"],
                                 user.get("student_id"), user.get("teacher_id"),
                                 user.get("must_change_password", 0)))
                if current_user.must_change_password:
                    flash("You must set a new password before continuing.", "info")
                    return redirect(url_for("account_password"))
                return redirect(url_for("dashboard"))
            else:
                error = "Incorrect password."
        else:
            error = "Username not found."

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# Change / set password for the currently logged-in user, regardless of role.
# Used both for the forced-reset flow (must_change_password=1) and for anyone
# who just wants to change their own password without going through Settings
# (which is admin-only).
@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    error = None
    success = None

    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        conn = get_conn()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT password_hash FROM users WHERE user_id = %s", (current_user.id,))
        user = cursor.fetchone()

        if not user or not check_password_hash(user["password_hash"], current_password):
            error = "Current password is incorrect."
        elif new_password != confirm_password:
            error = "New passwords do not match."
        elif len(new_password) < 6:
            error = "New password must be at least 6 characters."
        else:
            new_hash = generate_password_hash(new_password)
            cursor.execute(
                "UPDATE users SET password_hash = %s, must_change_password = 0 WHERE user_id = %s",
                (new_hash, current_user.id)
            )
            conn.commit()
            cursor.close()
            conn.close()
            current_user.must_change_password = False
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard"))

        cursor.close()
        conn.close()

    return render_template("change_password.html",
        error=error, success=success,
        forced=current_user.must_change_password)


# Dashboard
@app.route("/")
@login_required
def dashboard():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    # Get all academic years for the dropdown
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    # Get selected year and semester from URL parameters
    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)

    # If no year selected, use the current one
    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        if current_year:
            selected_year_id = current_year["year_id"]
        else:
            selected_year_id = years[0]["year_id"]

    # Get semesters for selected year
    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters 
            WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    # If no semester selected, use the first one
    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    if current_user.role == 'student':
            my_grades = []
            gpa = None
            if selected_semester_id:
                cursor.execute("""
                    SELECT c.course_name, e.final_grade
                    FROM enrollments e
                    JOIN courses c ON e.course_id = c.course_id
                    WHERE e.student_id = %s AND e.semester_id = %s
                """, (current_user.student_id, selected_semester_id))
                my_grades = cursor.fetchall()

                grade_points = {'S': 4.0, 'A': 3.5, 'B': 3.0, 'C': 2.0, 'D': 1.0, 'F': 0.0}
                points = [grade_points[g['final_grade']] for g in my_grades if g['final_grade'] in grade_points]
                if points:
                    gpa = round(sum(points) / len(points), 2)

            cursor.close()
            conn.close()
            return render_template("student_dashboard.html",
                years=years, semesters=semesters,
                selected_year_id=selected_year_id, selected_semester_id=selected_semester_id,
                my_grades=my_grades, gpa=gpa)

    # Admin & teacher: existing school-wide metrics
    metrics = {"students": 0, "courses": 0, "enrollments": 0}
    top_students = []
    grade_distribution = []

    if selected_semester_id:
        # Active students
        cursor.execute("""
            SELECT COUNT(DISTINCT student_id) as count 
            FROM enrollments 
            WHERE semester_id = %s
        """, (selected_semester_id,))
        metrics["students"] = cursor.fetchone()["count"]

        # Active courses
        cursor.execute("""
            SELECT COUNT(DISTINCT course_id) as count 
            FROM enrollments 
            WHERE semester_id = %s
        """, (selected_semester_id,))
        metrics["courses"] = cursor.fetchone()["count"]

        # Total enrollments
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM enrollments 
            WHERE semester_id = %s
        """, (selected_semester_id,))
        metrics["enrollments"] = cursor.fetchone()["count"]

        # Grades for GPA calculation
        cursor.execute("""
            SELECT 
                s.student_id,
                CONCAT(s.first_name, ' ', COALESCE(s.middle_name, ''), ' ', s.last_name) AS student_name,
                s.grade_level,
                e.final_grade
            FROM enrollments e
            JOIN students s ON e.student_id = s.student_id
            WHERE e.semester_id = %s
            AND e.final_grade IS NOT NULL
        """, (selected_semester_id,))
        raw_grades = cursor.fetchall()

        # Calculate GPA per student in Python
        grade_points = {'S': 4.0, 'A': 3.5, 'B': 3.0, 'C': 2.0, 'D': 1.0, 'F': 0.0}
        student_map = {}
        for row in raw_grades:
            sid = row["student_id"]
            if sid not in student_map:
                student_map[sid] = {
                    "student_name": row["student_name"].strip(),
                    "grade_level": row["grade_level"],
                    "points": []
                }
            grade = row["final_grade"].strip().upper()
            if grade in grade_points:
                student_map[sid]["points"].append(grade_points[grade])

        for sid, data in student_map.items():
            if data["points"]:
                avg = sum(data["points"]) / len(data["points"])
                top_students.append({
                    "student_name": data["student_name"],
                    "grade_level": data["grade_level"],
                    "gpa": round(avg, 2),
                    "courses_taken": len(data["points"])
                })

        top_students = sorted(top_students, key=lambda x: x["gpa"], reverse=True)[:10]

        # Grade distribution
        cursor.execute("""
            SELECT final_grade, COUNT(*) as count
            FROM enrollments
            WHERE semester_id = %s
            AND final_grade IS NOT NULL
            GROUP BY final_grade
            ORDER BY FIELD(final_grade, 'S', 'A', 'B', 'C', 'D', 'F')
        """, (selected_semester_id,))
        grade_distribution = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("dashboard.html",
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id,
        metrics=metrics,
        top_students=top_students,
        grade_distribution=grade_distribution
    )

# Students
# Whitelist of allowed sort keys -> real SQL columns/expressions.
# NEVER let a raw URL value become a column name directly in SQL.
STUDENT_SORT_COLUMNS = {
    "name": "s.last_name",
    "email": "s.email",
    "grade": "s.grade_level",
    "courses": "course_count",
}
STUDENTS_PER_PAGE = 20

@app.route("/students")
@login_required
@roles_required("admin", "teacher")
def students():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    # Get semester filter from URL
    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)
    selected_grade = request.args.get("grade_level", type=int)

    # Search, sort, and pagination params
    search = request.args.get("search", "").strip()
    sort = request.args.get("sort", "name")
    if sort not in STUDENT_SORT_COLUMNS:
        sort = "name"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    # Get all years
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    # Default to current year
    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        selected_year_id = current_year["year_id"] if current_year else years[0]["year_id"]

    # Get semesters
    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters 
            WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    # Build WHERE clause pieces. Each filter adds its own placeholder
    # and matching parameter, in the same order, to keep them in sync.
    where_clauses = ["1=1"]
    where_params = []

    # Data-level scoping: a teacher only ever sees students in courses they teach
    if current_user.role == 'teacher':
        where_clauses.append("""
            s.student_id IN (
                SELECT e2.student_id FROM enrollments e2
                JOIN courses c2 ON e2.course_id = c2.course_id
                WHERE c2.teacher_id = %s
            )
        """)
        where_params.append(current_user.teacher_id)

    if selected_grade:
        where_clauses.append("s.grade_level = %s")
        where_params.append(selected_grade)

    if search:
        where_clauses.append(
            "(CONCAT(s.first_name, ' ', COALESCE(s.middle_name, ''), ' ', s.last_name) LIKE %s "
            "OR s.email LIKE %s)"
        )
        like_term = f"%{search}%"
        where_params.append(like_term)
        where_params.append(like_term)

    where_sql = " AND ".join(where_clauses)

    # Count total matching students (for pagination), independent of LIMIT/OFFSET
    cursor.execute(f"""
        SELECT COUNT(*) as total
        FROM students s
        WHERE {where_sql}
    """, where_params)
    total_count = cursor.fetchone()["total"]
    total_pages = max(1, (total_count + STUDENTS_PER_PAGE - 1) // STUDENTS_PER_PAGE)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * STUDENTS_PER_PAGE

    # Main query: enrollment count needs the semester param first (used inside the JOIN),
    # then the WHERE params, then LIMIT/OFFSET params at the end.
    sort_column = STUDENT_SORT_COLUMNS[sort]
    # direction is validated above against a fixed set, so it's safe to
    # interpolate directly here (it can only ever be "asc" or "desc")
    query_params = [selected_semester_id] + where_params + [STUDENTS_PER_PAGE, offset]

    cursor.execute(f"""
        SELECT 
            s.student_id,
            CONCAT(s.first_name, ' ', COALESCE(s.middle_name, ''), ' ', s.last_name) AS full_name,
            s.email,
            s.grade_level,
            COUNT(e.enrollment_id) as course_count
        FROM students s
        LEFT JOIN enrollments e ON s.student_id = e.student_id 
            AND e.semester_id = %s
        WHERE {where_sql}
        GROUP BY s.student_id
        ORDER BY {sort_column} {direction.upper()}
        LIMIT %s OFFSET %s
    """, query_params)

    students = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("students.html",
        students=students,
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id,
        selected_grade=selected_grade,
        search=search,
        sort=sort,
        direction=direction,
        page=page,
        total_pages=total_pages,
        total_count=total_count
    )

# Student Detail
@app.route("/students/<int:student_id>")
@login_required
def student_detail(student_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    # Data-level authorization: build the WHERE clause based on WHO is asking,
    # not just whether they're logged in.
    if current_user.role == 'admin':
        pass  # no extra restriction
    elif current_user.role == 'teacher':
        cursor.execute("""
            SELECT 1 FROM enrollments e
            WHERE e.student_id = %s
            AND e.course_id IN (SELECT course_id FROM courses WHERE teacher_id = %s)
            LIMIT 1
        """, (student_id, current_user.teacher_id))
        if not cursor.fetchone():
            flash("You don't have access to that student's record.", "error")
            return redirect(url_for('dashboard'))
    elif current_user.role == 'student':
        if student_id != current_user.student_id:
            flash("You don't have access to that student's record.", "error")
            return redirect(url_for('dashboard'))

    # ... rest of the existing route logic continues unchanged below

    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)

    # Get all years
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        selected_year_id = current_year["year_id"] if current_year else years[0]["year_id"]

    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    # Get student info
    cursor.execute("""
        SELECT student_id, first_name, middle_name, last_name, date_of_birth,
               email, address, guardian_name, guardian_phone,
               grade_level, enrollment_date
        FROM students WHERE student_id = %s
    """, (student_id,))
    student = cursor.fetchone()

    if not student:
        cursor.close()
        conn.close()
        return redirect(url_for("students"))

    # Build full name
    middle = f" {student['middle_name']} " if student['middle_name'] else " "
    student['full_name'] = f"{student['first_name']}{middle}{student['last_name']}"

    # Get grades for selected semester
    grades = []
    gpa = None
    if selected_semester_id:
        cursor.execute("""
            SELECT 
                c.course_name,
                COALESCE(e.final_grade, 'Not graded') AS final_grade,
                DATE(e.enrollment_date) AS enrolled_date,
                sem.semester_name,
                ay.year_name
            FROM enrollments e
            JOIN courses c ON e.course_id = c.course_id
            JOIN semesters sem ON e.semester_id = sem.semester_id
            JOIN academic_years ay ON e.academic_year_id = ay.year_id
            WHERE e.student_id = %s
            AND e.semester_id = %s
            ORDER BY c.course_name
        """, (student_id, selected_semester_id))
        grades = cursor.fetchall()

        # Calculate GPA
        grade_points = {'S': 4.0, 'A': 3.5, 'B': 3.0, 'C': 2.0, 'D': 1.0, 'F': 0.0}
        points = [grade_points[g['final_grade']] for g in grades if g['final_grade'] in grade_points]
        if points:
            gpa = round(sum(points) / len(points), 2)

    cursor.close()
    conn.close()

    return render_template("student_detail.html",
        student=student,
        grades=grades,
        gpa=gpa,
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id
    )

# Add Student
@app.route("/add-student", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def add_student():
    error = None
    success = None

    if request.method == "POST":
        fname = request.form["fname"].strip()
        mname = request.form["mname"].strip() or None
        lname = request.form["lname"].strip()
        date_of_birth = request.form.get("date_of_birth") or None
        email = request.form["email"].strip()
        address = request.form.get("address", "").strip() or None
        guardian_name = request.form.get("guardian_name", "").strip() or None
        guardian_phone = request.form.get("guardian_phone", "").strip() or None
        grade_level = request.form["grade_level"]

        if not all([fname, lname, email, grade_level]):
            error = "Please fill in all required fields."
        else:
            conn = get_conn()
            cursor = conn.cursor(buffered=True)
            try:
                cursor.execute(
                    """INSERT INTO students 
                    (first_name, middle_name, last_name, date_of_birth, email, 
                     address, guardian_name, guardian_phone, grade_level) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (fname, mname, lname, date_of_birth, email, 
                     address, guardian_name, guardian_phone, grade_level)
                )
                conn.commit()
                success = f"Student {fname} {lname} added successfully."
            except mysql.connector.IntegrityError:
                error = "A student with this email already exists."
            except mysql.connector.Error as e:
                error = f"Database error: {str(e)}"
            finally:
                cursor.close()
                conn.close()

    return render_template("add_student.html", error=error, success=success)

# Grades
@app.route("/grades", methods=["GET", "POST"])
@login_required
@roles_required("admin", "teacher") #students can view their own grades on the dashboard, so no need to allow them here
def grades():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)

    error = None
    success = None

    # Get semester filter from URL
    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)

    # Get all years
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    # Default to current year
    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        selected_year_id = current_year["year_id"] if current_year else years[0]["year_id"]

    # Get semesters
    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters 
            WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    # Handle form submissions
    if request.method == "POST":
        action = request.form.get("action")

        # Enroll student
        if action == "enroll":
            if current_user.role != 'admin':
                error = "Only admins can enroll students."
            else:
                student_id = request.form.get("student_id")
                course_id = request.form.get("course_id")

            if not all([student_id, course_id, selected_semester_id]):
                error = "Please select a student, course, and semester."
            else:
                try:
                    cursor.execute("""
                        INSERT INTO enrollments 
                        (student_id, course_id, semester_id, academic_year_id)
                        VALUES (%s, %s, %s, %s)
                    """, (student_id, course_id, selected_semester_id, selected_year_id))
                    conn.commit()
                    success = "Student enrolled successfully."
                except mysql.connector.IntegrityError:
                    error = "This student is already enrolled in that course."
                except mysql.connector.Error as e:
                    error = f"Database error: {str(e)}"

        # Update grade
        elif action == "update_grade":
            enrollment_id = request.form.get("enrollment_id")
            new_grade = request.form.get("new_grade")

            if not all([enrollment_id, new_grade]):
                error = "Please select an enrollment and a grade."
            else:
            # Ownership check: a teacher can only grade enrollments in courses THEY teach
                if current_user.role == 'teacher':
                    cursor.execute("""
                        SELECT 1 FROM enrollments e
                        JOIN courses c ON e.course_id = c.course_id
                        WHERE e.enrollment_id = %s AND c.teacher_id = %s
                    """, (enrollment_id, current_user.teacher_id))
                    if not cursor.fetchone():
                        error = "You can't update grades for a course you don't teach."

                if not error:
                    cursor.execute("UPDATE enrollments SET final_grade = %s WHERE enrollment_id = %s",
                                   (new_grade, enrollment_id))
                    conn.commit()
                    success = f"Grade updated to {new_grade} successfully."

    # Enrollments list — scoped for teachers to only their own courses' enrollments
    enrollments = []
    if selected_semester_id:
        where = "e.semester_id = %s"
        params = [selected_semester_id]
        if current_user.role == 'teacher':
            where += " AND c.teacher_id = %s"
            params.append(current_user.teacher_id)

        cursor.execute(f"""
            SELECT e.enrollment_id,
                   CONCAT(s.first_name, ' ', COALESCE(s.middle_name, ''), ' ', s.last_name) AS student_name,
                   s.grade_level, c.course_name, DATE(e.enrollment_date) AS enrollment_date,
                   COALESCE(e.final_grade, 'Not graded') AS final_grade
            FROM enrollments e
            JOIN students s ON e.student_id = s.student_id
            JOIN courses c ON e.course_id = c.course_id
            WHERE {where}
            ORDER BY s.last_name, c.course_name
        """, params)
        enrollments = cursor.fetchall()

    # Dropdown data — only admins need the full lists (enroll form is admin-only)
    all_students, all_courses = [], []
    if current_user.role == 'admin':
        cursor.execute("SELECT student_id, CONCAT(first_name, ' ', last_name) AS full_name FROM students ORDER BY last_name")
        all_students = cursor.fetchall()
        cursor.execute("SELECT course_id, course_name FROM courses ORDER BY course_name")
        all_courses = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("grades.html",
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id,
        enrollments=enrollments,
        all_students=all_students,
        all_courses=all_courses,
        error=error,
        success=success
    )

# Courses
@app.route("/courses")
@login_required
def courses():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)

    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)

    # Get all years
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    # Default to current year
    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        selected_year_id = current_year["year_id"] if current_year else years[0]["year_id"]

    # Get semesters
    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters 
            WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    # Get all courses with teacher and enrollment count
    cursor.execute("""
        SELECT 
            c.course_id,
            c.course_name,
            c.capacity,
            CONCAT(t.first_name, ' ', t.last_name) AS teacher,
            COUNT(e.enrollment_id) AS enrolled
        FROM courses c
        JOIN teachers t ON c.teacher_id = t.teacher_id
        LEFT JOIN enrollments e ON c.course_id = e.course_id
            AND e.semester_id = %s
        GROUP BY c.course_id
        ORDER BY c.course_name
    """, (selected_semester_id,))
    courses = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("courses.html",
        courses=courses,
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id
    )

# Course Detail
@app.route("/courses/<int:course_id>")
@login_required
def course_detail(course_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)

    if current_user.role == 'teacher':
        cursor.execute("SELECT 1 FROM courses WHERE course_id = %s AND teacher_id = %s",
                       (course_id, current_user.teacher_id))
        if not cursor.fetchone():
            flash("You don't have access to that course.", "error")
            return redirect(url_for('dashboard'))
    elif current_user.role == 'student':
        cursor.execute("SELECT 1 FROM enrollments WHERE course_id = %s AND student_id = %s LIMIT 1",
                       (course_id, current_user.student_id))
        if not cursor.fetchone():
            flash("You don't have access to that course.", "error")
            return redirect(url_for('dashboard'))

    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)

    # Get all years
    cursor.execute("SELECT year_id, year_name FROM academic_years ORDER BY year_name DESC")
    years = cursor.fetchall()

    if not selected_year_id and years:
        cursor.execute("SELECT year_id FROM academic_years WHERE is_current = 1 LIMIT 1")
        current_year = cursor.fetchone()
        selected_year_id = current_year["year_id"] if current_year else years[0]["year_id"]

    semesters = []
    if selected_year_id:
        cursor.execute("""
            SELECT semester_id, semester_name 
            FROM semesters WHERE year_id = %s 
            ORDER BY semester_order
        """, (selected_year_id,))
        semesters = cursor.fetchall()

    if not selected_semester_id and semesters:
        selected_semester_id = semesters[0]["semester_id"]

    # Get course info
    cursor.execute("""
        SELECT 
            c.course_id,
            c.course_name,
            c.capacity,
            CONCAT(t.first_name, ' ', t.last_name) AS teacher,
            t.email AS teacher_email
        FROM courses c
        JOIN teachers t ON c.teacher_id = t.teacher_id
        WHERE c.course_id = %s
    """, (course_id,))
    course = cursor.fetchone()

    if not course:
        cursor.close()
        conn.close()
        return redirect(url_for("courses"))

    # Get enrolled students
    students = []
    grade_distribution = []
    enrollment_count = 0

    if selected_semester_id:
        cursor.execute("""
            SELECT 
                CONCAT(s.first_name, ' ', COALESCE(s.middle_name, ''), ' ', s.last_name) AS student_name,
                s.grade_level,
                COALESCE(e.final_grade, 'Not graded') AS final_grade,
                DATE(e.enrollment_date) AS enrolled_on
            FROM enrollments e
            JOIN students s ON e.student_id = s.student_id
            WHERE e.course_id = %s
            AND e.semester_id = %s
            ORDER BY s.last_name
        """, (course_id, selected_semester_id))
        students = cursor.fetchall()
        enrollment_count = len(students)

        # Grade distribution
        cursor.execute("""
            SELECT final_grade, COUNT(*) as count
            FROM enrollments
            WHERE course_id = %s
            AND semester_id = %s
            AND final_grade IS NOT NULL
            GROUP BY final_grade
            ORDER BY FIELD(final_grade, 'S', 'A', 'B', 'C', 'D', 'F')
        """, (course_id, selected_semester_id))
        grade_distribution = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("course_detail.html",
        course=course,
        students=students,
        grade_distribution=grade_distribution,
        enrollment_count=enrollment_count,
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id
    )

#Settings
@app.route("/settings", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def settings():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)

    error = request.args.get("error", None)
    success = request.args.get("success", None)
    active_tab = request.args.get("tab", "academic")

    # ── ACADEMIC YEARS & SEMESTERS ──
    if request.method == "POST" and request.form.get("action") == "add_year":
        year_name = request.form.get("year_name", "").strip()
        is_current = 1 if request.form.get("is_current") else 0
        if not year_name:
            error = "Year name is required."
        else:
            try:
                if is_current:
                    cursor.execute("UPDATE academic_years SET is_current = 0")
                    conn.commit()
                cursor.execute(
                    "INSERT INTO academic_years (year_name, is_current) VALUES (%s, %s)",
                    (year_name, is_current)
                )
                conn.commit()
                success = f"Academic year {year_name} added."
                active_tab = "academic"
            except mysql.connector.IntegrityError:
                error = "That academic year already exists."

    elif request.method == "POST" and request.form.get("action") == "add_semester":
        year_id = request.form.get("year_id")
        semester_name = request.form.get("semester_name", "").strip()
        semester_order = request.form.get("semester_order")
        if not all([year_id, semester_name, semester_order]):
            error = "All semester fields are required."
        else:
            try:
                cursor.execute(
                    """INSERT INTO semesters (year_id, semester_name, semester_order)
                    VALUES (%s, %s, %s)""",
                    (year_id, semester_name, semester_order)
                )
                conn.commit()
                success = f"Semester {semester_name} added."
                active_tab = "academic"
            except mysql.connector.IntegrityError:
                error = "That semester already exists for this year."

    elif request.method == "POST" and request.form.get("action") == "set_current_year":
        year_id = request.form.get("year_id")
        if year_id:
            cursor.execute("UPDATE academic_years SET is_current = 0")
            conn.commit()
            cursor.execute(
                "UPDATE academic_years SET is_current = 1 WHERE year_id = %s",
                (year_id,)
            )
            conn.commit()
            success = "Current academic year updated."
            active_tab = "academic"

    # ── TEACHERS ──
    elif request.method == "POST" and request.form.get("action") == "add_teacher":
        fname = request.form.get("fname", "").strip()
        lname = request.form.get("lname", "").strip()
        email = request.form.get("email", "").strip()
        hire_date = request.form.get("hire_date")
        if not all([fname, lname, email, hire_date]):
            error = "All teacher fields are required."
        else:
            try:
                cursor.execute(
                    """INSERT INTO teachers (first_name, last_name, email, hire_date)
                    VALUES (%s, %s, %s, %s)""",
                    (fname, lname, email, hire_date)
                )
                conn.commit()
                success = f"Teacher {fname} {lname} added."
                active_tab = "teachers"
            except mysql.connector.IntegrityError:
                error = "A teacher with this email already exists."

    # ── COURSES ──
    elif request.method == "POST" and request.form.get("action") == "add_course":
        course_name = request.form.get("course_name", "").strip()
        description = request.form.get("description", "").strip() or None
        capacity = request.form.get("capacity")
        teacher_id = request.form.get("teacher_id")
        if not all([course_name, capacity, teacher_id]):
            error = "Course name, capacity and teacher are required."
        else:
            try:
                cursor.execute(
                    """INSERT INTO courses (course_name, description, capacity, teacher_id)
                    VALUES (%s, %s, %s, %s)""",
                    (course_name, description, capacity, teacher_id)
                )
                conn.commit()
                success = f"Course {course_name} added."
                active_tab = "courses"
            except mysql.connector.IntegrityError:
                error = "That course already exists."

    # ── ACCOUNT ──
    elif request.method == "POST" and request.form.get("action") == "change_password":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        cursor.execute(
            "SELECT password_hash FROM users WHERE user_id = %s",
            (current_user.id,)
        )
        user = cursor.fetchone()

        if not user or not check_password_hash(user["password_hash"], current_password):
            error = "Current password is incorrect."
            active_tab = "account"
        elif new_password != confirm_password:
            error = "New passwords do not match."
            active_tab = "account"
        elif len(new_password) < 6:
            error = "New password must be at least 6 characters."
            active_tab = "account"
        else:
            new_hash = generate_password_hash(new_password)
            cursor.execute(
                "UPDATE users SET password_hash = %s, must_change_password = 0 WHERE user_id = %s",
                (new_hash, current_user.id)
            )
            conn.commit()
            success = "Password changed successfully."
            active_tab = "account"

    # ── FETCH DATA FOR DISPLAY ──
    cursor.execute(
        "SELECT year_id, year_name, is_current FROM academic_years ORDER BY year_name DESC"
    )
    academic_years = cursor.fetchall()

    cursor.execute("""
        SELECT s.semester_id, s.semester_name, s.semester_order, ay.year_name
        FROM semesters s
        JOIN academic_years ay ON s.year_id = ay.year_id
        ORDER BY ay.year_name DESC, s.semester_order
    """)
    semesters = cursor.fetchall()

    cursor.execute(
        "SELECT teacher_id, first_name, last_name, email, hire_date FROM teachers ORDER BY last_name"
    )
    teachers = cursor.fetchall()

    cursor.execute("""
        SELECT c.course_id, c.course_name, c.description, c.capacity,
               CONCAT(t.first_name, ' ', t.last_name) AS teacher
        FROM courses c
        JOIN teachers t ON c.teacher_id = t.teacher_id
        ORDER BY c.course_name
    """)
    courses = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("settings.html",
        active_tab=active_tab,
        academic_years=academic_years,
        semesters=semesters,
        teachers=teachers,
        courses=courses,
        error=error,
        success=success
    )

# Delete routes
@app.route("/settings/delete/year/<int:year_id>", methods=["POST"])
@login_required
@roles_required("admin")
def delete_year(year_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        # Check if year has semesters
        cursor.execute(
            "SELECT COUNT(*) as count FROM semesters WHERE year_id = %s",
            (year_id,)
        )
        count = cursor.fetchone()["count"]
        if count > 0:
            # Delete will cascade to semesters but check enrollments first
            cursor.execute("""
                SELECT COUNT(*) as count FROM enrollments e
                JOIN semesters s ON e.semester_id = s.semester_id
                WHERE s.year_id = %s
            """, (year_id,))
            enrollment_count = cursor.fetchone()["count"]
            if enrollment_count > 0:
                return redirect(url_for("settings", tab="academic",
                    error="Cannot delete this year. It has active enrollments attached to it."))

        cursor.execute("DELETE FROM academic_years WHERE year_id = %s", (year_id,))
        conn.commit()
    except mysql.connector.Error as e:
        return redirect(url_for("settings", tab="academic",
            error=f"Database error: {str(e)}"))
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("settings", tab="academic",
        success="Academic year deleted successfully."))


@app.route("/settings/delete/semester/<int:semester_id>", methods=["POST"])
@login_required
@roles_required("admin")
def delete_semester(semester_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        # Check if semester has enrollments
        cursor.execute(
            "SELECT COUNT(*) as count FROM enrollments WHERE semester_id = %s",
            (semester_id,)
        )
        count = cursor.fetchone()["count"]
        if count > 0:
            return redirect(url_for("settings", tab="academic",
                error=f"Cannot delete this semester. It has {count} enrollment(s) attached to it."))

        cursor.execute("DELETE FROM semesters WHERE semester_id = %s", (semester_id,))
        conn.commit()
    except mysql.connector.Error as e:
        return redirect(url_for("settings", tab="academic",
            error=f"Database error: {str(e)}"))
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("settings", tab="academic",
        success="Semester deleted successfully."))


@app.route("/settings/delete/teacher/<int:teacher_id>", methods=["POST"])
@login_required
@roles_required("admin")
def delete_teacher(teacher_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        # Check if teacher has courses assigned
        cursor.execute(
            "SELECT COUNT(*) as count FROM courses WHERE teacher_id = %s",
            (teacher_id,)
        )
        count = cursor.fetchone()["count"]
        if count > 0:
            return redirect(url_for("settings", tab="teachers",
                error=f"Cannot delete this teacher. They are assigned to {count} course(s). Reassign the courses first."))

        cursor.execute("DELETE FROM teachers WHERE teacher_id = %s", (teacher_id,))
        conn.commit()
    except mysql.connector.Error as e:
        return redirect(url_for("settings", tab="teachers",
            error=f"Database error: {str(e)}"))
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("settings", tab="teachers",
        success="Teacher deleted successfully."))


@app.route("/settings/delete/course/<int:course_id>", methods=["POST"])
@login_required
@roles_required("admin")
def delete_course(course_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        # Check if course has enrollments
        cursor.execute(
            "SELECT COUNT(*) as count FROM enrollments WHERE course_id = %s",
            (course_id,)
        )
        count = cursor.fetchone()["count"]
        if count > 0:
            return redirect(url_for("settings", tab="courses",
                error=f"Cannot delete this course. It has {count} enrollment(s). Remove enrollments first."))

        cursor.execute("DELETE FROM courses WHERE course_id = %s", (course_id,))
        conn.commit()
    except mysql.connector.Error as e:
        return redirect(url_for("settings", tab="courses",
            error=f"Database error: {str(e)}"))
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("settings", tab="courses",
        success="Course deleted successfully."))

# Edit student details
@app.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def edit_student(student_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)
    error = None

    if request.method == "POST":
        fname = request.form["fname"].strip()
        mname = request.form["mname"].strip() or None
        lname = request.form["lname"].strip()
        date_of_birth = request.form.get("date_of_birth") or None
        email = request.form["email"].strip()
        address = request.form.get("address", "").strip() or None
        guardian_name = request.form.get("guardian_name", "").strip() or None
        guardian_phone = request.form.get("guardian_phone", "").strip() or None
        grade_level = request.form["grade_level"]

        if not all([fname, lname, email, grade_level]):
            error = "Please fill in all required fields."
        else:
            try:
                cursor.execute("""
                    UPDATE students SET
                        first_name = %s, middle_name = %s, last_name = %s,
                        date_of_birth = %s, email = %s, address = %s,
                        guardian_name = %s, guardian_phone = %s, grade_level = %s
                    WHERE student_id = %s
                """, (fname, mname, lname, date_of_birth, email, address,
                      guardian_name, guardian_phone, grade_level, student_id))
                conn.commit()
                cursor.close()
                conn.close()
                return redirect(url_for("student_detail", student_id=student_id))
            except mysql.connector.IntegrityError:
                error = "Another student with this email already exists."
            except mysql.connector.Error as e:
                error = f"Database error: {str(e)}"

    # GET request — load current student data into the form
    cursor.execute("""
        SELECT first_name, middle_name, last_name, date_of_birth,
               email, address, guardian_name, guardian_phone, grade_level
        FROM students WHERE student_id = %s
    """, (student_id,))
    student = cursor.fetchone()
    cursor.close()
    conn.close()

    if not student:
        return redirect(url_for("students"))

    return render_template("edit_student.html", 
        student=student, student_id=student_id, error=error)

# Import Students
@app.route("/students/import", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def import_students():
    results = None
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            results = {"error": "No file selected."}
        elif not file.filename.lower().endswith(".csv"):
            results = {"error": "Please upload a .csv file."}
        else:
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)

            added, updated, skipped = [], [], []
            conn = get_conn()
            cursor = conn.cursor(dictionary=True, buffered=True)

            for row_num, row in enumerate(reader, start=2):  # row 1 is the header
                fname = (row.get("first_name") or "").strip()
                mname = (row.get("middle_name") or "").strip() or None
                lname = (row.get("last_name") or "").strip()
                email = (row.get("email") or "").strip()
                dob = (row.get("date_of_birth") or "").strip() or None
                address = (row.get("address") or "").strip() or None
                guardian_name = (row.get("guardian_name") or "").strip() or None
                guardian_phone = (row.get("guardian_phone") or "").strip() or None
                grade_level = (row.get("grade_level") or "").strip()

                if not fname or not lname or not email or not grade_level:
                    skipped.append((row_num, "Missing a required field (first_name, last_name, email, grade_level)"))
                    continue
                if "@" not in email:
                    skipped.append((row_num, f"Invalid email: {email}"))
                    continue
                if grade_level not in ("9", "10", "11", "12"):
                    skipped.append((row_num, f"Invalid grade_level: {grade_level}"))
                    continue

                try:
                    cursor.execute("SELECT * FROM students WHERE email = %s", (email,))
                    existing = cursor.fetchone()

                    new_values = {
                        "first_name": fname, "middle_name": mname, "last_name": lname,
                        "date_of_birth": dob, "address": address,
                        "guardian_name": guardian_name, "guardian_phone": guardian_phone,
                        "grade_level": grade_level
                    }

                    if existing:
                        changes = [
                            f"{field}: '{existing.get(field)}' -> '{new_val}'"
                            for field, new_val in new_values.items()
                            if str(existing.get(field)) != str(new_val) and not (existing.get(field) is None and new_val is None)
                        ]
                        if changes:
                            cursor.execute("""
                                UPDATE students SET first_name=%s, middle_name=%s, last_name=%s,
                                    date_of_birth=%s, address=%s, guardian_name=%s,
                                    guardian_phone=%s, grade_level=%s
                                WHERE email=%s
                            """, (fname, mname, lname, dob, address, guardian_name,
                                  guardian_phone, grade_level, email))
                            updated.append((email, changes))
                    else:
                        cursor.execute("""
                            INSERT INTO students (first_name, middle_name, last_name,
                                date_of_birth, email, address, guardian_name, guardian_phone, grade_level)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (fname, mname, lname, dob, email, address,
                              guardian_name, guardian_phone, grade_level))
                        added.append(email)

                except mysql.connector.Error as e:
                    skipped.append((row_num, f"Database error: {str(e)}"))

            conn.commit()
            cursor.close()
            conn.close()
            results = {"added": added, "updated": updated, "skipped": skipped}

    return render_template("import_students.html", results=results)

# Export Template
@app.route("/students/import/template")
@login_required
@roles_required("admin")
def student_import_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "first_name", "middle_name", "last_name", "date_of_birth",
        "email", "address", "guardian_name", "guardian_phone", "grade_level"
    ])
    writer.writerow([
        "Jane", "M", "Doe", "2010-04-12",
        "jane.doe@example.com", "123 Main St", "John Doe", "555-0100", "9"
    ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=student_import_template.csv"}
    )

if __name__ == '__main__':
    run_daily_backup()
    cleanup_old_backups()
    app.run(debug=True)
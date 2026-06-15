from flask import Flask, render_template, request, redirect, url_for
from dotenv import load_dotenv
import mysql.connector
import os

load_dotenv()

app = Flask(__name__)

# Connect to MySQL database
def get_conn():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USERNAME'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_DATABASE'),
        auth_plugin='mysql_native_password'
    )

# Routes
@app.route("/")
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

    # Dashboard metrics
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
@app.route("/students")
def students():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    
    # Get semester filter from URL
    selected_year_id = request.args.get("year_id", type=int)
    selected_semester_id = request.args.get("semester_id", type=int)
    selected_grade = request.args.get("grade_level", type=int)

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

    # Get all students with enrollment count for selected semester
    grade_filter = "AND s.grade_level = %s" if selected_grade else ""
    params = [selected_semester_id]
    if selected_grade:
        params.append(selected_grade)

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
        WHERE 1=1 {grade_filter}
        GROUP BY s.student_id
        ORDER BY s.last_name
    """, params)

    students = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("students.html",
        students=students,
        years=years,
        semesters=semesters,
        selected_year_id=selected_year_id,
        selected_semester_id=selected_semester_id,
        selected_grade=selected_grade
    )


@app.route("/students/<int:student_id>")
def student_detail(student_id):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

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
        SELECT first_name, middle_name, last_name, 
               email, grade_level, enrollment_date
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
def add_student():
    if request.method == "POST":
        fname = request.form["fname"]
        lname = request.form["lname"]
        email = request.form["email"]
        grade_level = request.form["grade_level"]
        
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO students (first_name, last_name, email, grade_level) VALUES (%s, %s, %s, %s)",
            (fname, lname, email, grade_level)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for("students"))
    
    return render_template("add_student.html")

if __name__ == '__main__':
    app.run(debug=True)

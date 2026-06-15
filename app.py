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
@app.route('/')
def dashboard():
    return render_template('dashboard.html')

# Students
@app.route("/students")
def students():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT student_id, first_name, last_name, grade_level FROM students ORDER BY last_name")
    students = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("students.html", students=students)

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

# EduTrack

A Flask + MySQL school database management system for tracking students,
courses, enrollments, and grades across academic years and semesters.
Built as a hands-on security/DBA learning project — the goal is a genuinely
production-conscious app, not just a portfolio demo.

## Features

- Role-based access control (admin / teacher / student-parent), enforced at
  both the route level and the data level (e.g. a teacher can only view or
  grade students in courses they actually teach, not just any student ID).
- Passwords hashed with werkzeug's salted scrypt (not raw SHA256), with a
  forced-password-reset flow for migrated/seeded accounts.
- CSRF protection (Flask-WTF) on every state-changing form.
- Encrypted-at-rest daily backups: `mysqldump` output is streamed straight
  into AES-256-CTR encryption with an HMAC-SHA256 integrity tag — plaintext
  SQL never touches disk. The encryption passphrase is stored in Windows
  Credential Manager (via `keyring`), not in a config file.
- CSV student import/export with per-row validation.

## Tech Stack

- Python 3 / Flask
- MySQL (developed against MySQL Server 8.0)
- Flask-Login, Flask-WTF, werkzeug
- `cryptography` + `keyring` for backup encryption

## Setup

### 1. Clone and create a virtual environment

```powershell
git clone <your-repo-url>
cd EduTrack
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in real values:

```
DB_USERNAME=your_username_here
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_DATABASE=school_db
SECRET_KEY=generate-a-long-random-value-here
```

`SECRET_KEY` should be a long random string (used by Flask for session
signing) — don't reuse the placeholder from `.env.example`.

### 3. Create the database and import the schema

```sql
CREATE DATABASE school_db;
```

```powershell
mysql -u your_username -p school_db < database\schema.sql
```

### 4. Create the first admin account

The app has no public registration route by design — accounts are
provisioned directly. Generate a proper password hash:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourStrongPasswordHere'))"
```

Then insert the account:

```sql
INSERT INTO users (username, password_hash, role, must_change_password)
VALUES ('admin', 'scrypt:...paste-hash-here...', 'admin', 0);
```

### 5. (Optional) Enable encrypted backups

Requires `mysqldump.exe` — update the `MYSQLDUMP_PATH` constant in `app.py`
if your MySQL install location differs from the default. Then seed the
backup encryption passphrase once per machine:

```powershell
python setup_backup_encryption.py
```

This stores the passphrase in Windows Credential Manager. Backups run
automatically on app startup (`run_daily_backup()`), skipping if a backup
for the current day already exists.

To restore/verify a backup:

```powershell
python decrypt_backup.py backups\school_db_backup_2026-07-10.sql.enc restored.sql
```

### 6. Run the app

```powershell
python app.py
```

## Project Structure

```
EduTrack/
├── app.py                       # Main Flask application
├── migrate_password_hashing.py  # One-time SHA256 -> scrypt migration
├── setup_backup_encryption.py   # One-time backup passphrase setup
├── decrypt_backup.py            # Backup restore/verification tool
├── database/
│   └── schema.sql               # Table structure only, no data
├── templates/                   # Jinja2 templates
├── backups/                     # Encrypted daily backups (gitignored)
├── .env.example                 # Template for required environment variables
└── requirements.txt
```

## Known Limitations / In Progress

This project is being built and audited incrementally. Currently open items:

- Raw database exception text is shown to users in several routes
  (information disclosure risk) — should log server-side and show generic
  messages instead.
- No rate limiting or account lockout on `/login` (brute-force exposure).
- Session cookie flags (`SECURE`, `SAMESITE`) aren't explicitly configured.
- CSV import checks file extension only, not content — no guard against
  formula injection (e.g. `=HYPERLINK(...)`) or a file size cap.
- Password policy is length-only (`>= 6` chars) — no complexity or
  known-breach checking.
- No audit trail for grade changes, deletions, or enrollments (who/when).
- Backups are encrypted but not yet on a documented restore-test schedule,
  and currently live on the same disk as the live database.

This list is intentionally public — part of the point of this project is
practicing security auditing in the open rather than presenting a polished
facade.

## License

Apache License 2.0 — see `LICENSE`.

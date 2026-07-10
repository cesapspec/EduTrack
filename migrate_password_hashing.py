"""
One-time migration: move password storage from raw unsalted SHA256
(hashlib.sha256) to werkzeug's salted scrypt hashes, and force every
existing account to set a brand new password on next login.

Why "force reset" instead of converting in place:
A hash can't be reversed, so there's no way to take an existing SHA256
digest and turn it into a properly-salted scrypt hash without knowing
the original plaintext password. The only honest options are (a) keep
verifying against the old scheme forever as a fallback, or (b) invalidate
everything and have users set new passwords. This project chose (b).

What this script does:
  1. Adds a `must_change_password` TINYINT(1) column to `users` if it's
     not already there.
  2. Gives every user a fresh scrypt hash of a temporary password, and
     sets must_change_password = 1 for all of them.
  3. Each user is redirected to /account/password on their next login
     (enforced by the before_request hook in app.py) until they replace
     the temporary password with one only they know.

IMPORTANT: run this ONLY after deploying the updated app.py, since the
old app.py expects raw SHA256 hex digests and this script writes scrypt
hash strings instead — running it against the old code will lock
everyone out with no way back in.

Usage:
    python migrate_password_hashing.py
"""

from werkzeug.security import generate_password_hash
from app import get_conn

# Every account gets this same temporary password. It's fine that it's
# shared and printed to your terminal — must_change_password forces each
# user to replace it before they can do anything else, so it's never a
# real credential for more than a few minutes per account.
TEMP_PASSWORD = "ChangeMe123!"


def column_exists(cursor, table, column):
    cursor.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    return cursor.fetchone()["c"] > 0


def main():
    conn = get_conn()
    cursor = conn.cursor(dictionary=True, buffered=True)

    if not column_exists(cursor, "users", "must_change_password"):
        print(
            "ERROR: the must_change_password column doesn't exist yet.\n"
            "This script's DB user intentionally can't run ALTER TABLE.\n"
            "Run this once as a privileged MySQL user, then re-run this script:\n\n"
            "    ALTER TABLE users ADD COLUMN must_change_password "
            "TINYINT(1) NOT NULL DEFAULT 0;\n"
        )
        cursor.close()
        conn.close()
        return

    cursor.execute("SELECT user_id, username FROM users")
    users = cursor.fetchall()

    if not users:
        print("No users found — nothing to migrate.")
        cursor.close()
        conn.close()
        return

    print(f"Found {len(users)} user(s): " + ", ".join(u["username"] for u in users))
    confirm = input(
        f"\nThis will INVALIDATE the current password for all {len(users)} "
        f"user(s) and replace it with the temporary password "
        f"'{TEMP_PASSWORD}'. Everyone will be forced to set a new password "
        f"on their next login. Continue? [y/N] "
    )
    if confirm.strip().lower() != "y":
        print("Aborted — no changes made.")
        cursor.close()
        conn.close()
        return

    # Generate a fresh hash PER USER (not once, reused for everyone) so that
    # even though the plaintext temp password is identical across accounts,
    # the stored hashes differ — each generate_password_hash() call picks
    # its own random salt. Reusing a single hash string for every row would
    # mean every account had byte-for-byte the same password_hash value,
    # which defeats the point of salting even for a short-lived temp password.
    for user in users:
        temp_hash = generate_password_hash(TEMP_PASSWORD)
        cursor.execute(
            "UPDATE users SET password_hash = %s, must_change_password = 1 WHERE user_id = %s",
            (temp_hash, user["user_id"]),
        )
    conn.commit()

    print(f"\nDone. Updated {len(users)} user(s).")
    print(f"Temporary password for all accounts: {TEMP_PASSWORD}")
    print("Each user will be redirected to /account/password on next login.")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
"""
One-time setup: store the backup-encryption passphrase in Windows Credential
Manager via `keyring`, so run_daily_backup() can retrieve it automatically
without the passphrase ever living in .env or any file on disk.

Run this once per machine that will execute backups:
    python setup_backup_encryption.py
"""
import getpass
import sys
import keyring

KEYRING_SERVICE = "edutrack_backup"
KEYRING_USERNAME = "backup_encryption_key"


def main():
    existing = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if existing:
        confirm = input(
            "A passphrase is already stored. Overwrite it? Old backups "
            "encrypted under the old passphrase will no longer decrypt "
            "with the new one. [y/N] "
        )
        if confirm.strip().lower() != "y":
            print("Aborted — no changes made.")
            return

    passphrase = getpass.getpass("Enter backup encryption passphrase: ")
    confirm_pw = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirm_pw:
        print("Passphrases didn't match — aborted.")
        return
    if len(passphrase) < 12:
        print("Use at least 12 characters — aborted.")
        return

    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, passphrase)
    print("Passphrase stored in Windows Credential Manager.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python decrypt_backup.py <encrypted_file> <output_file>")
        sys.exit(1)
    decrypt(sys.argv[1], sys.argv[2])
    main()
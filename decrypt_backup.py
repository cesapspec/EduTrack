"""
Decrypt an EduTrack backup produced by run_daily_backup().
Usage: python decrypt_backup.py backups/school_db_backup_2026-07-10.sql.enc restored.sql
"""
import sys
import keyring
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hmac as crypto_hmac, hashes
from cryptography.exceptions import InvalidSignature
from app import _derive_keys, KEYRING_SERVICE, KEYRING_USERNAME


def decrypt(enc_path, out_path):
    passphrase = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not passphrase:
        print("ERROR: no backup passphrase found for this machine/user.")
        return
    passphrase = passphrase.encode("utf-8")

    with open(enc_path, "rb") as f:
        salt = f.read(16)
        nonce = f.read(16)
        body = f.read()
    ciphertext, tag = body[:-32], body[-32:]

    aes_key, hmac_key = _derive_keys(passphrase, salt)

    mac = crypto_hmac.HMAC(hmac_key, hashes.SHA256())
    mac.update(ciphertext)
    try:
        mac.verify(tag)
    except InvalidSignature:
        print("INTEGRITY CHECK FAILED — wrong passphrase, or file is "
              "corrupted/tampered. Refusing to write output.")
        return

    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(nonce)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    with open(out_path, "wb") as f:
        f.write(plaintext)
    print(f"Decrypted to {out_path} — integrity check passed.")


if __name__ == "__main__":
    decrypt(sys.argv[1], sys.argv[2])
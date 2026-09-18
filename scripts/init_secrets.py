"""One-time Docker initialization. No secret values are printed."""
import base64
import os
from pathlib import Path
import secrets

from app.auth import hash_password


def write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def main():
    folder = Path("/run/agente")
    folder.mkdir(parents=True, exist_ok=True)
    values = {"admin_token": secrets.token_urlsafe(48), "encryption_key": base64.urlsafe_b64encode(os.urandom(32)).decode()}
    for name, value in values.items():
        path = folder / name
        if not path.exists():
            write_private(path, value)
        path.chmod(0o600)
        os.chown(path, 10001, 10001)
    password_hash = folder / "superuser_password_hash"
    initial_password = folder / "superuser_password.initial"
    if not password_hash.exists():
        if initial_password.exists():
            password = initial_password.read_text(encoding="utf-8").strip()
        else:
            password = secrets.token_urlsafe(24)
            write_private(initial_password, password)
        write_private(password_hash, hash_password(password))
    password_hash.chmod(0o600)
    os.chown(password_hash, 10001, 10001)
    if initial_password.exists():
        # Only a host operator using root may retrieve this bootstrap credential.
        initial_password.chmod(0o600)
        os.chown(initial_password, 0, 0)
    storage = Path("/app/storage")
    storage.mkdir(parents=True, exist_ok=True)
    os.chown(storage, 10001, 10001)
    print("Secretos y almacenamiento inicializados; se conservaron los valores existentes.")


if __name__ == "__main__":
    main()

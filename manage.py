import argparse
import getpass
import os
import secrets

from werkzeug.security import generate_password_hash

from panel.store import initialize, save_password, setting


def main():
    parser = argparse.ArgumentParser(description="Manage V2Ray Desk")
    parser.add_argument("command", choices=["init", "reset-password", "serve"])
    parser.add_argument("--data", default=os.environ.get("PANEL_DATA", "./data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--generate", action="store_true", help="Generate a strong password")
    parser.add_argument("--show-password", action="store_true", help="Display the new password once")
    args = parser.parse_args()
    if args.command == "serve":
        from waitress import serve
        from panel.app import create_app
        serve(create_app(args.data), host=args.host, port=args.port, threads=12,
              max_request_body_size=4 * 1024 * 1024, channel_timeout=60)
        return
    path = initialize(args.data)
    if args.command == "init" and setting(path, "password_hash"):
        print("Admin already initialized. Use reset-password to change the password.")
        return
    password = secrets.token_urlsafe(18) if args.generate else getpass.getpass("New admin password (12-128 characters): ")
    confirmation = password if args.generate else getpass.getpass("Confirm password: ")
    if password != confirmation or not 12 <= len(password) <= 128:
        raise SystemExit("Passwords must match and contain 12-128 characters.")
    save_password(path, generate_password_hash(password, method="scrypt"))
    print("Password saved. Existing sessions have been invalidated.")
    if args.generate or args.show_password:
        print("Panel password: " + password)
        print("Keep this password private. It is not stored as plaintext.")


if __name__ == "__main__":
    main()

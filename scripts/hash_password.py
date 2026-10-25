"""Print a bcrypt hash for RECRUITER_PASSWORD_HASH.

Usage: python -m scripts.hash_password   (prompts for the password)
"""

import getpass
import sys

import bcrypt


def main() -> None:
    pw = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("Recruiter password: ")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    print(bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode())


if __name__ == "__main__":
    main()

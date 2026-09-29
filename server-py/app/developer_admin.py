"""Local administrator command for the shared-code developer trial.

Run from the application root: PYTHONPATH=server-py python -m app.developer_admin status
Legacy one-time invitations are no longer issued or accepted.
"""
import argparse

from .developer_access import AccessError, _shared_invitation_code


def main():
    parser = argparse.ArgumentParser(description="CliperX developer trial administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Check whether shared registration is configured")
    args = parser.parse_args()
    if args.command == "status":
        try:
            _shared_invitation_code()
        except AccessError:
            print("shared-registration-disabled")
        else:
            print("shared-registration-configured")


if __name__ == "__main__":
    main()

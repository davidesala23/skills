#!/usr/bin/env python3
"""Read starred Gmail messages through IMAP without changing mailbox flags.

Configuration comes from $SKILLS_CONFIG_DIR/ingest/config.json. Credentials stay
in the configured macOS Keychain entry. The list command returns metadata only;
get returns a message body, not attachment contents. Use Message-ID for durable
deduplication: IMAP UIDs belong to a mailbox and can change.
"""

import argparse
import email
import email.policy
import html
import imaplib
import json
import re
import ssl
import subprocess
import sys

from ingest_config import load_config


class ReaderError(ValueError):
    pass


def keychain_credentials(service):
    probe = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True, text=True,
    )
    if probe.returncode:
        raise ReaderError("The configured Keychain entry is unavailable")
    account = re.search(r'"acct"<blob>="([^"]+)"', probe.stdout)
    if not account:
        raise ReaderError("The configured Keychain entry has no account attribute")
    password = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if password.returncode:
        raise ReaderError("The configured Keychain password is unavailable")
    value = password.stdout.strip().replace(" ", "")
    if not value:
        raise ReaderError("The configured Keychain password is empty")
    return account.group(1), value


def starred_folder(connection):
    status, listing = connection.list()
    if status != "OK":
        raise ReaderError("IMAP mailbox discovery failed")
    for raw in listing or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Flagged" in line:
            match = re.search(r'"([^"]+)"\s*$', line)
            if match:
                return match.group(1)
    return "[Gmail]/Starred"


def select_and_search(connection, folder):
    status, _ = connection.select('"' + folder + '"', readonly=True)
    if status != "OK":
        raise ReaderError("The starred mailbox could not be opened read-only")
    status, data = connection.uid("search", None, "ALL")
    if status != "OK" or not data or data[0] is None:
        raise ReaderError("IMAP UID SEARCH failed")
    return data[0].split()


def fetch_message(connection, uid, section):
    status, data = connection.uid("fetch", uid, section)
    if status != "OK" or not data:
        raise ReaderError("IMAP message fetch failed; coverage is incomplete")
    payloads = [part[1] for part in data if isinstance(part, tuple)
                and len(part) > 1 and isinstance(part[1], bytes)]
    if len(payloads) != 1:
        raise ReaderError("IMAP returned an incomplete or ambiguous message")
    return email.message_from_bytes(payloads[0], policy=email.policy.default)


def headers(message, uid):
    return {
        "uid": uid.decode() if isinstance(uid, bytes) else uid,
        "date": str(message.get("Date", "")),
        "from": str(message.get("From", "")),
        "subject": str(message.get("Subject", "")),
        "message_id": str(message.get("Message-ID", "")),
    }


def body_text(message):
    part = message.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = message.get_body(preferencelist=("html",))
    if part is None:
        return None
    text = part.get_content()
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 <\1>", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_list(connection, folder, limit=None):
    uids = select_and_search(connection, folder)
    selected = uids if limit is None else uids[-limit:]
    return [headers(fetch_message(
        connection, uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
    ), uid) for uid in reversed(selected)]


def read_one(connection, folder, uid):
    select_and_search(connection, folder)
    message = fetch_message(connection, uid.encode(), "(BODY.PEEK[])")
    result = headers(message, uid)
    body = body_text(message)
    result.update({"to": str(message.get("To", "")), "body": body,
                   "body_available": body is not None,
                   "scope": "message body; attachments are not fetched"})
    return result


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    commands = command.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="starred message metadata, newest first")
    listing.add_argument("--limit", type=positive_integer,
                         help="explicit metadata cap; omit to list every starred message")
    listing.add_argument("--json", action="store_true")
    get = commands.add_parser("get", help="read a starred message body by IMAP UID")
    get.add_argument("uid")
    get.add_argument("--json", action="store_true")
    commands.add_parser("doctor", help="check configured Keychain and read-only mailbox access")
    return command


def execute(args, config):
    service = config.get("gmail_keychain_service")
    host = config.get("gmail_imap_host")
    if not all(isinstance(value, str) and value.strip() for value in (service, host)):
        raise ReaderError("Configure gmail_keychain_service and gmail_imap_host")
    account, password = keychain_credentials(service)
    connection = imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context())
    try:
        try:
            connection.login(account, password)
        except imaplib.IMAP4.error:
            raise ReaderError("IMAP login failed; check the configured credentials") from None
        folder = starred_folder(connection)
        if args.command == "doctor":
            return {"ok": True, "read_only": True,
                    "starred_count": len(select_and_search(connection, folder))}
        if args.command == "list":
            return read_list(connection, folder, args.limit)
        return read_one(connection, folder, args.uid)
    finally:
        try:
            connection.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args, load_config())
    except (OSError, ValueError, imaplib.IMAP4.error) as error:
        detail = str(error) if isinstance(error, ValueError) else "IMAP or Keychain operation failed"
        print(json.dumps({"error": "gmail_reader_error", "detail": detail}), file=sys.stderr)
        return 2
    if getattr(args, "json", False) or args.command == "doctor":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif isinstance(result, list):
        for item in result:
            print(f"[{item['uid']}] {item['date']}\n  {item['from']}\n  {item['subject']}\n  {item['message_id']}\n")
    else:
        for key in ("date", "from", "to", "subject", "message_id"):
            print(f"{key.capitalize()}: {result[key]}")
        print("\n" + (result["body"] if result["body_available"] else "Body unavailable"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

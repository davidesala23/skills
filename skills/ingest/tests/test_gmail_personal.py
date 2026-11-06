import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("gmail_reader", SCRIPTS / "gmail-personal.py")
READER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READER)

MESSAGE = b"From: sender@example.test\r\nTo: reader@example.test\r\nSubject: A request\r\nMessage-ID: <stable-id@example.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlease review the draft.\r\n"


class FakeMailbox:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.failed_uid = None

    def login(self, account, password):
        self.calls.append(("login", account, password))

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Flagged) "/" "[Gmail]/Localized"']

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"2"]

    def uid(self, command, uid, section):
        self.calls.append((command, uid, section))
        if command == "search":
            return "OK", [b"1 2"]
        if uid == self.failed_uid:
            return "NO", [None]
        return "OK", [(b"BODY", MESSAGE), b")"]

    def logout(self):
        self.closed = True


class GmailReaderTest(unittest.TestCase):
    def test_all_metadata_uses_readonly_and_peek_in_newest_order(self):
        mailbox = FakeMailbox()
        rows = READER.read_list(mailbox, READER.starred_folder(mailbox))
        self.assertEqual([row["uid"] for row in rows], ["2", "1"])
        self.assertEqual(rows[0]["message_id"], "<stable-id@example.test>")
        self.assertIn(("select", '"[Gmail]/Localized"', True), mailbox.calls)
        fetches = [call for call in mailbox.calls if call[0] == "fetch"]
        self.assertTrue(all("BODY.PEEK" in call[2] for call in fetches))
        self.assertEqual(set(call[0] for call in mailbox.calls), {"select", "search", "fetch"})

    def test_get_reads_body_without_marking_seen(self):
        mailbox = FakeMailbox()
        row = READER.read_one(mailbox, "Starred", "1")
        self.assertEqual(row["body"].strip(), "Please review the draft.")
        self.assertTrue(row["body_available"])
        self.assertIn(("fetch", b"1", "(BODY.PEEK[])"), mailbox.calls)
        self.assertIn(("select", '"Starred"', True), mailbox.calls)

    def test_failed_fetch_cannot_be_reported_as_complete_metadata(self):
        mailbox = FakeMailbox()
        mailbox.failed_uid = b"1"
        with self.assertRaisesRegex(READER.ReaderError, "coverage is incomplete"):
            READER.read_list(mailbox, "Starred")

    def test_missing_configuration_stops_before_credentials_or_network(self):
        error = io.StringIO()
        with patch.object(READER, "load_config", side_effect=ValueError("configuration missing")), \
                patch.object(READER, "keychain_credentials") as credentials, \
                patch.object(READER.imaplib, "IMAP4_SSL") as network, \
                contextlib.redirect_stderr(error):
            self.assertEqual(READER.main(["list", "--json"]), 2)
        credentials.assert_not_called()
        network.assert_not_called()
        self.assertEqual(json.loads(error.getvalue())["error"], "gmail_reader_error")

    def test_login_failure_does_not_echo_credentials_or_server_error(self):
        mailbox = FakeMailbox()
        secret = "synthetic-app-secret"
        mailbox.login = unittest.mock.Mock(side_effect=READER.imaplib.IMAP4.error(secret))
        config = {"gmail_keychain_service": "fixture-entry", "gmail_imap_host": "imap.example.test"}
        error = io.StringIO()
        with patch.object(READER, "load_config", return_value=config), \
                patch.object(READER, "keychain_credentials", return_value=("fixture-account", secret)), \
                patch.object(READER.imaplib, "IMAP4_SSL", return_value=mailbox), \
                contextlib.redirect_stderr(error):
            self.assertEqual(READER.main(["doctor"]), 2)
        self.assertNotIn(secret, error.getvalue())
        self.assertNotIn("fixture-account", error.getvalue())
        self.assertTrue(mailbox.closed)

    def test_keychain_failure_does_not_print_subprocess_output(self):
        secret = "synthetic-keychain-output"
        failure = subprocess.CompletedProcess([], 1, stdout=secret, stderr=secret)
        with patch.object(READER.subprocess, "run", return_value=failure):
            with self.assertRaises(READER.ReaderError) as error:
                READER.keychain_credentials("fixture-entry")
        self.assertNotIn(secret, str(error.exception))

    def test_doctor_does_not_print_account_or_password(self):
        mailbox = FakeMailbox()
        config = {"gmail_keychain_service": "fixture-entry", "gmail_imap_host": "imap.example.test"}
        output = io.StringIO()
        with patch.object(READER, "load_config", return_value=config), \
                patch.object(READER, "keychain_credentials", return_value=("fixture-account", "synthetic-secret")), \
                patch.object(READER.imaplib, "IMAP4_SSL", return_value=mailbox), \
                contextlib.redirect_stdout(output):
            self.assertEqual(READER.main(["doctor"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "read_only": True, "starred_count": 2})
        self.assertNotIn("fixture-account", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertTrue(mailbox.closed)


if __name__ == "__main__":
    unittest.main()

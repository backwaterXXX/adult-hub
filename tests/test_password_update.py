"""Focused regression and integration tests for secure operator password update and session revocation.

Covers:
1. Concurrency race protection (Item 1):
   - Ensures a login that verified the previous hash cannot create a usable session
     after password update/revocation commits.
   - Verifies lock-order consistency across user authentication and password update.
2. Pairing session lifecycle (Item 2):
   - Safe handling of pending unassigned pairing rows (expires pending unassigned
     pairings on admin password update without changing unrelated approved pairings).
3. Transaction rollback and row-lock release (Item 3):
   - Row locks are released on every failure path (unknown user, inactive user, DB error).
   - Rollback is called, commit is not called, error is propagated.
4. Username uniqueness schema verification (Item 4):
   - Confirms case-insensitive unique username constraint via idx_client_users_username
     on lower(username).
5. Revoked refresh token handling (Item 5):
   - Refreshes for sessions revoked by password change raise "Session is revoked",
     not false-positive token replay errors. Unnecessary spent-token insertions removed.
6. Local CLI behavior and password redaction (Item 6):
   - Output (stdout/stderr) never includes supplied passwords on success or error.
   - Enforces supported syntax (update-password --username USERNAME); no redundant aliases.
7. Password length policy:
   - MIN_PASSWORD_LEN = 8, MAX_PASSWORD_LEN = 128, and live test password 'konrad123' accepted.
8. Live PostgreSQL integration (via PgTestCase when TEST_DATABASE_URL is available).
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.client_auth.crypto import hash_password, verify_password
from common.client_auth.service import (
    BoundedValidationError,
    MIN_PASSWORD_LEN,
    MAX_PASSWORD_LEN,
    authenticate_by_password,
    bootstrap_owner,
    refresh_session_token,
    update_user_password,
    verify_access_token,
    AuthenticationFailed,
)
from common.client_auth_cli import main as cli_main, run_update_password
from tests.pg_helper import PgTestCase


class TestPasswordLengthPolicy(unittest.TestCase):
    """Confirm password policy bounds remain strictly enforced."""

    def test_minimum_password_length_remains_8(self):
        """Verify MIN_PASSWORD_LEN is exactly 8 and MAX_PASSWORD_LEN is 128."""
        self.assertEqual(MIN_PASSWORD_LEN, 8)
        self.assertEqual(MAX_PASSWORD_LEN, 128)

    def test_passwords_shorter_than_8_chars_are_rejected(self):
        """Passwords with length < 8 raise BoundedValidationError."""
        short_passwords = ["", "1", "1234567", "a" * 7]
        mock_conn = MagicMock()
        for pwd in short_passwords:
            with self.subTest(pwd=pwd):
                with self.assertRaises(BoundedValidationError) as ctx:
                    update_user_password(mock_conn, "testuser", pwd)
                self.assertIn("Password length must be between 8 and 128", str(ctx.exception))

    def test_8_character_password_is_accepted(self):
        """An 8-character password meets the minimum requirement and is accepted."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = ("usr_123", "testuser", True, False)
        result = update_user_password(mock_conn, "testuser", "12345678")
        self.assertEqual(result["username"], "testuser")

    def test_konrad123_test_password_is_accepted(self):
        """The test account password 'konrad123' (9 characters) is strictly accepted."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = ("usr_123", "konrad", True, False)
        result = update_user_password(mock_conn, "konrad", "konrad123")
        self.assertEqual(result["username"], "konrad")
        self.assertEqual(result["id"], "usr_123")

    def test_passwords_exceeding_max_len_are_rejected(self):
        """Passwords exceeding 128 characters raise BoundedValidationError."""
        mock_conn = MagicMock()
        long_pwd = "a" * 129
        with self.assertRaises(BoundedValidationError) as ctx:
            update_user_password(mock_conn, "testuser", long_pwd)
        self.assertIn("Password length must be between 8 and 128", str(ctx.exception))

    def test_empty_or_whitespace_username_is_rejected(self):
        """Empty or whitespace username raises BoundedValidationError."""
        mock_conn = MagicMock()
        for bad_username in ["", "   ", None]:
            with self.subTest(username=bad_username):
                with self.assertRaises(BoundedValidationError):
                    update_user_password(mock_conn, bad_username, "konrad123")


class TestUpdateUserPasswordService(unittest.TestCase):
    """Unit tests for update_user_password service logic (Items 2, 3, 5)."""

    def test_missing_user_is_rejected_with_rollback(self):
        """Attempting to update a nonexistent user raises ValueError, calls rollback, and not commit."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        with self.assertRaises(ValueError) as ctx:
            update_user_password(mock_conn, "ghost_user", "konrad123")

        self.assertIn("does not exist", str(ctx.exception))
        # Ensure lookup was run with FOR UPDATE
        self.assertEqual(mock_conn.execute.call_count, 1)
        query = mock_conn.execute.call_args_list[0][0][0]
        self.assertIn("SELECT id, username, is_active, is_admin FROM client_users", query)
        self.assertIn("FOR UPDATE", query)
        # Rollback called to release transaction/locks, commit never called
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    def test_inactive_user_is_rejected_with_rollback(self):
        """Attempting to update an inactive user raises ValueError, calls rollback, and releases row lock."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = ("usr_inactive", "disabled_user", False, False)

        with self.assertRaises(ValueError) as ctx:
            update_user_password(mock_conn, "disabled_user", "konrad123")

        self.assertIn("is inactive", str(ctx.exception))
        self.assertEqual(mock_conn.execute.call_count, 1)
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    def test_database_error_triggers_rollback_and_propagates(self):
        """Any database error during update executes rollback, does not commit, and propagates."""
        mock_conn = MagicMock()
        # Lookup succeeds, but subsequent UPDATE fails
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=("usr_k1", "konrad", True, False))),
            RuntimeError("PostgreSQL connection lost"),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            update_user_password(mock_conn, "konrad", "konrad123")

        self.assertIn("connection lost", str(ctx.exception))
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    def test_successful_password_update_and_revocation_queries(self):
        """Successful password update hashes password, revokes sessions, and avoids spent-token bloat."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = ("usr_k1", "konrad", True, False)

        res = update_user_password(mock_conn, "konrad", "konrad123")
        self.assertEqual(res, {"id": "usr_k1", "username": "konrad"})

        calls = mock_conn.execute.call_args_list
        # Queries executed for non-admin user:
        # 1. SELECT user with FOR UPDATE
        # 2. UPDATE client_users SET password_hash = ...
        # 3. UPDATE client_sessions SET revoked_at = ...
        # 4. UPDATE client_pairing_sessions SET status = 'revoked' WHERE user_id = ...
        self.assertEqual(len(calls), 4)

        # Query 1: lookup with FOR UPDATE
        self.assertIn("SELECT id, username, is_active, is_admin FROM client_users", calls[0][0][0])
        self.assertIn("FOR UPDATE", calls[0][0][0])

        # Query 2: user password update
        update_user_sql, update_user_params = calls[1][0]
        self.assertIn("UPDATE client_users SET password_hash = %s", update_user_sql)
        updated_hash = update_user_params[0]
        self.assertTrue(verify_password("konrad123", updated_hash))
        self.assertFalse(verify_password("wrongpassword", updated_hash))
        self.assertEqual(update_user_params[1], "usr_k1")

        # Query 3: session revocation (revoked_at set)
        session_sql, session_params = calls[2][0]
        self.assertIn("UPDATE client_sessions SET revoked_at = CURRENT_TIMESTAMP", session_sql)
        self.assertIn("WHERE user_id = %s AND revoked_at IS NULL", session_sql)
        self.assertEqual(session_params, ("usr_k1",))

        # Query 4: pairing session revocation for this user
        pairing_sql, pairing_params = calls[3][0]
        self.assertIn("UPDATE client_pairing_sessions SET status = 'revoked'", pairing_sql)
        self.assertIn("WHERE user_id = %s", pairing_sql)
        self.assertEqual(pairing_params, ("usr_k1",))

        # Ensure NO insertions into client_session_spent_tokens occurred (Item 5)
        for c in calls:
            self.assertNotIn("client_session_spent_tokens", c[0][0])

        # Commit called
        mock_conn.commit.assert_called_once()

    def test_admin_password_update_expires_unassigned_pending_pairings(self):
        """Admin password update expires pending unassigned pairing rows (Item 2)."""
        mock_conn = MagicMock()
        # is_admin = True
        mock_conn.execute.return_value.fetchone.return_value = ("usr_admin", "owner", True, True)

        res = update_user_password(mock_conn, "owner", "konrad123")
        self.assertEqual(res, {"id": "usr_admin", "username": "owner"})

        calls = mock_conn.execute.call_args_list
        self.assertEqual(len(calls), 5)

        # Query 4: revokes user's own pairings
        self.assertIn("WHERE user_id = %s AND status IN ('pending', 'approved')", calls[3][0][0])
        self.assertEqual(calls[3][0][1], ("usr_admin",))

        # Query 5: expires pending unassigned pairings without touching unrelated approved pairings
        expire_sql = calls[4][0][0]
        self.assertIn("UPDATE client_pairing_sessions SET status = 'expired'", expire_sql)
        self.assertIn("WHERE user_id IS NULL AND status = 'pending'", expire_sql)

        mock_conn.commit.assert_called_once()


class TestLoginPasswordUpdateConcurrency(unittest.TestCase):
    """Unit tests for concurrency race protection and lock order (Item 1)."""

    @patch("common.client_auth.service.check_and_increment_rate_limit", return_value=True)
    def test_login_recheck_prevents_stale_session_if_password_changed_during_verify(self, mock_rate):
        """Login that verified previous hash is rejected if password changed before session commit."""
        mock_conn = MagicMock()
        old_hash = hash_password("OldPassword123")
        new_hash = hash_password("konrad123")

        # 1. First SELECT returns the old hash
        # 2. Second SELECT (under FOR SHARE lock) returns the new hash (updated concurrently)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=("usr_1", "konrad", old_hash, True))),
            MagicMock(fetchone=MagicMock(return_value=(new_hash, True))),
        ]

        with self.assertRaises(AuthenticationFailed) as ctx:
            authenticate_by_password(
                mock_conn,
                username="konrad",
                password="OldPassword123",
                device_name="TV",
                device_id="tv-1",
                client_ip="192.168.1.10",
            )

        self.assertIn("Invalid username or password", str(ctx.exception))
        # Ensure rollback was called
        mock_conn.rollback.assert_called()
        # Ensure session was NOT inserted and commit was not called
        insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT INTO client_sessions" in c[0][0]]
        self.assertEqual(len(insert_calls), 0)
        mock_conn.commit.assert_not_called()

    @patch("common.client_auth.service.check_and_increment_rate_limit", return_value=True)
    def test_login_recheck_prevents_stale_session_if_user_deactivated_during_verify(self, mock_rate):
        """Login that verified hash is rejected if account deactivated before session commit."""
        mock_conn = MagicMock()
        pwd_hash = hash_password("ValidPassword123")

        # First SELECT returns active user, FOR SHARE returns inactive user
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=("usr_1", "konrad", pwd_hash, True))),
            MagicMock(fetchone=MagicMock(return_value=(pwd_hash, False))),
        ]

        with self.assertRaises(AuthenticationFailed):
            authenticate_by_password(
                mock_conn,
                username="konrad",
                password="ValidPassword123",
                device_name="TV",
                device_id="tv-1",
                client_ip="192.168.1.10",
            )

        mock_conn.rollback.assert_called()
        insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT INTO client_sessions" in c[0][0]]
        self.assertEqual(len(insert_calls), 0)
        mock_conn.commit.assert_not_called()

    def test_lock_order_consistency(self):
        """Verify lock acquisition order: client_users is locked before client_sessions."""
        # update_user_password locks client_users FOR UPDATE first
        mock_conn1 = MagicMock()
        mock_conn1.execute.return_value.fetchone.return_value = ("usr_1", "konrad", True, False)
        update_user_password(mock_conn1, "konrad", "konrad123")

        update_calls = [c[0][0] for c in mock_conn1.execute.call_args_list]
        self.assertIn("FOR UPDATE", update_calls[0])
        self.assertIn("client_users", update_calls[0])
        # client_sessions is updated AFTER client_users is locked
        self.assertIn("client_sessions", update_calls[2])


class TestRevokedRefreshTokenBehavior(unittest.TestCase):
    """Verify refresh token behavior on revoked sessions (Item 5)."""

    def test_refresh_on_revoked_session_raises_session_revoked_not_token_replayed(self):
        """A refresh token for a revoked session raises 'Session is revoked', not token replay."""
        mock_conn = MagicMock()
        # _find_replayed_session_row returns None (not in spent tokens)
        # _lock_refresh_session_row returns session with revoked_at set
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=None)),  # _fetch_spent_token_row
            MagicMock(fetchone=MagicMock(return_value=None)),  # prev_cursor
            MagicMock(
                fetchone=MagicMock(
                    return_value=(
                        "sess_123",
                        "usr_1",
                        "dev_1",
                        "Living Room",
                        False,  # is_expired
                        "2026-11-24 12:00:00",  # revoked_at IS NOT NULL
                        "current_hash",
                        None,
                        "konrad",
                        True,
                    )
                )
            ),
        ]

        with patch("common.client_auth.service._safe_hash_token", return_value="current_hash"):
            with self.assertRaises(AuthenticationFailed) as ctx:
                refresh_session_token(mock_conn, "ah_ref_validtoken123")

            self.assertEqual(str(ctx.exception), "Session is revoked")
            self.assertNotIn("replayed", str(ctx.exception).lower())


class TestCliOutputRedactionAndSyntax(unittest.TestCase):
    """Verify CLI never leaks passwords to stdout/stderr and enforces syntax (Item 6)."""

    @patch("common.client_auth_cli.db.connection")
    @patch("common.client_auth_cli.ensure_auth_schema")
    def test_cli_output_never_includes_password_on_success(self, mock_schema, mock_db_conn):
        """CLI stdout and stderr never contain the plain-text password upon success."""
        mock_conn = MagicMock()
        mock_db_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = ("usr_konrad", "konrad", True, True)

        secret_password = "konrad123"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdin", io.StringIO(f"{secret_password}\n")):
            with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                exit_code = run_update_password("konrad")

        self.assertEqual(exit_code, 0)
        self.assertNotIn(secret_password, stdout_buf.getvalue())
        self.assertNotIn(secret_password, stderr_buf.getvalue())
        self.assertIn("Successfully updated password for account 'konrad'", stdout_buf.getvalue())

    @patch("sys.stdin.isatty", return_value=True)
    def test_cli_output_never_includes_password_on_interactive_mismatch(self, mock_isatty):
        """CLI stderr never contains either entered password when interactive confirmation fails."""
        pwd1 = "konrad123"
        pwd2 = "different123"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("getpass.getpass", side_effect=[pwd1, pwd2]):
            with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                exit_code = run_update_password("konrad")

        self.assertEqual(exit_code, 1)
        self.assertNotIn(pwd1, stdout_buf.getvalue())
        self.assertNotIn(pwd1, stderr_buf.getvalue())
        self.assertNotIn(pwd2, stdout_buf.getvalue())
        self.assertNotIn(pwd2, stderr_buf.getvalue())
        self.assertIn("Passwords do not match", stderr_buf.getvalue())

    @patch("common.client_auth_cli.db.connection")
    @patch("common.client_auth_cli.ensure_auth_schema")
    def test_cli_output_never_includes_password_on_short_password_error(self, mock_schema, mock_db_conn):
        """CLI stderr never contains the rejected short password upon bounds failure."""
        mock_conn = MagicMock()
        mock_db_conn.return_value.__enter__.return_value = mock_conn
        short_pwd = "short"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdin", io.StringIO(f"{short_pwd}\n")):
            with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                exit_code = run_update_password("konrad")

        self.assertEqual(exit_code, 1)
        self.assertNotIn(short_pwd, stdout_buf.getvalue())
        self.assertNotIn(short_pwd, stderr_buf.getvalue())
        self.assertIn("Password length must be between 8 and 128", stderr_buf.getvalue())

    @patch("common.client_auth_cli.db.connection")
    @patch("common.client_auth_cli.ensure_auth_schema")
    def test_cli_output_never_includes_password_on_user_not_found(self, mock_schema, mock_db_conn):
        """CLI stderr never contains the password when target user does not exist."""
        mock_conn = MagicMock()
        mock_db_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = None

        secret_password = "konrad123"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        with patch("sys.stdin", io.StringIO(f"{secret_password}\n")):
            with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                exit_code = run_update_password("nonexistent_user")

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret_password, stdout_buf.getvalue())
        self.assertNotIn(secret_password, stderr_buf.getvalue())
        self.assertIn("does not exist", stderr_buf.getvalue())

    def test_cli_syntax_enforces_required_username_flag(self):
        """CLI requires --username flag; missing flag raises SystemExit with code 2."""
        with patch("sys.argv", ["client_auth_cli.py", "update-password"]):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as ctx:
                    cli_main()
                self.assertEqual(ctx.exception.code, 2)

    def test_cli_rejects_removed_alias_set_password(self):
        """CLI rejects redundant 'set-password' alias; only 'update-password' is supported."""
        with patch("sys.argv", ["client_auth_cli.py", "set-password", "--username", "konrad"]):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as ctx:
                    cli_main()
                self.assertEqual(ctx.exception.code, 2)


class TestUsernameUniquenessSchema(unittest.TestCase):
    """Confirm schema enforces unique lower(username) (Item 4)."""

    def test_schema_enforces_case_insensitive_unique_username_index(self):
        """Verify the exact UNIQUE index constraint idx_client_users_username exists on lower(username)."""
        # Resolve from this file so the check also works outside /app/adult-hub
        # (CI checks the repository out under a different absolute path).
        baseline_path = (
            Path(__file__).resolve().parents[1] / "common" / "migrations" / "schema_baseline.sql"
        )
        baseline_sql = baseline_path.read_text(encoding="utf-8")

        # The squashed baseline is the only place the index is defined.
        self.assertIn("idx_client_users_username", baseline_sql)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_client_users_username", baseline_sql)
        self.assertIn("(lower(username))", baseline_sql)


class TestLivePasswordUpdateIntegration(PgTestCase):
    """End-to-end integration tests using disposable PostgreSQL when TEST_DATABASE_URL is set."""

    def test_end_to_end_password_update_and_revocation(self):
        """End-to-end: update password to konrad123, re-auth succeeds, old session fails, pairing expired."""
        # 1. Bootstrap user account with initial password
        initial_pwd = "InitialPassword123!"
        user = bootstrap_owner(self.conn, "konrad", initial_pwd)
        self.assertEqual(user["username"], "konrad")

        # 2. Login to create an active session
        auth_session = authenticate_by_password(
            self.conn,
            username="konrad",
            password=initial_pwd,
            device_name="Living Room TV",
            device_id="tv-lr",
            client_ip="192.168.1.55",
        )
        old_access = auth_session["access_token"]
        old_refresh = auth_session["refresh_token"]

        # Verify session is initially valid
        valid_user = verify_access_token(self.conn, old_access)
        self.assertIsNotNone(valid_user)
        self.assertEqual(valid_user["username"], "konrad")

        # Create a pending unassigned pairing session (user_id IS NULL)
        self.conn.execute(
            "INSERT INTO client_pairing_sessions ("
            "id, pairing_code, normalized_code, secret_hash, device_name, device_id, status, "
            "poll_attempts, expires_at, client_ip"
            ") VALUES ('pair_unassigned_1', 'CODE01', 'CODE01', 'hash_sec_1', 'TV Unassigned', 'tv-u', 'pending', "
            "0, CURRENT_TIMESTAMP + interval '10 minutes', '192.168.1.99')"
        )
        self.conn.commit()

        # 3. Perform password update to konrad123
        updated_user = update_user_password(self.conn, "konrad", "konrad123")
        self.assertEqual(updated_user["username"], "konrad")

        # 4. Old access token MUST now be rejected (revoked_at set)
        invalid_user = verify_access_token(self.conn, old_access)
        self.assertIsNone(invalid_user, "Access token from old session must be rejected after password update")

        # 5. Old refresh token MUST be rejected with 'Session is revoked', not replay
        with self.assertRaises(AuthenticationFailed) as ctx:
            refresh_session_token(self.conn, old_refresh)
        self.assertEqual(str(ctx.exception), "Session is revoked")

        # Verify client_session_spent_tokens has NOT recorded the refresh token (Item 5)
        spent_rows = self.conn.execute(
            "SELECT count(*) FROM client_session_spent_tokens WHERE session_id = %s",
            (auth_session["access_token"],),
        ).fetchone()
        self.assertEqual(spent_rows[0], 0)

        # 6. Verify pending unassigned pairing was expired (Item 2)
        pair_row = self.conn.execute(
            "SELECT status FROM client_pairing_sessions WHERE id = 'pair_unassigned_1'"
        ).fetchone()
        self.assertEqual(pair_row[0], "expired")

        # 7. Login with old password MUST fail
        with self.assertRaises(AuthenticationFailed):
            authenticate_by_password(
                self.conn,
                username="konrad",
                password=initial_pwd,
                device_name="Living Room TV",
                device_id="tv-lr",
                client_ip="192.168.1.55",
            )

        # 8. Login with new password 'konrad123' MUST succeed
        new_auth = authenticate_by_password(
            self.conn,
            username="konrad",
            password="konrad123",
            device_name="Living Room TV",
            device_id="tv-lr",
            client_ip="192.168.1.55",
        )
        self.assertTrue(new_auth["access_token"].startswith("ah_tok_"))
        self.assertEqual(new_auth["user"]["username"], "konrad")

        # Verify new access token works
        new_valid = verify_access_token(self.conn, new_auth["access_token"])
        self.assertIsNotNone(new_valid)
        self.assertEqual(new_valid["username"], "konrad")


if __name__ == "__main__":
    unittest.main()

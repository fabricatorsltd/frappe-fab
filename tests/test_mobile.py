from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab.mobile import MOBILE_OAUTH_CLIENT, check_authorization_code, check_bearer_token

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0)


class Row(dict):
	"""Stands in for the dicts frappe.db.get_value(as_dict=True) returns."""

	__getattr__ = dict.get


def throwing(frappe):
	"""frappe.throw() aborts the hook, so the mock has to raise as well."""

	def throw(message, exc=None):
		raise ValueError(message)

	frappe.throw.side_effect = throw
	frappe.session.data = {}
	return frappe


def code(client=MOBILE_OAUTH_CLIENT, challenge="abc", method="s256"):
	return SimpleNamespace(
		client=client, code_challenge=challenge, code_challenge_method=method, expiration_time=None
	)


@patch("fab.mobile._", new=str)
@patch("fab.mobile.now_datetime", return_value=NOW)
@patch("fab.mobile.frappe")
class TestCheckAuthorizationCode(unittest.TestCase):
	def test_a_code_with_s256_is_accepted_and_expires(self, frappe, _now):
		throwing(frappe)
		doc = code()

		check_authorization_code(doc)

		self.assertEqual(doc.expiration_time, NOW + datetime.timedelta(seconds=120))

	def test_a_code_without_challenge_is_refused(self, frappe, _now):
		throwing(frappe)

		with self.assertRaises(ValueError):
			check_authorization_code(code(challenge=None, method=None))

	def test_a_plain_challenge_is_refused(self, frappe, _now):
		throwing(frappe)

		with self.assertRaises(ValueError):
			check_authorization_code(code(method="plain"))

	def test_an_impersonated_session_is_refused(self, frappe, _now):
		throwing(frappe).session.data = {"impersonated_by": "Administrator"}

		with self.assertRaises(ValueError):
			check_authorization_code(code())

	def test_other_clients_are_left_alone(self, frappe, _now):
		throwing(frappe)
		doc = code(client="other", challenge=None, method=None)

		check_authorization_code(doc)

		self.assertIsNone(doc.expiration_time)


@patch("fab.mobile._", new=str)
@patch("fab.mobile.now_datetime", return_value=NOW)
@patch("fab.mobile.frappe")
class TestCheckBearerToken(unittest.TestCase):
	def token(self, client=MOBILE_OAUTH_CLIENT):
		return SimpleNamespace(client=client)

	def test_the_password_grant_is_refused(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "password"}

		with self.assertRaises(ValueError):
			check_bearer_token(self.token())

	def test_the_password_grant_stays_open_to_other_clients(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "password"}

		check_bearer_token(self.token("other"))

	def test_a_live_code_is_accepted(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "authorization_code", "code": "c"}
		frappe.db.get_value.return_value = NOW + datetime.timedelta(seconds=30)

		check_bearer_token(self.token())

	def test_an_expired_code_is_refused(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "authorization_code", "code": "c"}
		frappe.db.get_value.return_value = NOW - datetime.timedelta(seconds=1)

		with self.assertRaises(ValueError):
			check_bearer_token(self.token())

	def test_a_refresh_token_of_another_client_is_refused(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "refresh_token", "refresh_token": "r"}
		frappe.db.get_value.return_value = Row(name="old", client="other")

		with self.assertRaises(ValueError):
			check_bearer_token(self.token())

	def test_the_mismatch_is_refused_for_any_client(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "refresh_token", "refresh_token": "r"}
		frappe.db.get_value.return_value = Row(name="old", client=MOBILE_OAUTH_CLIENT)

		with self.assertRaises(ValueError):
			check_bearer_token(self.token("other"))

	def test_a_refresh_revokes_the_old_token(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "refresh_token", "refresh_token": "r"}
		frappe.db.get_value.return_value = Row(name="old", client=MOBILE_OAUTH_CLIENT)
		frappe.db._cursor.rowcount = 1

		check_bearer_token(self.token())

		sql, name = frappe.db.sql.call_args.args
		self.assertIn("set status = 'Revoked'", sql)
		self.assertIn("status = 'Active'", sql)
		self.assertEqual(name, "old")

	def test_the_loser_of_a_concurrent_refresh_is_refused(self, frappe, _now):
		throwing(frappe).form_dict = {"grant_type": "refresh_token", "refresh_token": "r"}
		frappe.db.get_value.return_value = Row(name="old", client=MOBILE_OAUTH_CLIENT)
		frappe.db._cursor.rowcount = 0

		with self.assertRaises(ValueError):
			check_bearer_token(self.token())

from __future__ import annotations

import frappe

# Documents whose outgoing emails belong to billing/administration rather than
# support. Frappe has a single site-wide default outgoing account; the helpdesk
# relies on it (support@), so billing mail is routed here per document type.
BILLING_DOCTYPES = frozenset(
	{
		"Sales Invoice",
		"Payment Entry",
		"Payment Request",
		"Dunning",
		"Process Statement Of Accounts",
	}
)


def _billing_account():
	"""The enabled outgoing Email Account configured as the billing sender
	(site config `billing_email`), or None when the site has none: then the
	default outgoing account applies unchanged."""
	email = (frappe.conf.get("billing_email") or "").strip().lower()
	if not email:
		return None
	return frappe.db.get_value(
		"Email Account",
		{"email_id": email, "enable_outgoing": 1},
		["name", "email_id", "email_account_name"],
		as_dict=True,
	)


def route_billing_sender(doc, method=None):
	"""Communication before_insert: outgoing mail on billing documents is sent
	from the billing account instead of the site default.

	Setting `sender` (and `email_account`) before insert is enough: the send that
	follows resolves the outgoing account from the sender address."""
	if doc.get("sent_or_received") != "Sent":
		return
	if doc.get("communication_type") not in (None, "", "Communication"):
		return
	if doc.get("reference_doctype") not in BILLING_DOCTYPES:
		return
	account = _billing_account()
	if not account:
		return
	doc.sender = account.email_id
	doc.sender_full_name = account.email_account_name
	doc.email_account = account.name

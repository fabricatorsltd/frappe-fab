import frappe
from frappe import _


@frappe.whitelist(methods=["POST"])
def impersonate(user: str, reason: str):
	"""Frappe's impersonation, without the notice and the email to the user:
	support tests customer accounts, and the alert worries them. The Activity
	Log still records who impersonated whom, and why."""
	frappe.has_permission("User", "impersonate", throw=True)

	frappe.get_doc(
		{
			"doctype": "Activity Log",
			"user": user,
			"status": "Success",
			"subject": _("User {0} impersonated as {1}").format(frappe.session.user, user),
			"content": reason,
			"operation": "Impersonate",
		}
	).insert(ignore_permissions=True, ignore_links=True)
	frappe.local.login_manager.impersonate(user)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_password(
	new_password: str, logout_all_sessions: int = 0, key: str | None = None, old_password: str | None = None
):
	"""Frappe's password change and reset, then the mobile app signs in again: its
	refresh token would otherwise outlive the password it was issued under."""
	from frappe.core.doctype.user.user import update_password as frappe_update_password

	from fab.mobile import revoke_mobile_tokens

	result = frappe_update_password(new_password, logout_all_sessions, key, old_password)
	if frappe.local.response.get("http_status_code") != 410 and frappe.session.user != "Guest":
		revoke_mobile_tokens(frappe.session.user)
	return result

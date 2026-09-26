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

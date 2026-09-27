import frappe
from frappe.model.document import Document


class MobileDevice(Document):
	pass


def get_permission_query_conditions(user: str | None = None) -> str:
	"""Desk users hold read and delete on the doctype, but only over their own devices."""
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""
	return f"`tabMobile Device`.`user` = {frappe.db.escape(user)}"


def has_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return "System Manager" in frappe.get_roles(user) or doc.user == user

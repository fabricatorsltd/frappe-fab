from __future__ import annotations

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice as _SalesInvoice
from erpnext.selling.doctype.quotation.quotation import Quotation as _Quotation
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder as _SalesOrder
from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote as _DeliveryNote
from frappe import _

# The role allowed to sell below the item's max_discount, and the role holding the
# permlevel 1 write on the two override fields (see fab.install).
MAX_DISCOUNT_OVERRIDE_ROLE = "Sales Manager"


class MaxDiscountOverride:
	"""Let the maximum discount be waived on a single document.

	SellingController.validate() calls validate_max_discount() and throws, so a
	doc_events hook can never run before it: the check has to be replaced on the
	controller class itself.
	"""

	def validate_max_discount(self):
		if not self.get("fab_max_discount_override"):
			super().validate_max_discount()
			return

		if not (self.get("fab_max_discount_override_reason") or "").strip():
			frappe.throw(_("Give a reason for overriding the maximum discount."))

		if MAX_DISCOUNT_OVERRIDE_ROLE not in frappe.get_roles():
			frappe.throw(_("Only a Sales Manager can override the maximum discount."))


class Quotation(MaxDiscountOverride, _Quotation):
	pass


class SalesOrder(MaxDiscountOverride, _SalesOrder):
	pass


class SalesInvoice(MaxDiscountOverride, _SalesInvoice):
	pass


class DeliveryNote(MaxDiscountOverride, _DeliveryNote):
	pass

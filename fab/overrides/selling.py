from __future__ import annotations

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice as _SalesInvoice
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals as _taxes_and_totals
from erpnext.selling.doctype.quotation.quotation import Quotation as _Quotation
from erpnext.selling.doctype.quotation.quotation import get_ordered_items
from erpnext.selling.doctype.quotation.quotation import make_sales_order as _make_sales_order
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


def rows_for_totals(items):
	"""The rows a quotation is totalled on.

	ERPNext already drops the alternative rows (calculate_taxes_and_totals.filter_rows);
	an optional row is quoted with its price but is not part of the offer either, so it
	leaves the totals the same way.
	"""
	return [
		row for row in items if not row.get("is_alternative") and not row.get("fab_is_optional")
	]


def validate_optional_items(items):
	"""An optional row stands on its own: it neither replaces a row nor is replaced.

	The parent of an alternative is the closest row above it that is not itself an
	alternative, the same rule Quotation.get_rows_with_alternatives() reads the table by.
	"""
	parent = None

	for row in items:
		if row.get("fab_is_optional") and row.get("is_alternative"):
			frappe.throw(
				_("Row {0}: an item cannot be optional and alternative at the same time.").format(row.idx)
			)

		if row.get("fab_is_optional") and row.get("has_alternative_item"):
			frappe.throw(_("Row {0}: an optional item cannot have alternative items.").format(row.idx))

		if row.get("is_alternative"):
			if parent is not None and parent.get("fab_is_optional"):
				frappe.throw(
					_("Row {0} is an alternative to the optional item in row {1}.").format(
						row.idx, parent.idx
					)
				)
		else:
			parent = row


class QuotationTotals(_taxes_and_totals):
	"""Widen the ERPNext row filter to the optional rows."""

	def filter_rows(self):
		return rows_for_totals(self.doc.get("items"))


class OptionalItems:
	"""Odoo style optional lines: printed with their price, outside the totals, and
	picked one by one when the quotation becomes an order."""

	def validate(self):
		super().validate()
		validate_optional_items(self.get("items"))

	def calculate_taxes_and_totals(self):
		# the base implementation is calculate_taxes_and_totals(self) plus the commission
		# of the later selling documents, which a quotation never reaches
		QuotationTotals(self)

	def get_valid_items(self):
		"""Widen the ERPNext filter, which drops the rows of an alternatives set that were
		not ordered, to the optional rows nobody picked."""
		ordered_items = get_ordered_items(self.name)
		return [
			row
			for row in super().get_valid_items()
			if not row.get("fab_is_optional") or row.name in ordered_items
		]

	def get_ordered_status(self):
		"""ERPNext only asks get_valid_items() when the quotation carries alternatives, so
		an optional row the customer never ordered would hold it at Partially Ordered."""
		if not any(row.get("fab_is_optional") for row in self.get("items")):
			return super().get_ordered_status()

		ordered_items = get_ordered_items(self.name)
		if not ordered_items:
			return "Open"

		self._items = self.get_valid_items()
		for row in self._items:
			if row.name not in ordered_items or row.stock_qty > ordered_items[row.name]:
				return "Partially Ordered"

		return "Ordered"


class Quotation(MaxDiscountOverride, OptionalItems, _Quotation):
	pass


class SalesOrder(MaxDiscountOverride, _SalesOrder):
	pass


class SalesInvoice(MaxDiscountOverride, _SalesInvoice):
	pass


class DeliveryNote(MaxDiscountOverride, _DeliveryNote):
	pass


@frappe.whitelist()
def make_sales_order(source_name: str, target_doc=None, args=None):
	"""Keep the optional rows the customer did not pick out of the order.

	ERPNext maps an unselected row only when it belongs to an alternatives set, so a
	plain optional row would always come along. The selection travels in the same
	flags the alternative dialog uses, so the rows are dropped after the mapping
	instead of forking the whole mapper.
	"""
	doclist = _make_sales_order(source_name, target_doc, args=args)
	drop_unpicked_optional_items(source_name, doclist)
	return doclist


def drop_unpicked_optional_items(source_name: str, doclist):
	optional_rows = set(
		frappe.get_all(
			"Quotation Item",
			filters={"parent": source_name, "fab_is_optional": 1},
			pluck="name",
		)
	)
	if not optional_rows:
		return

	picked = {row.get("name") for row in frappe.flags.get("args", {}).get("selected_items", [])}
	kept = [
		row
		for row in doclist.get("items")
		if row.quotation_item not in optional_rows or row.quotation_item in picked
	]
	if len(kept) == len(doclist.get("items")):
		return

	doclist.set("items", kept)
	for idx, row in enumerate(kept, start=1):
		row.idx = idx

	doclist.run_method("calculate_taxes_and_totals")

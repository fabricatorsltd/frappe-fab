from __future__ import annotations

import unittest
from importlib import import_module
from unittest.mock import patch

from erpnext.controllers.selling_controller import SellingController

from fab.hooks import override_doctype_class
from fab.overrides.selling import MAX_DISCOUNT_OVERRIDE_ROLE, MaxDiscountOverride


class StandardController:
	"""Stands in for the ERPNext controller under the mixin.

	The real controllers need a site to be instantiated, so the mixin is mounted on
	a stub that only records whether the standard max discount check ran. That the
	mixin sits above the real controllers, and that its super() call reaches the
	ERPNext implementation, is asserted separately.
	"""

	def __init__(self, **values):
		self._values = values
		self.standard_check_ran = False

	def get(self, fieldname):
		return self._values.get(fieldname)

	def validate_max_discount(self):
		self.standard_check_ran = True


class Document(MaxDiscountOverride, StandardController):
	pass


class TestMaxDiscountOverride(unittest.TestCase):
	def test_standard_check_runs_when_the_flag_is_off(self):
		doc = Document(fab_max_discount_override=0)

		doc.validate_max_discount()

		self.assertTrue(doc.standard_check_ran)

	@patch("fab.overrides.selling.frappe")
	def test_an_empty_reason_is_refused(self, frappe):
		frappe.throw.side_effect = Exception
		doc = Document(fab_max_discount_override=1, fab_max_discount_override_reason="   ")

		with self.assertRaises(Exception):
			doc.validate_max_discount()

		self.assertFalse(doc.standard_check_ran)
		frappe.get_roles.assert_not_called()

	@patch("fab.overrides.selling.frappe")
	def test_a_sales_user_cannot_override(self, frappe):
		frappe.throw.side_effect = Exception
		frappe.get_roles.return_value = ["Sales User", "Employee"]
		doc = Document(
			fab_max_discount_override=1,
			fab_max_discount_override_reason="agreed with the customer",
		)

		with self.assertRaises(Exception):
			doc.validate_max_discount()

		self.assertFalse(doc.standard_check_ran)

	@patch("fab.overrides.selling.frappe")
	def test_a_sales_manager_with_a_reason_skips_the_check(self, frappe):
		frappe.throw.side_effect = Exception
		frappe.get_roles.return_value = ["Sales User", MAX_DISCOUNT_OVERRIDE_ROLE]
		doc = Document(
			fab_max_discount_override=1,
			fab_max_discount_override_reason="agreed with the customer",
		)

		doc.validate_max_discount()

		self.assertFalse(doc.standard_check_ran)
		frappe.throw.assert_not_called()

	def test_the_hooks_wire_every_selling_document_to_the_mixin(self):
		self.assertEqual(
			set(override_doctype_class),
			{"Quotation", "Sales Order", "Sales Invoice", "Delivery Note"},
		)

		for doctype, dotted_path in override_doctype_class.items():
			# frappe.get_attr() needs a site, so the path is resolved the plain way
			modulename, classname = dotted_path.rsplit(".", 1)
			controller = getattr(import_module(modulename), classname)

			self.assertTrue(issubclass(controller, MaxDiscountOverride), doctype)
			self.assertTrue(issubclass(controller, SellingController), doctype)
			# the mixin has to sit above the ERPNext controller, or its check is shadowed
			self.assertIs(controller.validate_max_discount, MaxDiscountOverride.validate_max_discount)
			self.assertIs(
				super(MaxDiscountOverride, controller).validate_max_discount,
				SellingController.validate_max_discount,
			)


if __name__ == "__main__":
	unittest.main()

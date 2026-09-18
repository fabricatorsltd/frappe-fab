from __future__ import annotations

import unittest
from unittest.mock import patch

from fab.overrides.selling import rows_for_totals, validate_optional_items


class Row(dict):
	"""Stands in for a Quotation Item row: get() and .idx are all the helpers read."""

	def __init__(self, idx, **flags):
		super().__init__(**flags)
		self.idx = idx


def throwing(frappe):
	"""frappe.throw() aborts the validation, so the mock has to raise as well."""

	def throw(message):
		raise ValueError(message)

	frappe.throw.side_effect = throw
	return frappe


class TestRowsForTotals(unittest.TestCase):
	def test_a_plain_row_is_counted(self):
		rows = [Row(1), Row(2)]

		self.assertEqual(rows_for_totals(rows), rows)

	def test_an_alternative_row_is_left_out(self):
		plain, alternative = Row(1), Row(2, is_alternative=1)

		self.assertEqual(rows_for_totals([plain, alternative]), [plain])

	def test_an_optional_row_is_left_out(self):
		plain, optional = Row(1), Row(2, fab_is_optional=1)

		self.assertEqual(rows_for_totals([plain, optional]), [plain])

	def test_the_order_of_the_counted_rows_is_kept(self):
		first, second, third = Row(1), Row(3), Row(5)
		rows = [first, Row(2, is_alternative=1), second, Row(4, fab_is_optional=1), third]

		self.assertEqual(rows_for_totals(rows), [first, second, third])


@patch("fab.overrides.selling.frappe")
class TestValidateOptionalItems(unittest.TestCase):
	def test_an_alternative_under_a_plain_row_is_accepted(self, frappe):
		throwing(frappe)

		validate_optional_items([Row(1), Row(2, is_alternative=1), Row(3, fab_is_optional=1)])

		frappe.throw.assert_not_called()

	def test_an_optional_row_after_an_alternatives_set_is_accepted(self, frappe):
		throwing(frappe)

		validate_optional_items(
			[Row(1), Row(2), Row(3, is_alternative=1), Row(4, fab_is_optional=1), Row(5, fab_is_optional=1)]
		)

		frappe.throw.assert_not_called()

	def test_a_row_cannot_be_optional_and_alternative(self, frappe):
		throwing(frappe)

		with self.assertRaises(ValueError):
			validate_optional_items([Row(1), Row(2, is_alternative=1, fab_is_optional=1)])

	def test_an_optional_row_cannot_be_the_parent_of_an_alternative(self, frappe):
		throwing(frappe)

		with self.assertRaises(ValueError):
			validate_optional_items([Row(1), Row(2, fab_is_optional=1), Row(3, is_alternative=1)])

	def test_an_optional_row_cannot_carry_the_parent_flag(self, frappe):
		throwing(frappe)

		with self.assertRaises(ValueError):
			validate_optional_items([Row(1, fab_is_optional=1, has_alternative_item=1)])

	def test_an_alternative_set_keeps_its_parent_across_its_own_rows(self, frappe):
		"""The parent is the closest row above that is not itself an alternative, so a
		second alternative in the same set is still checked against the plain row."""
		throwing(frappe)

		validate_optional_items([Row(1), Row(2, is_alternative=1), Row(3, is_alternative=1)])

		frappe.throw.assert_not_called()


if __name__ == "__main__":
	unittest.main()

from __future__ import annotations

import unittest

from fab.odoo_erpnext_import import (
	ERPNextImportSettings,
	build_item_code,
	build_supplier_bill_no,
	build_tax_rows,
	get_address_state_values,
	get_target_document_name,
	is_return_move,
	sanitize_code_fragment,
)


class TestOdooERPNextImport(unittest.TestCase):
	def setUp(self):
		self.settings = ERPNextImportSettings(
			company="fabricators",
			currency="EUR",
			customer_group="Commercial",
			supplier_group="Services",
			territory="All Territories",
			service_item_group="Services",
			product_item_group="Products",
			stock_uom="Nos",
			sales_income_account="4110 - Sales - fab",
			service_income_account="4120 - Service - fab",
			purchase_expense_account="5201 - Administrative Expenses - fab",
			purchase_surcharge_account="5201 - Administrative Expenses - fab",
			receivable_account="1310 - Debtors - fab",
			payable_account="2110 - Creditors - fab",
			sales_tax_account="VAT Output - fab",
			purchase_tax_account="VAT Input - fab",
			cost_center="Main - fab",
			sales_mode_of_payment="Wire Transfer",
		)

	def test_build_item_code_uses_normalized_product_code(self):
		self.assertEqual(build_item_code(26, "[ACQ-SVC-EXT]", "Imported line"), "ACQ-SVC-EXT-26")

	def test_build_item_code_falls_back_to_description(self):
		self.assertEqual(build_item_code(None, "", "Bank charges / monthly"), "ODOO-LINE-BANK-CHARGES-MONTHLY")

	def test_get_target_document_name_uses_exact_odoo_number(self):
		self.assertEqual(
			get_target_document_name({"odoo_number": "FIC/2025/001", "odoo_id": 10}, "Sales Invoice"),
			"FIC/2025/001",
		)

	def test_get_target_document_name_builds_stable_supplier_draft_name(self):
		self.assertEqual(
			get_target_document_name({"odoo_number": "", "odoo_id": 312}, "Purchase Invoice"),
			"ODOO-DRAFT-PI-312",
		)

	def test_build_supplier_bill_no_falls_back_to_odoo_id(self):
		self.assertEqual(build_supplier_bill_no({"odoo_number": "", "reference": "", "odoo_id": 99}), "ODOO-99")

	def test_build_tax_rows_maps_supplier_cassa_to_expense_account(self):
		move = {
			"tax_amount": 10.0,
			"lines": [
				{
					"subtotal": 250.0,
					"total": 260.0,
					"tax_names": [
						"0% - Operazione non soggetta a IVA ai sensi dell’art. 1,commi 54-89, Legge n. 190/2014 e succ. modifiche/integrazioni",
						"Cassa professionisti 4%",
					],
				}
			]
		}

		rows = build_tax_rows(move=move, doctype="Purchase Invoice", settings=self.settings)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["account_head"], "5201 - Administrative Expenses - fab")
		self.assertEqual(rows[0]["tax_amount"], 10.0)

	def test_build_tax_rows_maps_sales_tax_to_vat_output(self):
		move = {
			"tax_amount": 33.0,
			"lines": [
				{"subtotal": 100.0, "total": 122.0, "tax_names": ["22%"]},
				{"subtotal": 50.0, "total": 61.0, "tax_names": ["22%"]},
			]
		}

		rows = build_tax_rows(move=move, doctype="Sales Invoice", settings=self.settings)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["account_head"], "VAT Output - fab")
		self.assertEqual(rows[0]["tax_amount"], 33.0)

	def test_build_tax_rows_uses_header_tax_when_lines_have_no_delta(self):
		move = {
			"tax_amount": -5.71,
			"lines": [
				{
					"subtotal": 25.95,
					"total": 25.95,
					"tax_names": ["22% S EC"],
				}
			],
		}

		rows = build_tax_rows(move=move, doctype="Purchase Invoice", settings=self.settings)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["account_head"], "VAT Input - fab")
		self.assertEqual(rows[0]["tax_amount"], 5.71)

	def test_build_tax_rows_keep_negative_header_for_return_move(self):
		move = {
			"tax_amount": -1.41,
			"lines": [
				{
					"subtotal": -6.39,
					"total": -7.8,
					"tax_names": ["22% G"],
				}
			],
		}

		rows = build_tax_rows(move=move, doctype="Purchase Invoice", settings=self.settings)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["tax_amount"], -1.41)

	def test_build_tax_rows_keeps_zero_tax_sales_label(self):
		move = {
			"tax_amount": 0.0,
			"lines": [
				{
					"subtotal": 1420.0,
					"total": 1420.0,
					"tax_names": ["0% Art.7 ter D.P.R. 633/72"],
				}
			],
		}

		rows = build_tax_rows(move=move, doctype="Sales Invoice", settings=self.settings)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["account_head"], "VAT Output - fab")
		self.assertEqual(rows[0]["tax_amount"], 0.0)
		self.assertEqual(rows[0]["tax_exemption_reason"], "N2-Non Soggette")

	def test_sanitize_code_fragment_collapses_noise(self):
		self.assertEqual(sanitize_code_fragment("  hello / weird__code "), "HELLO-WEIRD-CODE")

	def test_is_return_move_detects_negative_lines(self):
		self.assertTrue(is_return_move({"lines": [{"subtotal": -1.0, "total": -1.22, "unit_price": -1.0, "quantity": 1.0}]}))

	def test_is_return_move_ignores_discount_lines_in_net_positive_doc(self):
		self.assertFalse(
			is_return_move(
				{
					"lines": [
						{"subtotal": 10.0, "total": 12.2, "unit_price": 10.0, "quantity": 1.0},
						{"subtotal": -1.0, "total": -1.22, "unit_price": -1.0, "quantity": 1.0},
					]
				}
			)
		)

	def test_get_address_state_values_use_temp_code_for_italy(self):
		state, state_code = get_address_state_values(
			move={"partner_country": "Italy"},
			settings=self.settings,
		)

		self.assertEqual(state, "RM")
		self.assertEqual(state_code, "RM")

	def test_get_address_state_values_keep_explicit_state_code(self):
		state, state_code = get_address_state_values(
			move={"partner_country": "Italy", "partner_state_code": "mi"},
			settings=self.settings,
		)

		self.assertEqual(state, "MI")
		self.assertEqual(state_code, "MI")

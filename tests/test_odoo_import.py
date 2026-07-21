from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fab.odoo_import import (
	build_move_domain,
	flatten_move_lines,
	normalize_move,
	write_invoice_bundle,
)


class TestOdooImport(unittest.TestCase):
	def test_build_move_domain_filters_posted_customer_invoices(self):
		self.assertEqual(
			build_move_domain("out_invoice", "2025-01-01", posted_only=True),
			[
				["move_type", "=", "out_invoice"],
				["invoice_date", ">=", "2025-01-01"],
				["state", "=", "posted"],
			],
		)

	def test_build_move_domain_keeps_supplier_states_open(self):
		self.assertEqual(
			build_move_domain("in_invoice", "2025-01-01", posted_only=False),
			[
				["move_type", "=", "in_invoice"],
				["invoice_date", ">=", "2025-01-01"],
			],
		)

	def test_normalize_move_skips_non_product_lines(self):
		move = {
			"id": 10,
			"name": "FATT/2025/00001",
			"state": "posted",
			"payment_state": "paid",
			"partner_id": [25, "Test Customer"],
			"invoice_date": "2025-03-13",
			"invoice_date_due": "2025-03-13",
			"ref": "",
			"currency_id": [125, "EUR"],
			"company_id": [1, "FABRICATORS S.R.L."],
			"amount_untaxed_signed": 100,
			"amount_tax_signed": 22,
			"amount_total_signed": 122,
			"invoice_line_ids": [91, 92],
		}
		line_lookup = {
			91: {
				"name": "Service line",
				"product_id": [3, "Servizi"],
				"quantity": 2,
				"price_unit": 50,
				"discount": 0,
				"tax_ids": [16],
				"price_subtotal": 100,
				"price_total": 122,
				"display_type": "product",
			},
			92: {
				"name": "Section",
				"product_id": False,
				"quantity": 0,
				"price_unit": 0,
				"discount": 0,
				"tax_ids": [],
				"price_subtotal": 0,
				"price_total": 0,
				"display_type": "line_section",
			},
		}
		product_lookup = {3: {"name": "Servizi", "default_code": "SERV", "detailed_type": "service"}}
		partner_lookup = {25: {"name": "Test Customer", "vat": "IT123", "country_id": [110, "Italia"]}}
		tax_lookup = {16: {"name": "22%"}}

		result = normalize_move(
			move=move,
			move_type="out_invoice",
			line_lookup=line_lookup,
			product_lookup=product_lookup,
			partner_lookup=partner_lookup,
			tax_lookup=tax_lookup,
		)

		self.assertEqual(result["partner_name"], "Test Customer")
		self.assertEqual(len(result["lines"]), 1)
		self.assertEqual(result["lines"][0]["product_code"], "SERV")
		self.assertEqual(result["lines"][0]["tax_names"], ["22%"])

	def test_write_invoice_bundle_creates_staging_files(self):
		bundle = {
			"date_from": "2025-01-01",
			"filters": {},
			"customer_invoices": [
				{
					"odoo_id": 10,
					"move_type": "out_invoice",
					"odoo_number": "FATT/2025/00001",
					"state": "posted",
					"payment_state": "paid",
					"reference": "",
					"invoice_date": "2025-03-13",
					"due_date": "2025-03-13",
					"partner_id": 25,
					"partner_name": "Test Customer",
					"partner_vat": "IT123",
					"partner_email": "",
					"partner_phone": "",
					"partner_street": "",
					"partner_city": "",
					"partner_zip": "",
					"partner_country": "Italia",
					"currency": "EUR",
					"company": "FABRICATORS S.R.L.",
					"untaxed_amount": 100,
					"tax_amount": 22,
					"total_amount": 122,
					"lines": [
						{
							"odoo_line_id": 91,
							"description": "Service line",
							"product_id": 3,
							"product_name": "Servizi",
							"product_code": "SERV",
							"product_type": "service",
							"quantity": 2,
							"unit_price": 50,
							"discount": 0,
							"tax_ids": [16],
							"tax_names": ["22%"],
							"subtotal": 100,
							"total": 122,
						}
					],
				}
			],
			"supplier_invoices": [],
		}

		with tempfile.TemporaryDirectory() as tmpdir:
			result = write_invoice_bundle(bundle, tmpdir)
			base = Path(tmpdir)
			self.assertEqual(result["customer_invoice_count"], 1)
			self.assertTrue((base / "odoo_invoice_bundle.json").exists())
			self.assertTrue((base / "odoo_customer_invoice_lines.csv").exists())
			loaded = json.loads((base / "odoo_invoice_bundle.json").read_text())
			self.assertEqual(loaded["customer_invoices"][0]["odoo_number"], "FATT/2025/00001")
			lines = flatten_move_lines(bundle["customer_invoices"])
			self.assertEqual(lines[0]["tax_names"], "22%")

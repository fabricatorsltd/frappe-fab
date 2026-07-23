from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frappe


DEFAULT_BUNDLE_CANDIDATES = (
	"./odoo_invoice_bundle.json",
	"/tmp/odoo_invoice_bundle.json",
)
CUSTOMER_BUNDLE_KEY = "customer_invoices"
SUPPLIER_BUNDLE_KEY = "supplier_invoices"
SUPPLIER_DRAFT_NAME_PREFIX = "ODOO-DRAFT-PI-"
MAX_ITEM_CODE_LENGTH = 140


@dataclass(slots=True)
class ERPNextImportSettings:
	company: str
	currency: str
	customer_group: str
	supplier_group: str
	territory: str
	service_item_group: str
	product_item_group: str
	stock_uom: str
	sales_income_account: str
	service_income_account: str
	purchase_expense_account: str
	purchase_surcharge_account: str
	receivable_account: str
	payable_account: str
	sales_tax_account: str
	purchase_tax_account: str
	cost_center: str
	sales_mode_of_payment: str
	temp_italy_state_code: str = "RM"


def import_odoo_invoice_bundle(
	bundle_path: str | None = None,
	company: str | None = None,
	customer_limit: int | None = None,
	supplier_limit: int | None = None,
	commit_every: int = 25,
) -> dict[str, Any]:
	settings = resolve_import_settings(company=company)
	ensure_price_neutral_import_settings()
	resolved_bundle_path = resolve_bundle_path(bundle_path)
	bundle = json.loads(resolved_bundle_path.read_text())
	results = {
		"bundle_path": str(resolved_bundle_path),
		"company": settings.company,
		"customers": {"created": 0, "updated": 0, "reused": 0},
		"suppliers": {"created": 0, "updated": 0, "reused": 0},
		"items": {"created": 0, "updated": 0, "reused": 0},
		"sales_invoices": {"created": 0, "submitted": 0, "cancelled": 0, "reused": 0},
		"purchase_invoices": {"created": 0, "submitted": 0, "cancelled": 0, "reused": 0},
		"warnings": [],
	}

	customer_moves = bundle.get(CUSTOMER_BUNDLE_KEY, [])
	supplier_moves = bundle.get(SUPPLIER_BUNDLE_KEY, [])
	if customer_limit is not None:
		customer_moves = customer_moves[:customer_limit]
	if supplier_limit is not None:
		supplier_moves = supplier_moves[:supplier_limit]

	processed = 0
	for move in customer_moves:
		import_move_safely(move=move, doctype="Sales Invoice", settings=settings, results=results)
		processed += 1
		if processed % commit_every == 0:
			frappe.db.commit()

	for move in supplier_moves:
		import_move_safely(move=move, doctype="Purchase Invoice", settings=settings, results=results)
		processed += 1
		if processed % commit_every == 0:
			frappe.db.commit()

	reset_invoice_series_counters()
	frappe.db.commit()
	frappe.clear_cache()
	return results


def ensure_price_neutral_import_settings() -> None:
	"""Keep ERPNext price automation away from imported rates.

	Imported lines are historical facts: without these switches ERPNext creates
	an Item Price from every invoice line and refills zero rates from the item
	price list or last purchase rate, silently inflating documents that carry
	unbilled or note lines.
	"""
	frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 0)
	frappe.db.set_single_value("Buying Settings", "disable_last_purchase_rate", 1)


def load_invoice_bundle(bundle_path: str | None) -> dict[str, Any]:
	path = resolve_bundle_path(bundle_path)
	return json.loads(path.read_text())


def resolve_bundle_path(bundle_path: str | None) -> Path:
	candidates = [bundle_path] if bundle_path else []
	candidates.extend(DEFAULT_BUNDLE_CANDIDATES)
	for candidate in candidates:
		if not candidate:
			continue
		path = Path(candidate).expanduser().resolve()
		if path.exists():
			return path
	raise FileNotFoundError("Could not find odoo_invoice_bundle.json. Pass bundle_path explicitly.")


def resolve_import_settings(company: str | None = None) -> ERPNextImportSettings:
	company_name = company or frappe.defaults.get_global_default("default_company") or frappe.get_all(
		"Company", pluck="name", limit_page_length=1
	)[0]
	company_doc = frappe.get_doc("Company", company_name)
	return ERPNextImportSettings(
		company=company_doc.name,
		currency=company_doc.default_currency,
		customer_group=pick_existing("Customer Group", ["Commercial", "All Customer Groups"]),
		supplier_group=pick_existing("Supplier Group", ["Services", "Local", "All Supplier Groups"]),
		territory=pick_existing("Territory", ["All Territories", "Italy", "Rest Of The World"]),
		service_item_group=pick_existing("Item Group", ["Services", "Products", "All Item Groups"]),
		product_item_group=pick_existing("Item Group", ["Products", "Consumable", "All Item Groups"]),
		stock_uom=ensure_import_uom(),
		sales_income_account=pick_existing(
			"Account",
			[company_doc.default_income_account, "4110 - Sales - fab", "4120 - Service - fab"],
			filters={"company": company_doc.name, "root_type": "Income", "is_group": 0},
		),
		service_income_account=pick_existing(
			"Account",
			[company_doc.default_income_account, "4120 - Service - fab", "4110 - Sales - fab"],
			filters={"company": company_doc.name, "root_type": "Income", "is_group": 0},
		),
		purchase_expense_account=pick_existing(
			"Account",
			[company_doc.default_expense_account, "5201 - Administrative Expenses - fab"],
			filters={"company": company_doc.name, "root_type": "Expense", "is_group": 0},
		),
		purchase_surcharge_account=pick_existing(
			"Account",
			[company_doc.default_expense_account, "5201 - Administrative Expenses - fab"],
			filters={"company": company_doc.name, "root_type": "Expense", "is_group": 0},
		),
		receivable_account=company_doc.default_receivable_account,
		payable_account=company_doc.default_payable_account,
		sales_tax_account=company_doc.fab_itx_vat_output_account,
		purchase_tax_account=company_doc.fab_itx_vat_input_account,
		cost_center=company_doc.cost_center,
		sales_mode_of_payment=pick_existing_mode_of_payment(),
		temp_italy_state_code=get_temp_italy_state_code(),
	)


def import_move_safely(
	move: dict[str, Any],
	doctype: str,
	settings: ERPNextImportSettings,
	results: dict[str, Any],
) -> None:
	"""Import one document, isolating failures so the run continues.

	Some source documents cannot be created at all (e.g. a party with no tax id
	fails the regional validate on insert). Rolling back to a per document
	savepoint drops that document cleanly and records it, instead of aborting
	the whole migration.
	"""
	frappe.db.savepoint("before_move")
	try:
		import_move(move=move, doctype=doctype, settings=settings, results=results)
	except Exception as exc:
		frappe.db.rollback(save_point="before_move")
		bucket = results["sales_invoices" if doctype == "Sales Invoice" else "purchase_invoices"]
		increment(bucket, "skipped")
		results["warnings"].append(
			f"{doctype} {move.get('odoo_number')}: skipped, {str(exc)[:200]}"
		)


def import_move(
	move: dict[str, Any],
	doctype: str,
	settings: ERPNextImportSettings,
	results: dict[str, Any],
) -> str:
	target_name = get_target_document_name(move=move, doctype=doctype)
	status_bucket = results["sales_invoices" if doctype == "Sales Invoice" else "purchase_invoices"]
	existing_name = find_existing_document(move=move, doctype=doctype, target_name=target_name)
	if existing_name:
		doc = frappe.get_doc(doctype, existing_name)
		finalize_imported_document(doc=doc, move=move, settings=settings, results=results, status_bucket=status_bucket)
		increment(status_bucket, "reused")
		return doc.name

	if doctype == "Sales Invoice":
		party_name = ensure_customer(move=move, settings=settings, results=results)
		address_name = ensure_party_address("Customer", party_name, move, settings)
	else:
		party_name = ensure_supplier(move=move, settings=settings, results=results)
		address_name = ensure_party_address("Supplier", party_name, move, settings)

	item_rows = build_item_rows(move=move, doctype=doctype, settings=settings, results=results)
	tax_rows = build_tax_rows(move=move, doctype=doctype, settings=settings)
	doc = frappe.get_doc(
		build_invoice_payload(
			move=move,
			doctype=doctype,
			party_name=party_name,
			address_name=address_name,
			items=item_rows,
			taxes=tax_rows,
			settings=settings,
		)
	)
	doc.insert(ignore_permissions=True)
	if target_name and doc.name != target_name:
		doc = rename_invoice(doc.doctype, doc.name, target_name)

	increment(status_bucket, "created")
	finalize_imported_document(doc=doc, move=move, settings=settings, results=results, status_bucket=status_bucket)
	return doc.name


def ensure_customer(move: dict[str, Any], settings: ERPNextImportSettings, results: dict[str, Any]) -> str:
	return ensure_party(
		doctype="Customer",
		name_field="customer_name",
		group_field="customer_group",
		group_value=settings.customer_group,
		move=move,
		results=results["customers"],
		defaults={
			"customer_type": "Company",
			"territory": settings.territory,
			"default_currency": settings.currency,
		},
	)


def ensure_supplier(move: dict[str, Any], settings: ERPNextImportSettings, results: dict[str, Any]) -> str:
	return ensure_party(
		doctype="Supplier",
		name_field="supplier_name",
		group_field="supplier_group",
		group_value=settings.supplier_group,
		move=move,
		results=results["suppliers"],
		defaults={
			"supplier_type": "Company",
			"country": move.get("partner_country") or "",
			"default_currency": settings.currency,
		},
	)


def ensure_party(
	doctype: str,
	name_field: str,
	group_field: str,
	group_value: str,
	move: dict[str, Any],
	results: dict[str, int],
	defaults: dict[str, Any],
) -> str:
	party_name = cstr(move.get("partner_name")).strip() or f"Odoo Partner {move.get('partner_id')}"
	tax_id = cstr(move.get("partner_vat")).strip()
	existing_name = find_existing_party(doctype=doctype, party_name=party_name, tax_id=tax_id)
	if existing_name:
		doc = frappe.get_doc(doctype, existing_name)
		changed = False
		if tax_id and not cstr(doc.get("tax_id")).strip():
			doc.tax_id = tax_id
			changed = True
		if doctype == "Supplier" and move.get("partner_country") and not cstr(doc.get("country")).strip():
			doc.country = move.get("partner_country")
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
			increment(results, "updated")
		else:
			increment(results, "reused")
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": doctype,
			name_field: get_unique_party_name(doctype=doctype, party_name=party_name, tax_id=tax_id, partner_id=move.get("partner_id")),
			group_field: group_value,
			"tax_id": tax_id,
			**defaults,
		}
	)
	doc.insert(ignore_permissions=True)
	increment(results, "created")
	return doc.name


def find_existing_party(doctype: str, party_name: str, tax_id: str) -> str | None:
	if tax_id:
		existing_name = frappe.db.get_value(doctype, {"tax_id": tax_id}, "name")
		if existing_name:
			return existing_name
	if frappe.db.exists(doctype, party_name):
		return party_name
	return None


def get_unique_party_name(doctype: str, party_name: str, tax_id: str, partner_id: int | None) -> str:
	if not frappe.db.exists(doctype, party_name):
		return party_name
	candidates = [tax_id, f"ODOO-{partner_id}" if partner_id else ""]
	for suffix in candidates:
		suffix = cstr(suffix).strip()
		if not suffix:
			continue
		candidate = truncate_value(f"{party_name} [{suffix}]", 140)
		if not frappe.db.exists(doctype, candidate):
			return candidate
	return frappe.generate_hash(length=10)


def partition_move_lines(move: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
	"""Split source lines into item lines and a document level discount.

	Odoo books some discounts as negative lines ("Bonus", promo credits).
	ERPNext refuses negative rates, so those lines are folded into a single
	invoice discount on net total. Lines whose sign opposes the document sign
	are the discounts; on credit notes the roles are inverted, since the
	exporter flips every line.
	"""
	doc_sign = -1.0 if is_return_move(move) else 1.0
	item_lines: list[dict[str, Any]] = []
	discount_total = 0.0
	for line in move.get("lines") or []:
		if flt(line.get("subtotal")) * doc_sign < 0:
			discount_total += abs(flt(line.get("subtotal")))
		else:
			item_lines.append(line)
	return item_lines, discount_total


def build_item_rows(
	move: dict[str, Any],
	doctype: str,
	settings: ERPNextImportSettings,
	results: dict[str, Any],
) -> list[dict[str, Any]]:
	rows = []
	is_return = is_return_move(move)
	item_lines, _ = partition_move_lines(move)
	for line in item_lines:
		item_code = ensure_item_for_line(line=line, doctype=doctype, settings=settings, results=results)
		item_name = get_item_display_name(line=line, fallback=item_code)
		qty = abs(flt(line.get("quantity")))
		subtotal = abs(flt(line.get("subtotal")))
		if qty:
			# subtotal is already net of the source discount, so deriving the
			# rate from it reproduces the billed amount exactly
			rate = subtotal / qty
		else:
			# zero quantity lines are notes or unbilled options: keep the text,
			# bill nothing even if the source kept a unit price on them
			qty = 1.0
			rate = subtotal
		common = {
			"item_code": item_code,
			"item_name": item_name,
			"description": line.get("description") or line.get("product_name") or item_name,
			"qty": (-1 if is_return else 1) * qty,
			"uom": settings.stock_uom,
			"conversion_factor": 1,
			"rate": rate,
			"cost_center": settings.cost_center,
		}
		if doctype == "Sales Invoice":
			common["income_account"] = get_income_account_for_line(line=line, settings=settings)
		else:
			common["expense_account"] = settings.purchase_expense_account
		rows.append(common)
	return rows


def get_net_rounding_gap(
	move: dict[str, Any],
	items: list[dict[str, Any]],
	discount_total: float,
) -> float:
	"""Measure how far the built rows drift from the untaxed total Odoo booked.

	Odoo computes header totals from unrounded line values, so the sum of the
	rounded line subtotals it exports can drift by a cent, and item rates are
	stored at currency precision on top. The header is what reached SdI: the
	gap is folded into the document level discount to land exactly on it.
	"""
	header_net = round(flt(move.get("untaxed_amount")), 2)
	if not header_net or not items:
		return 0.0
	target = abs(header_net) + discount_total
	actual = sum(abs(round(round(flt(row["rate"]), 2) * flt(row["qty"]), 2)) for row in items)
	gap = round(actual - target, 2)
	if abs(gap) > 0.05:
		return 0.0
	return gap


def ensure_item_for_line(
	line: dict[str, Any],
	doctype: str,
	settings: ERPNextImportSettings,
	results: dict[str, Any],
) -> str:
	item_code = build_item_code(
		product_id=line.get("product_id"),
		product_code=line.get("product_code"),
		description=line.get("description"),
	)
	existing = frappe.db.exists("Item", item_code)
	is_sales_item = 1 if doctype == "Sales Invoice" else 0
	is_purchase_item = 1 if doctype == "Purchase Invoice" else 0

	if existing:
		doc = frappe.get_doc("Item", item_code)
		changed = False
		if doc.stock_uom != settings.stock_uom:
			doc.stock_uom = settings.stock_uom
			changed = True
		if is_sales_item and not doc.is_sales_item:
			doc.is_sales_item = 1
			changed = True
		if is_purchase_item and not doc.is_purchase_item:
			doc.is_purchase_item = 1
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
			increment(results["items"], "updated")
		else:
			increment(results["items"], "reused")
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": get_item_display_name(line=line, fallback=item_code),
			"description": line.get("description") or line.get("product_name") or item_code,
			"item_group": get_item_group_for_line(line=line, settings=settings),
			"stock_uom": settings.stock_uom,
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"is_sales_item": is_sales_item,
			"is_purchase_item": is_purchase_item,
		}
	)
	doc.insert(ignore_permissions=True)
	increment(results["items"], "created")
	return doc.name


def build_invoice_payload(
	move: dict[str, Any],
	doctype: str,
	party_name: str,
	address_name: str | None,
	items: list[dict[str, Any]],
	taxes: list[dict[str, Any]],
	settings: ERPNextImportSettings,
) -> dict[str, Any]:
	due_date = get_effective_due_date(move)
	payload = {
		"doctype": doctype,
		"company": settings.company,
		"currency": settings.currency,
		"is_return": 1 if is_return_move(move) else 0,
		"posting_date": move.get("invoice_date"),
		"set_posting_time": 1,
		"posting_time": "00:00:00",
		"due_date": due_date,
		"update_stock": 0,
		"ignore_pricing_rule": 1,
		"remarks": build_remarks(move),
		"items": items,
		"taxes": taxes,
	}
	_, discount_total = partition_move_lines(move)
	discount_total += get_net_rounding_gap(move=move, items=items, discount_total=discount_total)
	if discount_total:
		# negative on returns: the discount must shrink the absolute value of an
		# already negative net total
		payload["apply_discount_on"] = "Net Total"
		payload["discount_amount"] = (-1 if payload["is_return"] else 1) * discount_total
	if doctype == "Sales Invoice":
		payload.update(
			{
				"customer": party_name,
				"customer_address": address_name,
				"debit_to": settings.receivable_account,
			}
		)
	else:
		payload.update(
			{
				"supplier": party_name,
				"supplier_address": address_name,
				"credit_to": settings.payable_account,
				"bill_no": build_supplier_bill_no(move),
				"bill_date": move.get("invoice_date"),
			}
		)
	return payload


def build_tax_rows(
	move: dict[str, Any],
	doctype: str,
	settings: ERPNextImportSettings,
) -> list[dict[str, Any]]:
	tax_map: dict[tuple[str, str], float] = defaultdict(float)
	tax_labels: list[str] = []
	is_return = is_return_move(move)
	for line in move.get("lines") or []:
		tax_amount = round(flt(line.get("total")) - flt(line.get("subtotal")), 2)
		tax_label = " | ".join(line.get("tax_names") or []) or "Imported Tax"
		if line.get("tax_names"):
			tax_labels.append(tax_label)
		if not tax_amount:
			continue
		account_head = get_tax_account_for_label(doctype=doctype, tax_label=tax_label, settings=settings)
		tax_map[(tax_label, account_head)] += tax_amount

	header_tax_amount = get_expected_tax_total(move=move, doctype=doctype, is_return=is_return)
	line_tax_amount = round(sum(tax_map.values()), 2)
	# per line rounding drifts a cent or two from the header total Odoo posted;
	# the header is the booked truth, so absorb any gap into the first tax row
	tax_gap = round(header_tax_amount - line_tax_amount, 2)
	if tax_gap and tax_labels:
		fallback_label = tax_labels[0]
		fallback_account = get_tax_account_for_label(doctype=doctype, tax_label=fallback_label, settings=settings)
		tax_map[(fallback_label, fallback_account)] += tax_gap
	elif doctype == "Sales Invoice" and not tax_map and tax_labels:
		fallback_label = tax_labels[0]
		fallback_account = get_tax_account_for_label(doctype=doctype, tax_label=fallback_label, settings=settings)
		tax_map[(fallback_label, fallback_account)] = 0.0

	return [
		{
			"charge_type": "Actual",
			"account_head": account_head,
			"description": truncate_value(tax_label, 140),
			"tax_amount": round(amount, 2),
			"cost_center": settings.cost_center,
			"tax_exemption_reason": get_tax_exemption_reason(tax_label) if doctype == "Sales Invoice" and round(amount, 2) == 0 else "",
		}
		for (tax_label, account_head), amount in tax_map.items()
	]


def ensure_party_address(
	party_doctype: str,
	party_name: str,
	move: dict[str, Any],
	settings: ERPNextImportSettings,
) -> str | None:
	country = get_existing_country(move.get("partner_country"))
	address_line1 = cstr(move.get("partner_street")).strip()
	city = cstr(move.get("partner_city")).strip()
	pincode = cstr(move.get("partner_zip")).strip()
	state, state_code = get_address_state_values(move=move, settings=settings)
	if not any([address_line1, city, pincode, country, state]):
		return None

	existing_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": party_doctype, "link_name": party_name, "parenttype": "Address"},
		"parent",
	)
	if existing_name:
		address = frappe.get_doc("Address", existing_name)
		changed = False
		if state and not cstr(address.get("state")).strip():
			address.state = state
			changed = True
		if state_code and address.meta.get_field("state_code") and not cstr(address.get("state_code")).strip():
			address.state_code = state_code
			changed = True
		if changed:
			address.save(ignore_permissions=True)
		update_primary_address_reference(party_doctype, party_name, existing_name)
		return existing_name

	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": truncate_value(party_name, 140),
			"address_type": "Billing",
			"address_line1": address_line1 or truncate_value(party_name, 140),
			"city": city,
			"pincode": pincode,
			"country": country,
			"state": state,
			"is_primary_address": 1,
			"links": [{"link_doctype": party_doctype, "link_name": party_name}],
		}
	)
	if state_code and address.meta.get_field("state_code"):
		address.state_code = state_code
	address.insert(ignore_permissions=True)
	update_primary_address_reference(party_doctype, party_name, address.name)
	return address.name


def update_primary_address_reference(party_doctype: str, party_name: str, address_name: str) -> None:
	fieldname = "customer_primary_address" if party_doctype == "Customer" else "supplier_primary_address"
	current_value = frappe.db.get_value(party_doctype, party_name, fieldname)
	if current_value == address_name:
		return
	frappe.db.set_value(party_doctype, party_name, fieldname, address_name, update_modified=False)


def get_existing_country(country_name: str | None) -> str | None:
	country = cstr(country_name).strip()
	if not country:
		return None
	if frappe.db.exists("Country", country):
		return country
	return None


def get_address_state_values(move: dict[str, Any], settings: ERPNextImportSettings) -> tuple[str, str]:
	state = cstr(move.get("partner_state")).strip()
	state_code = cstr(move.get("partner_state_code")).strip().upper()
	country = cstr(move.get("partner_country")).strip()
	if not state_code and len(state) == 2:
		state_code = state.upper()
	if country == "Italy" and not state_code:
		state_code = settings.temp_italy_state_code
	if not state and state_code:
		state = state_code
	return state, state_code


def get_tax_account_for_label(
	doctype: str,
	tax_label: str,
	settings: ERPNextImportSettings,
) -> str:
	if doctype == "Sales Invoice":
		return settings.sales_tax_account
	if "cassa professionisti" in tax_label.lower():
		return settings.purchase_surcharge_account
	return settings.purchase_tax_account


def get_tax_exemption_reason(tax_label: str) -> str:
	label = tax_label.lower()
	if "art.7 ter" in label or "non soggetta" in label:
		return "N2-Non Soggette"
	if "eu" in label or "ex" in label or "non impon" in label:
		return "N3-Non Imponibili"
	if "esent" in label:
		return "N4-Esenti"
	if "marg" in label:
		return "N5-Regime del margine / IVA non esposta in fattura"
	if "reverse" in label or "inversione" in label:
		return "N6-Inversione Contabile"
	return "N2-Non Soggette"


def build_remarks(move: dict[str, Any]) -> str:
	parts = [f"Imported from Odoo account.move {move.get('odoo_id')}."]
	if move.get("odoo_number"):
		parts.append(f"Source number: {move.get('odoo_number')}.")
	if move.get("reference"):
		parts.append(f"Source reference: {move.get('reference')}.")
	if move.get("payment_state"):
		parts.append(f"Source payment state: {move.get('payment_state')}.")
	return " ".join(parts)


def get_target_document_name(move: dict[str, Any], doctype: str) -> str | None:
	source_number = cstr(move.get("odoo_number")).strip()
	if source_number:
		return source_number
	if doctype == "Purchase Invoice":
		return f"{SUPPLIER_DRAFT_NAME_PREFIX}{move.get('odoo_id')}"
	return None


def find_existing_document(move: dict[str, Any], doctype: str, target_name: str | None) -> str | None:
	if target_name and frappe.db.exists(doctype, target_name):
		return target_name
	if doctype == "Purchase Invoice":
		bill_no = build_supplier_bill_no(move)
		existing_name = frappe.db.get_value(
			"Purchase Invoice",
			{"supplier": find_existing_party("Supplier", cstr(move.get("partner_name")).strip(), cstr(move.get("partner_vat")).strip()), "bill_no": bill_no},
			"name",
		)
		if existing_name:
			return existing_name
	return None


def build_supplier_bill_no(move: dict[str, Any]) -> str:
	return cstr(move.get("odoo_number")).strip() or cstr(move.get("reference")).strip() or f"ODOO-{move.get('odoo_id')}"


def rename_invoice(doctype: str, old_name: str, new_name: str):
	if old_name == new_name:
		return frappe.get_doc(doctype, old_name)
	if frappe.db.exists(doctype, new_name):
		return frappe.get_doc(doctype, new_name)
	frappe.rename_doc(doctype, old_name, new_name, force=True)
	return frappe.get_doc(doctype, new_name)


def validate_import_totals(move: dict[str, Any], doc, results: dict[str, Any]) -> None:
	# grand_total is negative on returns while the source header is unsigned
	source_total = round(abs(flt(move.get("total_amount"))), 2) * (-1 if is_return_move(move) else 1)
	if abs(source_total - flt(doc.grand_total)) > 0.005:
		results["warnings"].append(
			f"{doc.doctype} {doc.name}: source total {source_total:.2f} != ERPNext total {flt(doc.grand_total):.2f}"
		)


def finalize_imported_document(
	doc,
	move: dict[str, Any],
	settings: ERPNextImportSettings,
	results: dict[str, Any],
	status_bucket: dict[str, int],
) -> None:
	source_state = cstr(move.get("state")).lower()
	submitted = False
	if source_state == "posted" and doc.docstatus == 0:
		apply_sales_payment_schedule_defaults(doc=doc, move=move, settings=settings)
		frappe.db.savepoint("before_submit")
		try:
			doc.submit()
			submitted = True
			increment(status_bucket, "submitted")
		except frappe.ValidationError as exc:
			# one document failing a regional check (e.g. a party with no tax id)
			# must not abort the whole run; keep it as a draft to fix by hand
			frappe.db.rollback(save_point="before_submit")
			doc.reload()
			results["warnings"].append(
				f"{doc.doctype} {doc.name} ({move.get('odoo_number')}): left as draft, {exc}"
			)
			increment(status_bucket, "draft")
	elif source_state == "cancel" and doc.docstatus == 0:
		results["warnings"].append(
			f"{doc.doctype} {doc.name}: source state is cancelled; imported as draft to avoid GL reversal locking."
		)
	validate_import_totals(move=move, doc=doc, results=results)
	if submitted:
		close_settled_invoice(doc=doc, move=move, settings=settings, results=results)


MIGRATION_CLEARING_ACCOUNT = "Odoo Migration Clearing"
SETTLED_PAYMENT_STATES = ("paid", "in_payment")


def close_settled_invoice(doc, move: dict[str, Any], settings: ERPNextImportSettings, results: dict[str, Any]) -> None:
	"""Clear the open balance of invoices Odoo reports as settled.

	Source payments are not migrated, so without this every historical invoice
	would sit in the ageing report as receivable or payable. The offset goes to
	a dedicated clearing account: its balance shows the migration adjustment at
	a glance and keeps real bank accounts out of it.
	"""
	if doc.docstatus != 1:
		return
	if cstr(move.get("payment_state")).lower() not in SETTLED_PAYMENT_STATES:
		return
	outstanding = flt(doc.outstanding_amount)
	if not outstanding:
		return

	clearing_account = ensure_migration_clearing_account(settings)
	is_sales = doc.doctype == "Sales Invoice"
	party_account = doc.debit_to if is_sales else doc.credit_to
	party_type = "Customer" if is_sales else "Supplier"
	party = doc.customer if is_sales else doc.supplier
	party_row: dict[str, Any] = {
		"account": party_account,
		"party_type": party_type,
		"party": party,
		"reference_type": doc.doctype,
		"reference_name": doc.name,
		"cost_center": settings.cost_center,
	}
	clearing_row: dict[str, Any] = {"account": clearing_account, "cost_center": settings.cost_center}
	if (is_sales and outstanding > 0) or (not is_sales and outstanding < 0):
		party_row["credit_in_account_currency"] = abs(outstanding)
		clearing_row["debit_in_account_currency"] = abs(outstanding)
	else:
		party_row["debit_in_account_currency"] = abs(outstanding)
		clearing_row["credit_in_account_currency"] = abs(outstanding)

	entry = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": settings.company,
			"posting_date": doc.posting_date,
			"user_remark": f"Odoo migration settlement for {doc.doctype} {doc.name}",
			"accounts": [party_row, clearing_row],
		}
	)
	entry.flags.ignore_permissions = True
	entry.insert()
	entry.submit()
	increment(results.setdefault("closures", {}), "settled")


def ensure_migration_clearing_account(settings: ERPNextImportSettings) -> str:
	existing = frappe.db.get_value(
		"Account", {"company": settings.company, "account_name": MIGRATION_CLEARING_ACCOUNT}, "name"
	)
	if existing:
		return existing

	parent = frappe.db.get_value(
		"Account",
		{"company": settings.company, "root_type": "Asset", "is_group": 1, "parent_account": ["in", ["", None]]},
		"name",
	)
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"company": settings.company,
			"account_name": MIGRATION_CLEARING_ACCOUNT,
			"parent_account": parent,
			"root_type": "Asset",
			"is_group": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def reset_invoice_series_counters() -> None:
	reset_series_counter("Sales Invoice", "FATT/")
	reset_series_counter("Sales Invoice", "NDC/")
	reset_series_counter("Purchase Invoice", "ACQ/")
	reset_series_counter("Purchase Invoice", "RACQ/")


def reset_series_counter(doctype: str, prefix: str) -> None:
	series_totals: dict[str, int] = {}
	for name in frappe.get_all(doctype, pluck="name"):
		match = re.match(rf"^{re.escape(prefix)}(\d{{4}})/(\d+)$", cstr(name))
		if not match:
			continue
		year, number = match.groups()
		series_totals[f"{prefix}{year}/"] = max(series_totals.get(f"{prefix}{year}/", 0), cint(number))

	for series_name in frappe.db.sql(
		"select name from `tabSeries` where name like %s",
		(f"{prefix}%",),
		as_list=True,
	):
		if series_name[0] not in series_totals:
			frappe.db.sql("delete from `tabSeries` where name = %s", (series_name[0],))

	for series_name, current in series_totals.items():
		if frappe.db.sql("select name from `tabSeries` where name = %s", (series_name,)):
			frappe.db.sql("update `tabSeries` set current = %s where name = %s", (current, series_name))
		else:
			frappe.db.sql("insert into `tabSeries` (name, current) values (%s, %s)", (series_name, current))


def get_item_group_for_line(line: dict[str, Any], settings: ERPNextImportSettings) -> str:
	if cstr(line.get("product_type")).strip().lower() == "service":
		return settings.service_item_group
	return settings.product_item_group


def get_income_account_for_line(line: dict[str, Any], settings: ERPNextImportSettings) -> str:
	if cstr(line.get("product_type")).strip().lower() == "service":
		return settings.service_income_account
	return settings.sales_income_account


def is_return_move(move: dict[str, Any]) -> bool:
	move_type = cstr(move.get("move_type")).strip()
	if move_type:
		return move_type.endswith("_refund")
	# older bundles carry no move_type: fall back to the line sign, rounded so
	# float dust on zero total documents cannot flip the classification
	line_total = sum(flt(line.get("total")) or flt(line.get("subtotal")) for line in move.get("lines") or [])
	return round(line_total, 2) < 0


def get_expected_tax_total(move: dict[str, Any], doctype: str, is_return: bool) -> float:
	header_tax_amount = round(flt(move.get("tax_amount")), 2)
	if is_return:
		# the exporter ships signed header amounts: negative for out_refund but
		# positive for in_refund, while an ERPNext return always needs negative tax
		return -abs(header_tax_amount)
	if doctype == "Purchase Invoice":
		return abs(header_tax_amount)
	return header_tax_amount


def get_item_display_name(line: dict[str, Any], fallback: str) -> str:
	return truncate_value(line.get("product_name") or line.get("description") or fallback, 140)


def get_effective_due_date(move: dict[str, Any]) -> str:
	invoice_date = cstr(move.get("invoice_date")).strip()
	due_date = cstr(move.get("due_date")).strip()
	if not due_date:
		return invoice_date
	if not invoice_date:
		return due_date
	return max(invoice_date, due_date)


def apply_sales_payment_schedule_defaults(doc, move: dict[str, Any], settings: ERPNextImportSettings) -> None:
	if doc.doctype != "Sales Invoice" or not settings.sales_mode_of_payment:
		return
	if not doc.get("payment_schedule"):
		doc.append(
			"payment_schedule",
			{
				"due_date": get_effective_due_date(move),
				"invoice_portion": 100,
				"payment_amount": abs(flt(move.get("total_amount"))),
			},
		)
	for row in doc.payment_schedule:
		if not row.mode_of_payment:
			row.mode_of_payment = settings.sales_mode_of_payment


def build_item_code(product_id: int | None, product_code: str | None, description: str | None) -> str:
	if product_id:
		code_prefix = sanitize_code_fragment(product_code) if product_code else "ODOO-PROD"
		return truncate_value(f"{code_prefix}-{product_id}", MAX_ITEM_CODE_LENGTH)
	description_code = sanitize_code_fragment(description)[:24] or "LINE"
	return truncate_value(f"ODOO-LINE-{description_code}", MAX_ITEM_CODE_LENGTH)


def sanitize_code_fragment(value: str | None) -> str:
	text = cstr(value).strip().upper()
	text = re.sub(r"[^A-Z0-9]+", "-", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")
	return text or "ODOO"


def pick_existing(doctype: str, candidates: list[str], filters: dict[str, Any] | None = None) -> str:
	for candidate in candidates:
		if candidate and frappe.db.exists(doctype, candidate):
			return candidate
	query_filters = dict(filters or {})
	# never fall back to a disabled account: posting against one is rejected
	if doctype == "Account" and "disabled" not in query_filters:
		query_filters["disabled"] = 0
	rows = frappe.get_all(doctype, filters=query_filters, pluck="name", limit_page_length=1)
	if not rows:
		raise ValueError(f"Missing required setup records for {doctype}.")
	return rows[0]


def pick_existing_mode_of_payment() -> str:
	preferred = frappe.db.get_value(
		"Mode of Payment",
		{"enabled": 1, "mode_of_payment_code": ["is", "set"], "name": "Wire Transfer"},
		"name",
	)
	if preferred:
		return preferred
	fallback = frappe.db.get_value(
		"Mode of Payment",
		{"enabled": 1, "mode_of_payment_code": ["is", "set"]},
		"name",
	)
	if not fallback:
		raise ValueError("Missing enabled Mode of Payment with mode_of_payment_code.")
	return fallback


def get_temp_italy_state_code() -> str:
	configured = cstr(frappe.conf.get("odoo_import_temp_italy_state_code")).strip().upper()
	return configured[:2] or "RM"


def ensure_import_uom() -> str:
	uom_name = "Imported Unit"
	if frappe.db.exists("UOM", uom_name):
		return uom_name
	frappe.get_doc(
		{
			"doctype": "UOM",
			"uom_name": uom_name,
			"must_be_whole_number": 0,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
	return uom_name


def increment(bucket: dict[str, int], key: str) -> None:
	bucket[key] = cint(bucket.get(key)) + 1


def truncate_value(value: str, limit: int) -> str:
	return cstr(value)[:limit]


def flt(value: Any) -> float:
	return float(value or 0)


def cint(value: Any) -> int:
	return int(value or 0)


def cstr(value: Any) -> str:
	return "" if value is None else str(value)

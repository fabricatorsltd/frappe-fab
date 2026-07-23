from __future__ import annotations

import csv
import json
import ssl
import xmlrpc.client
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATE_FROM = "2025-01-01"
CUSTOMER_MOVE_TYPE = "out_invoice"
CUSTOMER_REFUND_TYPE = "out_refund"
SUPPLIER_MOVE_TYPE = "in_invoice"
SUPPLIER_REFUND_TYPE = "in_refund"
MOVE_BATCH_SIZE = 200

MOVE_FIELDS = [
	"name",
	"move_type",
	"state",
	"payment_state",
	"partner_id",
	"invoice_date",
	"invoice_date_due",
	"ref",
	"currency_id",
	"company_id",
	"amount_untaxed_signed",
	"amount_tax_signed",
	"amount_total_signed",
	"invoice_line_ids",
]
LINE_FIELDS = [
	"name",
	"product_id",
	"quantity",
	"price_unit",
	"discount",
	"tax_ids",
	"price_subtotal",
	"price_total",
	"display_type",
]
PARTNER_FIELDS = [
	"name",
	"vat",
	"email",
	"phone",
	"street",
	"city",
	"zip",
	"country_id",
	"is_company",
	"customer_rank",
	"supplier_rank",
]
PRODUCT_FIELDS = [
	"name",
	"default_code",
	"uom_id",
	"sale_ok",
	"purchase_ok",
	"type",
]
TAX_FIELDS = [
	"name",
	"amount",
	"type_tax_use",
]


@dataclass(slots=True)
class OdooConnectionConfig:
	base_url: str
	database: str
	username: str
	password: str
	verify_ssl: bool = True


class OdooClient:
	def __init__(self, config: OdooConnectionConfig):
		self.config = config
		self._uid: int | None = None
		self._common = xmlrpc.client.ServerProxy(
			self._build_url("/xmlrpc/2/common"),
			allow_none=True,
			context=self._get_ssl_context(),
		)
		self._models = xmlrpc.client.ServerProxy(
			self._build_url("/xmlrpc/2/object"),
			allow_none=True,
			context=self._get_ssl_context(),
		)

	def authenticate(self) -> int:
		if self._uid is not None:
			return self._uid

		self._uid = self._common.authenticate(
			self.config.database,
			self.config.username,
			self.config.password,
			{},
		)
		if not self._uid:
			raise ValueError("Failed to authenticate against Odoo API.")
		return self._uid

	def execute_kw(
		self,
		model: str,
		method: str,
		args: list[Any] | None = None,
		kwargs: dict[str, Any] | None = None,
	):
		return self._models.execute_kw(
			self.config.database,
			self.authenticate(),
			self.config.password,
			model,
			method,
			args or [],
			kwargs or {},
		)

	def search_read(
		self,
		model: str,
		domain: list[list[Any]],
		fields: list[str],
		order: str = "id asc",
		limit: int = MOVE_BATCH_SIZE,
	) -> list[dict[str, Any]]:
		offset = 0
		rows: list[dict[str, Any]] = []
		while True:
			batch = self.execute_kw(
				model,
				"search_read",
				[domain],
				{
					"fields": fields,
					"offset": offset,
					"limit": limit,
					"order": order,
				},
			)
			if not batch:
				return rows
			rows.extend(batch)
			if len(batch) < limit:
				return rows
			offset += limit

	def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
		if not ids:
			return []
		return self.execute_kw(model, "read", [ids], {"fields": fields})

	def _build_url(self, path: str) -> str:
		return self.config.base_url.rstrip("/") + path

	def _get_ssl_context(self):
		if self.config.verify_ssl:
			return ssl.create_default_context()
		return ssl._create_unverified_context()


def export_odoo_invoice_bundle(
	base_url: str,
	database: str,
	username: str,
	password: str,
	output_dir: str,
	date_from: str = DEFAULT_DATE_FROM,
	verify_ssl: bool = True,
	company_id: int | None = None,
) -> dict[str, Any]:
	client = OdooClient(
		OdooConnectionConfig(
			base_url=base_url,
			database=database,
			username=username,
			password=password,
			verify_ssl=verify_ssl,
		)
	)
	customer_invoices = fetch_invoice_documents(
		client=client,
		move_type=CUSTOMER_MOVE_TYPE,
		date_from=date_from,
		posted_only=True,
		company_id=company_id,
	) + fetch_invoice_documents(
		client=client,
		move_type=CUSTOMER_REFUND_TYPE,
		date_from=date_from,
		posted_only=True,
		company_id=company_id,
	)
	supplier_invoices = fetch_invoice_documents(
		client=client,
		move_type=SUPPLIER_MOVE_TYPE,
		date_from=date_from,
		posted_only=True,
		company_id=company_id,
	) + fetch_invoice_documents(
		client=client,
		move_type=SUPPLIER_REFUND_TYPE,
		date_from=date_from,
		posted_only=True,
		company_id=company_id,
	)

	bundle = {
		"date_from": date_from,
		"filters": {
			"company_id": company_id,
			"customer_invoices": {
				"move_types": [CUSTOMER_MOVE_TYPE, CUSTOMER_REFUND_TYPE],
				"state": "posted",
			},
			"supplier_invoices": {
				"move_types": [SUPPLIER_MOVE_TYPE, SUPPLIER_REFUND_TYPE],
				"state": "posted",
			},
		},
		"customer_invoices": customer_invoices,
		"supplier_invoices": supplier_invoices,
	}
	return write_invoice_bundle(bundle, output_dir)


def fetch_invoice_documents(
	client: OdooClient,
	move_type: str,
	date_from: str,
	posted_only: bool,
	company_id: int | None = None,
) -> list[dict[str, Any]]:
	moves = client.search_read(
		"account.move",
		build_move_domain(
			move_type=move_type, date_from=date_from, posted_only=posted_only, company_id=company_id
		),
		fields=MOVE_FIELDS,
		order="invoice_date asc, id asc",
	)
	line_ids = unique_ids(
		line_id for move in moves for line_id in (move.get("invoice_line_ids") or [])
	)
	lines = {row["id"]: row for row in client.read("account.move.line", line_ids, LINE_FIELDS)}
	product_ids = unique_ids(
		value_id(row.get("product_id")) for row in lines.values() if value_id(row.get("product_id"))
	)
	partner_ids = unique_ids(
		value_id(move.get("partner_id")) for move in moves if value_id(move.get("partner_id"))
	)
	tax_ids = unique_ids(tax_id for row in lines.values() for tax_id in (row.get("tax_ids") or []))

	products = {row["id"]: row for row in client.read("product.product", product_ids, PRODUCT_FIELDS)}
	partners = {row["id"]: row for row in client.read("res.partner", partner_ids, PARTNER_FIELDS)}
	taxes = {row["id"]: row for row in client.read("account.tax", tax_ids, TAX_FIELDS)}

	return [
		normalize_move(
			move=move,
			move_type=move_type,
			line_lookup=lines,
			product_lookup=products,
			partner_lookup=partners,
			tax_lookup=taxes,
		)
		for move in moves
	]


def build_move_domain(
	move_type: str, date_from: str, posted_only: bool, company_id: int | None = None
) -> list[list[Any]]:
	domain: list[list[Any]] = [
		["move_type", "=", move_type],
		["invoice_date", ">=", date_from],
	]
	if posted_only:
		domain.append(["state", "=", "posted"])
	if company_id is not None:
		domain.append(["company_id", "=", company_id])
	return domain


def normalize_move(
	move: dict[str, Any],
	move_type: str,
	line_lookup: dict[int, dict[str, Any]],
	product_lookup: dict[int, dict[str, Any]],
	partner_lookup: dict[int, dict[str, Any]],
	tax_lookup: dict[int, dict[str, Any]],
) -> dict[str, Any]:
	partner_id = value_id(move.get("partner_id"))
	partner = partner_lookup.get(partner_id, {})
	# refund lines carry positive amounts in Odoo; the importer detects credit
	# notes from negative line totals, so flip the sign here
	line_sign = -1.0 if move_type.endswith("_refund") else 1.0
	lines = []
	for line_id in move.get("invoice_line_ids") or []:
		line = line_lookup.get(line_id)
		if not line or line.get("display_type") not in (False, None, "product"):
			continue
		product_id = value_id(line.get("product_id"))
		product = product_lookup.get(product_id, {})
		line_tax_ids = line.get("tax_ids") or []
		lines.append(
			{
				"odoo_line_id": line_id,
				"description": line.get("name") or "",
				"product_id": product_id,
				"product_name": display_name(line.get("product_id")) or product.get("name") or "",
				"product_code": product.get("default_code") or "",
				"product_type": product.get("detailed_type") or product.get("type") or "",
				"quantity": line.get("quantity") or 0.0,
				"unit_price": line.get("price_unit") or 0.0,
				"discount": line.get("discount") or 0.0,
				"tax_ids": line_tax_ids,
				"tax_names": [tax_lookup.get(tax_id, {}).get("name") or str(tax_id) for tax_id in line_tax_ids],
				"subtotal": line_sign * (line.get("price_subtotal") or 0.0),
				"total": line_sign * (line.get("price_total") or 0.0),
			}
		)

	return {
		"odoo_id": move["id"],
		"move_type": move_type,
		"odoo_number": move.get("name") or "",
		"state": move.get("state") or "",
		"payment_state": move.get("payment_state") or "",
		"reference": move.get("ref") or "",
		"invoice_date": move.get("invoice_date") or "",
		"due_date": move.get("invoice_date_due") or "",
		"partner_id": partner_id,
		"partner_name": display_name(move.get("partner_id")) or partner.get("name") or "",
		"partner_vat": partner.get("vat") or "",
		"partner_email": partner.get("email") or "",
		"partner_phone": partner.get("phone") or "",
		"partner_street": partner.get("street") or "",
		"partner_city": partner.get("city") or "",
		"partner_zip": partner.get("zip") or "",
		"partner_country": display_name(partner.get("country_id")) or "",
		"currency": display_name(move.get("currency_id")) or "",
		"company": display_name(move.get("company_id")) or "",
		"untaxed_amount": move.get("amount_untaxed_signed") or 0.0,
		"tax_amount": move.get("amount_tax_signed") or 0.0,
		"total_amount": move.get("amount_total_signed") or 0.0,
		"lines": lines,
	}


def write_invoice_bundle(bundle: dict[str, Any], output_dir: str) -> dict[str, Any]:
	output_path = Path(output_dir).expanduser().resolve()
	output_path.mkdir(parents=True, exist_ok=True)

	customer_invoices = bundle["customer_invoices"]
	supplier_invoices = bundle["supplier_invoices"]
	customer_lines = flatten_move_lines(customer_invoices)
	supplier_lines = flatten_move_lines(supplier_invoices)
	customer_partners = unique_partner_rows(customer_invoices)
	supplier_partners = unique_partner_rows(supplier_invoices)
	products = unique_product_rows(customer_lines + supplier_lines)

	write_json(output_path / "odoo_invoice_bundle.json", bundle)
	write_csv(output_path / "odoo_customer_invoices.csv", flatten_moves(customer_invoices))
	write_csv(output_path / "odoo_customer_invoice_lines.csv", customer_lines)
	write_csv(output_path / "odoo_customer_partners.csv", customer_partners)
	write_csv(output_path / "odoo_supplier_invoices.csv", flatten_moves(supplier_invoices))
	write_csv(output_path / "odoo_supplier_invoice_lines.csv", supplier_lines)
	write_csv(output_path / "odoo_supplier_partners.csv", supplier_partners)
	write_csv(output_path / "odoo_products.csv", products)

	return {
		"output_dir": str(output_path),
		"customer_invoice_count": len(customer_invoices),
		"customer_line_count": len(customer_lines),
		"supplier_invoice_count": len(supplier_invoices),
		"supplier_line_count": len(supplier_lines),
		"product_count": len(products),
		"customer_partner_count": len(customer_partners),
		"supplier_partner_count": len(supplier_partners),
	}


def flatten_moves(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [
		{
			key: value
			for key, value in move.items()
			if key != "lines"
		}
		for move in moves
	]


def flatten_move_lines(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
	rows: list[dict[str, Any]] = []
	for move in moves:
		for line in move.get("lines", []):
			rows.append(
				{
					"odoo_move_id": move["odoo_id"],
					"odoo_number": move["odoo_number"],
					"move_type": move["move_type"],
					"state": move["state"],
					"invoice_date": move["invoice_date"],
					"due_date": move["due_date"],
					"partner_name": move["partner_name"],
					"currency": move["currency"],
					**line,
					"tax_names": " | ".join(line.get("tax_names") or []),
					"tax_ids": " | ".join(str(tax_id) for tax_id in (line.get("tax_ids") or [])),
				}
			)
	return rows


def unique_partner_rows(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
	seen: set[int] = set()
	rows: list[dict[str, Any]] = []
	for move in moves:
		partner_id = move.get("partner_id")
		if not partner_id or partner_id in seen:
			continue
		seen.add(partner_id)
		rows.append(
			{
				"partner_id": partner_id,
				"partner_name": move.get("partner_name") or "",
				"partner_vat": move.get("partner_vat") or "",
				"partner_email": move.get("partner_email") or "",
				"partner_phone": move.get("partner_phone") or "",
				"partner_street": move.get("partner_street") or "",
				"partner_city": move.get("partner_city") or "",
				"partner_zip": move.get("partner_zip") or "",
				"partner_country": move.get("partner_country") or "",
			}
		)
	return rows


def unique_product_rows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
	seen: set[int] = set()
	rows: list[dict[str, Any]] = []
	for line in lines:
		product_id = line.get("product_id")
		if not product_id or product_id in seen:
			continue
		seen.add(product_id)
		rows.append(
			{
				"product_id": product_id,
				"product_code": line.get("product_code") or "",
				"product_name": line.get("product_name") or "",
				"product_type": line.get("product_type") or "",
			}
		)
	return rows


def write_json(path: Path, data: dict[str, Any]) -> None:
	path.write_text(json.dumps(data, indent=2, ensure_ascii=True, sort_keys=True))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
	if not rows:
		path.write_text("")
		return

	fieldnames = list(rows[0])
	with path.open("w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=fieldnames)
		writer.writeheader()
		writer.writerows(rows)


def unique_ids(values) -> list[int]:
	return sorted({value for value in values if value})


def value_id(value: Any) -> int | None:
	if isinstance(value, list | tuple) and value:
		return value[0]
	return value or None


def display_name(value: Any) -> str:
	if isinstance(value, list | tuple) and len(value) > 1:
		return str(value[1])
	return ""

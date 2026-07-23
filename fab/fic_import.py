from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

AUTOFATTURA_TYPES = {"TD16", "TD17", "TD18", "TD19"}
REFUND_TYPES = {"TD04", "TD08"}
PRIMANOTA_SKIP_ACCOUNTS = {"autofatture"}


def build_fic_invoice_bundle(
	source_dir: str,
	company_vat: str,
	company: str,
	output_path: str | None = None,
	aggregate_unmatched_purchases: bool = True,
) -> dict[str, Any]:
	"""Convert a Fatture in Cloud yearly export tree into an invoice bundle.

	Reads every FatturaPA XML under source_dir, splits documents by the
	CedentePrestatore identity (company_vat sells, anyone else bills us),
	deduplicates the SdI copies of self billed documents, and marks payment
	state from the primanota finanziaria exports found in the tree. The
	output matches the Odoo bundle contract, so the same importer, verifier
	and rebuild tooling apply unchanged.

	Purchase names are positional per year: the source tree must stay
	frozen between runs, or reruns will duplicate renumbered documents.
	"""
	root_dir = Path(source_dir).expanduser()
	warnings: list[str] = []
	primanota = load_primanota_movements(root_dir, warnings)
	documents = []
	seen: set[tuple] = set()
	for path in sorted(root_dir.rglob("*.[xX][mM][lL]")):
		for body_doc in parse_fatturapa_file(path, company_vat=company_vat, company=company, warnings=warnings):
			key = (
				body_doc["move_type"],
				body_doc["_tipo"],
				body_doc["_numero"],
				body_doc["invoice_date"],
				body_doc["partner_vat"] or body_doc["partner_name"],
			)
			if key in seen:
				continue
			seen.add(key)
			documents.append(body_doc)

	assign_document_names(documents)
	matched_out, matched_in = apply_payment_states(documents, primanota)
	matched_in += match_payments_by_party_amount(documents, primanota)

	customer_invoices = [d for d in documents if d["move_type"].startswith("out_")]
	supplier_invoices = [d for d in documents if d["move_type"].startswith("in_")]

	report = {
		"documents": len(documents),
		"customer_invoices": len(customer_invoices),
		"supplier_invoices": len(supplier_invoices),
		"primanota_movements": len(primanota),
		"matched_incassi": matched_out,
		"matched_pagamenti": matched_in,
	}

	non_eur = [d for d in documents if d["currency"] != "EUR"]
	if non_eur:
		warnings.append(f"{len(non_eur)} documents are not in EUR and will be booked at face value")

	if aggregate_unmatched_purchases:
		aggregates, aggregate_report = build_unmatched_purchase_aggregates(
			primanota, documents, company=company
		)
		supplier_invoices.extend(aggregates)
		report.update(aggregate_report)

	report["warnings"] = warnings

	for doc in customer_invoices + supplier_invoices:
		for key in [k for k in doc if k.startswith("_")]:
			doc.pop(key)

	bundle = {
		"source": "fatture_in_cloud",
		"filters": {"company_vat": company_vat},
		"customer_invoices": customer_invoices,
		"supplier_invoices": supplier_invoices,
	}
	if output_path:
		Path(output_path).write_text(json.dumps(bundle, indent=1, default=str))
		report["output_path"] = output_path
	return report


def strip_tag(tag: str) -> str:
	return tag.split("}")[-1]


def first_text(node, tag_name: str) -> str:
	for el in node.iter():
		if strip_tag(el.tag) == tag_name:
			return (el.text or "").strip()
	return ""


def first_node(node, tag_name: str):
	for el in node.iter():
		if strip_tag(el.tag) == tag_name:
			return el
	return None


def to_float(value: str) -> float:
	try:
		return float((value or "0").replace(",", "."))
	except ValueError:
		return 0.0


def parse_party(node) -> dict[str, str]:
	if node is None:
		return {}
	vat_node = first_node(node, "IdFiscaleIVA")
	vat = ""
	if vat_node is not None:
		vat = first_text(vat_node, "IdPaese") + first_text(vat_node, "IdCodice")
	name = first_text(node, "Denominazione")
	if not name:
		name = " ".join(filter(None, [first_text(node, "Nome"), first_text(node, "Cognome")]))
	sede = first_node(node, "Sede")
	return {
		"vat": vat,
		"fiscal_code": first_text(node, "CodiceFiscale"),
		"name": name,
		"street": " ".join(
			filter(None, [first_text(sede, "Indirizzo"), first_text(sede, "NumeroCivico")])
		)
		if sede is not None
		else "",
		"city": first_text(sede, "Comune") if sede is not None else "",
		"zip": first_text(sede, "CAP") if sede is not None else "",
		"country_code": first_text(sede, "Nazione") if sede is not None else "",
	}


def parse_fatturapa_file(
	path: Path, company_vat: str, company: str, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
	try:
		root = ET.fromstring(path.read_bytes())
	except ET.ParseError:
		if warnings is not None:
			warnings.append(f"{path.name}: XML parse error, skipped")
		return []
	if "Semplificata" in strip_tag(root.tag):
		# the simplified schema has no DettaglioLinee or DatiRiepilogo:
		# parsing it with the ordinary layout would emit a zero document
		if warnings is not None:
			warnings.append(f"{path.name}: FatturaElettronicaSemplificata not supported, skipped")
		return []
	header = first_node(root, "FatturaElettronicaHeader")
	if header is None:
		return []
	cedente = parse_party(first_node(header, "CedentePrestatore"))
	cessionario = parse_party(first_node(header, "CessionarioCommittente"))

	own_ids = {company_vat, company_vat.replace("IT", ""), ""}
	is_sale = cedente.get("vat") in (company_vat, "IT" + company_vat.replace("IT", "")) or (
		cedente.get("fiscal_code") and cedente["fiscal_code"] in own_ids
	)

	documents = []
	for body in [el for el in root.iter() if strip_tag(el.tag) == "FatturaElettronicaBody"]:
		documents.append(
			parse_body(
				body,
				partner=cessionario if is_sale else cedente,
				is_sale=is_sale,
				company=company,
				source_file=path.name,
				warnings=warnings,
			)
		)
	return [d for d in documents if d]


def parse_body(
	body,
	partner: dict,
	is_sale: bool,
	company: str,
	source_file: str,
	warnings: list[str] | None = None,
) -> dict[str, Any] | None:
	dati = first_node(body, "DatiGeneraliDocumento")
	if dati is None:
		return None
	tipo = first_text(dati, "TipoDocumento")
	numero = first_text(dati, "Numero")
	data = first_text(dati, "Data")
	divisa = first_text(dati, "Divisa") or "EUR"
	is_refund = tipo in REFUND_TYPES
	is_autofattura = tipo in AUTOFATTURA_TYPES
	if is_autofattura:
		is_sale = False

	lines = []
	for det in [el for el in body.iter() if strip_tag(el.tag) == "DettaglioLinee"]:
		qty = to_float(first_text(det, "Quantita")) or 1.0
		subtotal = to_float(first_text(det, "PrezzoTotale"))
		aliquota = to_float(first_text(det, "AliquotaIVA"))
		natura = first_text(det, "Natura")
		if is_autofattura:
			# self billed integration: the supplier is owed the taxable
			# amount only, VAT goes to the Erario on both registers
			aliquota = 0.0
		label = f"{aliquota:g}%" + (f" {natura}" if natura else "")
		lines.append(
			{
				"description": first_text(det, "Descrizione") or numero,
				"quantity": qty,
				"unit_price": to_float(first_text(det, "PrezzoUnitario")),
				"discount": 0.0,
				"subtotal": subtotal,
				"total": round(subtotal * (1 + aliquota / 100.0), 2),
				"tax_names": [label],
			}
		)

	# freelancer pension fund contributions are billed on top of the lines
	for cassa in [el for el in body.iter() if strip_tag(el.tag) == "DatiCassaPrevidenziale"]:
		importo = to_float(first_text(cassa, "ImportoContributoCassa"))
		if not importo:
			continue
		aliquota = to_float(first_text(cassa, "AliquotaIVA"))
		natura = first_text(cassa, "Natura")
		label = f"{aliquota:g}%" + (f" {natura}" if natura else "")
		lines.append(
			{
				"description": f"Cassa previdenziale {first_text(cassa, 'TipoCassa')}",
				"quantity": 1.0,
				"unit_price": importo,
				"discount": 0.0,
				"subtotal": importo,
				"total": round(importo * (1 + aliquota / 100.0), 2),
				"tax_names": [label],
			}
		)

	untaxed = tax = 0.0
	for riep in [el for el in body.iter() if strip_tag(el.tag) == "DatiRiepilogo"]:
		untaxed += to_float(first_text(riep, "ImponibileImporto"))
		tax += to_float(first_text(riep, "Imposta"))
	if is_autofattura:
		tax = 0.0
	total = round(untaxed + tax, 2)

	documento_total = to_float(first_text(dati, "ImportoTotaleDocumento"))
	if (
		warnings is not None
		and documento_total
		and not is_autofattura
		and abs(abs(documento_total) - abs(total)) > 0.01
	):
		# document level rounding, cash discounts or stamp duty move the
		# payable away from the riepilogo sum: surface it, the operator
		# decides whether the difference needs a manual entry
		warnings.append(
			f"{source_file} ({tipo} {numero}): ImportoTotaleDocumento {documento_total:.2f}"
			f" != riepilogo {total:.2f}"
		)

	# bundle sign contract: sales positive, purchases negative headers;
	# refunds flip both the header and every line
	header_sign = (1 if is_sale else -1) * (-1 if is_refund else 1)
	line_sign = -1 if is_refund else 1
	for line in lines:
		line["subtotal"] = line_sign * line["subtotal"]
		line["total"] = line_sign * line["total"]

	if is_sale:
		move_type = "out_refund" if is_refund else "out_invoice"
	else:
		move_type = "in_refund" if is_refund else "in_invoice"

	due_date = first_text(body, "DataScadenzaPagamento") or data
	partner_country = country_name_from_code(partner.get("country_code") or "IT")
	digest = hashlib.md5(f"{tipo}|{numero}|{data}|{partner.get('vat') or partner.get('name')}".encode()).hexdigest()

	return {
		"company": company,
		"currency": divisa,
		"move_type": move_type,
		"state": "posted",
		"invoice_date": data,
		"due_date": due_date,
		"odoo_id": int(digest[:8], 16),
		"odoo_number": "",
		"reference": numero,
		"payment_state": "not_paid",
		"partner_id": int(hashlib.md5((partner.get("vat") or partner.get("name") or "?").encode()).hexdigest()[:8], 16),
		"partner_name": partner.get("name") or "Controparte sconosciuta",
		# private individuals carry only a fiscal code: the regional sales
		# validation wants a tax id, and the fiscal code is the right one
		"partner_vat": partner.get("vat") or partner.get("fiscal_code") or "",
		"partner_street": partner.get("street") or "",
		"partner_city": partner.get("city") or "",
		"partner_zip": partner.get("zip") or "",
		"partner_country": partner_country,
		"partner_email": "",
		"partner_phone": "",
		# one sign for the whole header: abs-ing each component would break
		# internal consistency on documents with mixed sign riepilogo rows
		"total_amount": header_sign * round(total, 2),
		"untaxed_amount": header_sign * round(untaxed, 2),
		"tax_amount": header_sign * round(tax, 2),
		"lines": lines,
		"_tipo": tipo,
		"_numero": numero,
		"_fiscal_code": partner.get("fiscal_code") or "",
		"_source_file": source_file,
	}


def assign_document_names(documents: list[dict[str, Any]]) -> None:
	"""Sales keep their FIC number as FIC/<year>/<number>; purchases get a
	sequential per year name since the supplier number is not unique."""
	counters: dict[tuple, int] = defaultdict(int)
	assigned: set[str] = set()
	for doc in sorted(documents, key=lambda d: (d["invoice_date"], d["_numero"])):
		year = doc["invoice_date"][:4]
		if doc["move_type"].startswith("out_"):
			number = re.sub(r"[^A-Za-z0-9]+", "-", doc["_numero"]).strip("-")
			if number.isdigit():
				number = number.zfill(3)
			name = f"FIC/{year}/{number}"
			# distinct source numbers can clean to the same slug: a silent
			# collision would make the importer merge two documents
			suffix = 2
			while name in assigned:
				name = f"FIC/{year}/{number}-{suffix}"
				suffix += 1
			doc["odoo_number"] = name
		else:
			series = "FIC-AUTO" if doc["_tipo"] in AUTOFATTURA_TYPES else "FIC-ACQ"
			counters[(series, year)] += 1
			doc["odoo_number"] = f"{series}/{year}/{counters[(series, year)]:04d}"
		assigned.add(doc["odoo_number"])


def load_primanota_movements(root_dir: Path, warnings: list[str] | None = None) -> list[dict[str, Any]]:
	try:
		import xlrd
	except ImportError:
		if warnings is not None:
			warnings.append("xlrd not installed: payment states and aggregates skipped")
		return []
	movements = []
	for xls in sorted(root_dir.rglob("primanota finanziaria.xls")):
		try:
			book = xlrd.open_workbook(str(xls), ignore_workbook_corruption=True)
		except Exception:
			continue
		sheet = book.sheet_by_index(0)
		for row in range(6, sheet.nrows):
			if not sheet.cell_value(row, 0):
				continue
			try:
				move_date = xlrd.xldate.xldate_as_datetime(
					float(sheet.cell_value(row, 0)), book.datemode
				).date()
			except (ValueError, TypeError):
				move_date = None
			movements.append(
				{
					"date": move_date,
					"account": str(sheet.cell_value(row, 1)).strip(),
					"description": str(sheet.cell_value(row, 2)).strip(),
					"party": str(sheet.cell_value(row, 3)).strip(),
					"in": to_float(str(sheet.cell_value(row, 4) or 0)),
					"out": to_float(str(sheet.cell_value(row, 5) or 0)),
					"year": xls.parent.parent.name if xls.parent.parent.name.isdigit() else "",
					"matched": False,
				}
			)
	return movements


def apply_payment_states(documents: list[dict[str, Any]], movements: list[dict[str, Any]]) -> tuple[int, int]:
	"""Match primanota rows to documents by source number.

	Incassi read "Fattura n. <numero>/<anno>", pagamenti "Acquisto n.
	<numero> - <controparte>". A document with at least one movement is
	marked paid; partial payments still settle here, the residue belongs to
	the migration clearing by design.
	"""
	out_index: dict[str, dict] = {}
	in_index: dict[str, list[dict]] = defaultdict(list)
	for doc in documents:
		year = doc["invoice_date"][:4]
		if doc["move_type"].startswith("out_"):
			out_index[f"{doc['_numero']}/{year}".lower()] = doc
		else:
			in_index[doc["_numero"].lower()].append(doc)

	def lookup_purchase(key: str, party: str, year: str):
		candidates = in_index.get(key)
		if candidates is None and key.endswith(f"/{year}"):
			candidates = in_index.get(key.rsplit("/", 1)[0])
		if not candidates:
			return None
		if len(candidates) == 1:
			return candidates[0]
		# supplier numbers are only unique per supplier: on a clash the
		# counterparty column decides, no counterparty means no match
		party_key = normalize_party(party)
		for doc in candidates:
			if normalize_party(doc["partner_name"]) == party_key:
				return doc
		return None

	matched_out = matched_in = 0
	for move in movements:
		if move["account"].lower() in PRIMANOTA_SKIP_ACCOUNTS:
			move["matched"] = True  # VAT legs of self billed docs, not supplier payments
			continue
		desc = move["description"]
		sale = re.match(r"Fattura n\. (.+?)(?: - .*)?$", desc)
		if sale and move["in"]:
			doc = out_index.get(sale.group(1).strip().lower())
			if doc:
				doc["payment_state"] = "paid"
				move["matched"] = True
				matched_out += 1
			continue
		purchase = re.match(r"Acquisto n\. (.+?)(?: - .*)?$", desc)
		if purchase and move["out"]:
			tail = desc[len("Acquisto n. "):]
			# the supplier number can itself contain " - ": try every split
			# point from the shortest prefix to the whole tail
			doc = None
			pieces = tail.split(" - ")
			for end in range(1, len(pieces) + 1):
				doc = lookup_purchase(" - ".join(pieces[:end]).strip().lower(), move["party"], move["year"])
				if doc:
					break
			if doc:
				doc["payment_state"] = "paid"
				move["matched"] = True
				matched_in += 1
	return matched_out, matched_in


def normalize_party(name: str) -> str:
	return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:20]


def match_payments_by_party_amount(
	documents: list[dict[str, Any]],
	movements: list[dict[str, Any]],
) -> int:
	"""Second pass for payments the number match cannot see.

	Card charges for foreign suppliers carry the supplier invoice number
	while the self billed integration carries ours, so those documents
	never match by number. Pair the leftover outflows with unpaid purchase
	documents of the same counterparty and the same amount, closest
	invoice date first, one movement per document.
	"""
	from datetime import date

	by_party: dict[str, list] = defaultdict(list)
	for doc in documents:
		# invoices only: a refund settles as an inflow or a compensation,
		# never as an outflow
		if doc["move_type"] == "in_invoice" and doc["payment_state"] == "not_paid":
			by_party[normalize_party(doc["partner_name"])].append(doc)

	matched = 0
	for move in movements:
		if move["matched"] or not move["out"]:
			continue
		best = None
		for doc in by_party.get(normalize_party(move["party"]), []):
			if doc["payment_state"] == "paid":
				continue
			if abs(abs(doc["total_amount"]) - move["out"]) > max(0.05, 0.02 * move["out"]):
				continue
			gap = 9999
			if move.get("date"):
				try:
					doc_date = date.fromisoformat(doc["invoice_date"])
					gap = abs((move["date"] - doc_date).days)
				except ValueError:
					pass
			if best is None or gap < best[0]:
				best = (gap, doc)
		if best:
			best[1]["payment_state"] = "paid"
			move["matched"] = True
			matched += 1
	return matched


def build_unmatched_purchase_aggregates(
	movements: list[dict[str, Any]],
	documents: list[dict[str, Any]],
	company: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	"""Fold paid outflows with no electronic document into one purchase
	invoice per year, so the historical balance closes without inventing
	line level detail. No VAT is claimed on the aggregate."""
	unpaid_parties = {
		normalize_party(d["partner_name"])
		for d in documents
		if d["move_type"].startswith("in_") and d["payment_state"] == "not_paid"
	}
	per_year: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
	per_account: dict[str, float] = defaultdict(float)
	suspect_double_count: dict[str, float] = defaultdict(float)
	unmatched_in_total = 0.0
	dropped_no_year = 0
	for move in movements:
		if move["matched"]:
			continue
		if not move["year"]:
			dropped_no_year += 1
			continue
		if move["in"]:
			unmatched_in_total += move["in"]
			continue
		if move["out"]:
			party = move["party"] or "Controparte non indicata"
			per_year[move["year"]][party] += move["out"]
			per_account[move["account"]] += move["out"]
			if normalize_party(party) in unpaid_parties:
				# an unpaid electronic document of the same counterparty
				# exists: aggregating this outflow may double count it
				suspect_double_count[party] += move["out"]

	aggregates = []
	report_rows = {}
	for year, parties in sorted(per_year.items()):
		total = round(sum(parties.values()), 2)
		if not total:
			continue
		lines = [
			{
				"description": f"Spese non elettroniche {year}: {party}",
				"quantity": 1.0,
				"unit_price": round(amount, 2),
				"discount": 0.0,
				"subtotal": round(amount, 2),
				"total": round(amount, 2),
				"tax_names": ["0% N2.2"],
			}
			for party, amount in sorted(parties.items(), key=lambda kv: -kv[1])
		]
		aggregates.append(
			{
				"company": company,
				"currency": "EUR",
				"move_type": "in_invoice",
				"state": "posted",
				"invoice_date": f"{year}-12-31",
				"due_date": f"{year}-12-31",
				"odoo_id": int(year) * 100 + 99,
				"odoo_number": f"FIC-ACQ/{year}/AGGR",
				"reference": f"Aggregato documenti non elettronici {year}",
				"payment_state": "paid",
				"partner_id": 999999,
				"partner_name": "FIC documenti non elettronici",
				"partner_vat": "",
				"partner_street": "",
				"partner_city": "",
				"partner_zip": "",
				# no address data at all: a synthetic party with a country but
				# no city fails the address mandatory fields
				"partner_country": "",
				"partner_email": "",
				"partner_phone": "",
				"total_amount": -total,
				"untaxed_amount": -total,
				"tax_amount": 0.0,
				"lines": lines,
			}
		)
		report_rows[year] = total
	return aggregates, {
		"aggregated_purchases_per_year": report_rows,
		"aggregated_purchases_per_account": {k: round(v, 2) for k, v in sorted(per_account.items())},
		"suspect_double_count_parties": {k: round(v, 2) for k, v in sorted(suspect_double_count.items())},
		"movements_dropped_no_year": dropped_no_year,
		# inflows with no matching document are reported only, never booked
		"unmatched_incassi_total": round(unmatched_in_total, 2),
	}


COUNTRY_NAMES = {
	"IT": "Italy",
	"US": "United States",
	"IE": "Ireland",
	"NL": "Netherlands",
	"DE": "Germany",
	"FR": "France",
	"GB": "United Kingdom",
	"LU": "Luxembourg",
	"ES": "Spain",
	"MC": "Monaco",
	"CH": "Switzerland",
	"BE": "Belgium",
	"SE": "Sweden",
	"AT": "Austria",
	"CZ": "Czech Republic",
	"PL": "Poland",
	"MT": "Malta",
	"CY": "Cyprus",
	"EE": "Estonia",
	"LT": "Lithuania",
	"SG": "Singapore",
	"AU": "Australia",
	"CA": "Canada",
	"IN": "India",
	"IL": "Israel",
	"JP": "Japan",
	"HK": "Hong Kong",
	"AE": "United Arab Emirates",
}


def country_name_from_code(code: str) -> str:
	code = (code or "IT").upper()
	if code in COUNTRY_NAMES:
		return COUNTRY_NAMES[code]
	try:
		import frappe

		name = frappe.db.get_value("Country", {"code": code.lower()}, "name")
	except Exception:
		name = None
	if name:
		COUNTRY_NAMES[code] = name
		return name
	return "Italy"

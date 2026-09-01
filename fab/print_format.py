"""Config-as-code for the FABRICATORS sales invoice print format."""

import os

import frappe

PRINT_FORMAT = "Fattura FABRICATORS"
TEMPLATE = os.path.join(os.path.dirname(__file__), "print_formats", "sales_invoice_registro.html")

# Company contact facts printed in the invoice footer; only filled when empty so
# a deliberate change on the site is never overwritten.
COMPANY_CONTACTS = {
	"email": "billing@fabricators.ltd",
	"phone_no": "+39 030 5357411",
	"website": "fabricators.ltd",
}
BANK_BIC = {"BANCA SELLA SPA": "SELBIT2BXXX"}


def ensure_all():
	ensure_invoice_line_fields()
	ensure_company_billing_data()
	ensure_sales_invoice_print_format()


def ensure_invoice_line_fields():
	"""Free lines around an invoice item, the way Odoo has section and note
	lines: a section heading printed as a band above the item and a note
	printed under it. ERPNext has no item-less rows, so they hang on the item."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Sales Invoice Item": [
				{
					"fieldname": "fab_print_section",
					"fieldtype": "Section Break",
					"label": "Print lines",
					"insert_after": "description",
					"collapsible": 1,
				},
				{
					"fieldname": "fab_section",
					"fieldtype": "Data",
					"label": "Section heading",
					"insert_after": "fab_print_section",
					"description": "Printed as a band above this line.",
				},
				{
					"fieldname": "fab_print_column",
					"fieldtype": "Column Break",
					"insert_after": "fab_section",
				},
				{
					"fieldname": "fab_note",
					"fieldtype": "Small Text",
					"label": "Note",
					"insert_after": "fab_print_column",
					"description": "Printed under this line.",
				},
			]
		},
		ignore_validate=True,
		update=True,
	)


def ensure_company_billing_data():
	for company in frappe.get_all("Company", fields=["name", "email", "phone_no", "website"]):
		values = {k: v for k, v in COMPANY_CONTACTS.items() if not company.get(k)}
		if values:
			frappe.db.set_value("Company", company.name, values, update_modified=False)
	for bank, bic in BANK_BIC.items():
		if frappe.db.exists("Bank", bank) and not frappe.db.get_value("Bank", bank, "swift_number"):
			frappe.db.set_value("Bank", bank, "swift_number", bic, update_modified=False)


def ensure_sales_invoice_print_format():
	"""Create or refresh the print format from the template file and make it the
	Sales Invoice default. Rendered by the Chrome PDF generator."""
	with open(TEMPLATE, encoding="utf-8") as f:
		html = f.read()
	values = {
		"doc_type": "Sales Invoice",
		"module": "FAB",
		"standard": "No",
		"custom_format": 1,
		"print_format_type": "Jinja",
		"disabled": 0,
		"default_print_language": "it",
		"pdf_generator": "chrome",
		"html": html,
	}
	if frappe.db.exists("Print Format", PRINT_FORMAT):
		doc = frappe.get_doc("Print Format", PRINT_FORMAT)
		if any(doc.get(k) != v for k, v in values.items()):
			doc.update(values)
			doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Print Format", "name": PRINT_FORMAT, **values})
		doc.insert(ignore_permissions=True)
	frappe.make_property_setter(
		{
			"doctype": "Sales Invoice",
			"doctype_or_field": "DocType",
			"property": "default_print_format",
			"value": PRINT_FORMAT,
			"property_type": "Data",
		},
		validate_fields_for_doctype=False,
	)

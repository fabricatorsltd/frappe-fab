"""Config-as-code for the FABRICATORS sales invoice print format."""

import os

import frappe

PRINT_FORMAT = "Fattura FABRICATORS"
PDF_GENERATOR = "chrome_pdfa"
ICC_DIRS = ("/usr/share/color/icc/ghostscript", "/usr/share/ghostscript/iccprofiles", "/usr/share/color/icc")
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
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	ensure_invoice_line_fields()
	ensure_company_billing_data()
	ensure_system_fonts()
	ensure_sales_invoice_print_format()


def ensure_system_fonts():
	"""The Chrome PDF generator only renders a font reliably when it is
	installed on the host (web fonts are not awaited before printing), so the
	bundled Montserrat files are copied into the user font directory."""
	import shutil
	import subprocess

	src = os.path.join(os.path.dirname(__file__), "public", "fonts")
	dst = os.path.expanduser("~/.local/share/fonts")
	changed = False
	for name in os.listdir(src):
		if not name.endswith(".ttf"):
			continue
		target = os.path.join(dst, name)
		if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(os.path.join(src, name)):
			os.makedirs(dst, exist_ok=True)
			shutil.copy2(os.path.join(src, name), target)
			changed = True
	if changed and shutil.which("fc-cache"):
		subprocess.run(["fc-cache", "-f"], check=False, capture_output=True)


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
	for company in frappe.get_all(
		"Company", filters={"company_name": ["like", "FABRICATORS%"]}, fields=["name", "email", "phone_no", "website"]
	):
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
		"pdf_generator": PDF_GENERATOR,
		"html": html,
	}
	frappe.make_property_setter(
		{
			"doctype": "Print Format",
			"doctype_or_field": "DocField",
			"fieldname": "pdf_generator",
			"property": "options",
			"value": "wkhtmltopdf\nchrome\n" + PDF_GENERATOR,
			"property_type": "Text",
		},
		validate_fields_for_doctype=False,
	)
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


def set_pdf_generator():
	"""before_request: download_pdf falls back to wkhtmltopdf when the request
	carries no pdf_generator, ignoring the print format's own setting. Fill it
	in from the format (or the doctype default) so PDF downloads match Print."""
	form = frappe.local.form_dict
	# before_request runs before the API layer sets form_dict.cmd: read the path
	path = getattr(frappe.local, "request", None) and frappe.local.request.path or ""
	method = form.get("cmd") or path.rsplit("/", 1)[-1]
	if method != "frappe.utils.print_format.download_pdf" or form.get("pdf_generator"):
		return
	print_format = form.get("format")
	if not print_format and form.get("doctype"):
		print_format = frappe.get_meta(form.doctype).default_print_format
	if not print_format or not frappe.db.exists("Print Format", print_format):
		return
	generator = frappe.get_cached_value("Print Format", print_format, "pdf_generator")
	if generator:
		form.pdf_generator = generator


def get_pdf(print_format, html, options, output, pdf_generator=None):
	"""pdf_generator hook: "chrome_pdfa" renders with Frappe's Chrome generator
	and then closes the file as PDF/A-2b with Ghostscript, carrying the title
	and author the format declares in <meta name="pdf-title|pdf-author">.
	Combined outputs (several documents in one writer) keep the plain PDF."""
	if pdf_generator != PDF_GENERATOR:
		return None
	from frappe.utils.pdf import get_chrome_pdf

	# workers under supervisor start without HOME and fontconfig then skips the
	# user font directory where ensure_system_fonts() put Montserrat
	os.environ.setdefault("HOME", os.path.expanduser("~"))
	pdf = get_chrome_pdf(print_format, html, options, output, pdf_generator="chrome")
	if output is not None or not isinstance(pdf, bytes):
		return pdf
	title = _meta(html, "pdf-title")
	author = _meta(html, "pdf-author")
	try:
		return to_pdfa(pdf, title=title, author=author)
	except Exception:
		frappe.log_error("PDF/A conversion failed, plain PDF returned", "fab.print_format.get_pdf")
		return pdf


def _meta(html, name):
	import html as html_module
	import re

	m = re.search(r'<meta\s+name="%s"\s+content="([^"]*)"' % re.escape(name), html)
	return html_module.unescape(m.group(1)) if m else ""


def _ps_string(value):
	return "(" + (value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def to_pdfa(pdf, title="", author=""):
	"""Rewrite a PDF as PDF/A-2b (sRGB output intent, XMP, embedded fonts)."""
	import shutil
	import subprocess
	import tempfile

	gs = shutil.which("gs")
	if not gs:
		raise RuntimeError("ghostscript is not installed")
	icc = next(
		(os.path.join(d, "srgb.icc") for d in ICC_DIRS if os.path.exists(os.path.join(d, "srgb.icc"))), None
	)
	if not icc:
		raise RuntimeError("sRGB ICC profile not found")
	with tempfile.TemporaryDirectory() as tmp:
		src = os.path.join(tmp, "in.pdf")
		dst = os.path.join(tmp, "out.pdf")
		defs = os.path.join(tmp, "pdfa.ps")
		with open(src, "wb") as f:
			f.write(pdf)
		with open(defs, "w") as f:
			f.write(
				"%!\n"
				"[ /Title " + _ps_string(title) + " /Author " + _ps_string(author) + " /Creator (Frappe) /DOCINFO pdfmark\n"
				"[/_objdef {icc_PDFA} /type /stream /OBJ pdfmark\n"
				"[{icc_PDFA} << /N 3 >> /PUT pdfmark\n"
				"[{icc_PDFA} " + _ps_string(icc) + " (r) file /PUT pdfmark\n"
				"[/_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark\n"
				"[{OutputIntent_PDFA} << /Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {icc_PDFA}"
				" /OutputConditionIdentifier (sRGB IEC61966-2.1) /Info (sRGB IEC61966-2.1) >> /PUT pdfmark\n"
				"[{Catalog} << /OutputIntents [ {OutputIntent_PDFA} ] >> /PUT pdfmark\n"
			)
		cmd = [
			gs, "-q", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dPDFA=2", "-dPDFACompatibilityPolicy=1",
			"--permit-file-read=" + os.path.dirname(icc) + "/", "-sDEVICE=pdfwrite",
			"-sProcessColorModel=DeviceRGB", "-sColorConversionStrategy=RGB", "-dEmbedAllFonts=true",
			"-sOutputFile=" + dst, defs, src,
		]
		run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
		if run.returncode != 0 or not os.path.exists(dst):
			raise RuntimeError("ghostscript failed: " + (run.stderr or run.stdout)[-800:])
		with open(dst, "rb") as f:
			return f.read()

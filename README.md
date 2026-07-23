# fab

Shared fabricators workspace shell and common Desk integration for Frappe and ERPNext.

## Scope

`fab` is the thin app that gives the fabricators suite a consistent home inside Desk. It
owns the shared workspace container, common branding, and the install-time
synchronization that keeps fab apps grouped together in the UI.

Current responsibilities include:

- the top-level fab desktop and sidebar entry points
- shared labels, translations, and visual assets
- install hooks that keep the fab app group consistent across sites
- migration tooling for moving accounting history from Odoo into ERPNext

## Odoo migration tooling

Two modules cover the migration path from Odoo:

- `fab/odoo_import.py` exports customer and supplier invoices (refunds
  included) from an Odoo database over XML-RPC into a self-contained JSON
  bundle. Nothing is written to Odoo.
- `fab/odoo_erpnext_import.py` replays a bundle into ERPNext: parties,
  addresses, items, invoices with their source numbering, taxes booked as
  actual amounts on the configured VAT accounts, and settlement journal
  entries against a dedicated clearing account for documents Odoo reports
  as paid or in payment.

The import is idempotent: existing documents are reused, so a partial or
repaired run can be replayed without duplicates. The Odoo header totals are
treated as the booked truth; per line rounding gaps are folded into the
document discount and the first tax row so documents land on their source
totals, and anything beyond the fold cap shows up in the verify step. The
importer also switches off ERPNext price automation
(`auto_insert_price_list_rate_if_missing`, last purchase rate) for the
site, since both refill zero rates on imported note lines.

Operational entry points, callable through `bench execute`:

- `import_odoo_invoice_bundle(bundle_path)` runs or resumes an import
- `verify_import_against_bundle(bundle_path)` compares every imported
  document against the bundle headers and reports drifts
- `rebuild_mismatched_documents(bundle_path)` drops drifted documents,
  settlement entries included, and lets the import recreate them

Requires company VAT accounts configured by `fab_italy_tax`
(`fab_itx_vat_output_account`, `fab_itx_vat_input_account`) and accounts of
type Tax for the VAT chain to aggregate correctly.

## Branches

- `develop`: integration branch for testing against Frappe/ERPNext `develop`
- `version-16`: stable branch for Frappe/ERPNext 16

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab.git --branch version-16
bench --site [site] install-app fab
```

Use `--branch develop` when testing the fab app set against Frappe `develop`.

## Contributing

Follow the official Frappe contribution guidelines:

- <https://github.com/frappe/erpnext/wiki/Contribution-Guidelines>

In particular, align proposals, coding standards, pull request hygiene, and
documentation updates with the upstream Frappe process.

## Development

```bash
cd apps/fab
pre-commit install
```

Pre-commit is configured for Ruff, ESLint, Prettier, and PyUpgrade.

## License

GNU Affero General Public License v3.0

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

from __future__ import annotations

import json

import frappe

# The record name has to match the label exactly: get_desktop_icons() collects the
# permitted parents by label but filters the children by parent_icon, which stores
# the linked record's name. A child pointing at "FAB" would be dropped from a set
# holding "fab", so the container would render without any of its apps.
FAB_PARENT_ICON = "fab"
FAB_PARENT_LABEL = "fab"
# Left empty on purpose. desktop_icon.html only tags the container with the
# "folder-icon" class in the branch that runs when logo_url and icon_image are
# both unset, and desktop.css hangs the folder background and the thumbnail grid
# off that class. Setting a logo takes the earlier branch, so the container never
# gets the class, render_folder_thumbnail() clears the logo it just rendered, and
# the tile ends up blank. ERPNext's own folders leave this empty for the same reason.
FAB_PARENT_LOGO = None
FAB_MODULE = "FAB"
FAB_CHILD_WORKSPACES = (
	{
		"app": "fab_banks_import",
		"icon": "bank",
		"old_name": "FAB Banks Import",
		"new_name": "Banks Import",
	},
	{
		"app": "fab_exchange_rate_sync",
		"icon": "change",
		"old_name": "Fab Exchange Rate Sync",
		"new_name": "Exchange Rate Sync",
	},
	{
		"app": "fab_italy_edi",
		"icon": "file",
		"old_name": "FAB Italy E-Invoicing",
		"new_name": "Italy E-Invoicing",
	},
	{
		"app": "fab_italy_tax",
		"icon": "balance-scale",
		"old_name": "Fab Italy Tax",
		"new_name": "Italy Tax",
	},
	{
		"app": "fab_openapi",
		"icon": "network",
		"old_name": "FAB OpenAPI",
		"new_name": "OpenAPI",
	},
	{
		"app": "fab_f24",
		"icon": "file",
		"old_name": "F24",
		"new_name": "F24",
	},
	{
		"app": "fab_hr_italy",
		"icon": "users",
		"old_name": "HR Italy",
		"new_name": "HR Italy",
	},
	{
		"app": "fab_jethr_import",
		"icon": "upload",
		"old_name": "Jet HR Import",
		"new_name": "Jet HR Import",
	},
	{
		"app": "fab_msp",
		"icon": "tool",
		"old_name": "MSP",
		"new_name": "MSP",
	},
	{
		"app": "fab_cashflow",
		"icon": "money",
		"old_name": "Cashflow",
		"new_name": "Cashflow",
	},
)


def after_install():
	sync_fab_desktop_group()


def after_migrate():
	sync_fab_desktop_group()


def after_app_install(app_name: str):
	# every install reruns frappe's desktop icon creation, which recreates child
	# icons from their workspace with the child app on them, so renormalize
	sync_fab_desktop_group()


def sync_fab_desktop_group():
	installed_apps = set(frappe.get_installed_apps())
	ensure_fab_parent_icon()

	for workspace in FAB_CHILD_WORKSPACES:
		if workspace["app"] not in installed_apps:
			continue

		rename_standard_doc("Desktop Icon", workspace["old_name"], workspace["new_name"])
		rename_standard_doc("Workspace", workspace["old_name"], workspace["new_name"])
		sync_workspace(workspace["old_name"], workspace["new_name"])
		sync_workspace_sidebar(
			old_name=workspace["old_name"],
			new_name=workspace["new_name"],
			icon_name=workspace["icon"],
		)
		sync_desktop_icon(
			icon_name=workspace["icon"],
			old_name=workspace["old_name"],
			new_name=workspace["new_name"],
		)


def normalize_fab_parent_icon_case():
	"""Lowercase the container icon and the links pointing at it.

	Sites installed before the rename still hold a "FAB" record with "FAB" in
	every child's parent_icon. The database collation is case insensitive, so the
	links keep resolving and nothing looks broken, but get_desktop_icons() matches
	those values against the parent label in Python and quietly drops every child.
	"""
	renamed = frappe.db.sql(
		"""UPDATE `tabDesktop Icon` SET name = %s WHERE name = %s AND BINARY name != %s""",
		(FAB_PARENT_ICON, FAB_PARENT_ICON, FAB_PARENT_ICON),
	)

	relinked = frappe.db.sql(
		"""UPDATE `tabDesktop Icon` SET parent_icon = %s
		WHERE parent_icon = %s AND BINARY parent_icon != %s""",
		(FAB_PARENT_ICON, FAB_PARENT_ICON, FAB_PARENT_ICON),
	)

	if renamed or relinked:
		frappe.clear_cache()


def ensure_fab_parent_icon():
	normalize_fab_parent_icon_case()

	icon = get_or_create_doc(
		"Desktop Icon",
		FAB_PARENT_ICON,
		{
			"doctype": "Desktop Icon",
			"label": FAB_PARENT_LABEL,
			"name": FAB_PARENT_ICON,
		},
	)

	update_doc(
		icon,
		{
			"app": "fab",
			"icon": None,
			"icon_type": "Folder",
			"idx": 0,
			"label": FAB_PARENT_LABEL,
			"link": None,
			"link_to": "",
			"link_type": "Workspace Sidebar",
			"logo_url": FAB_PARENT_LOGO,
			"standard": 1,
		},
	)

	if frappe.db.get_value("Desktop Icon", icon.name, "label") != FAB_PARENT_LABEL:
		frappe.db.set_value("Desktop Icon", icon.name, "label", FAB_PARENT_LABEL, update_modified=False)
		frappe.clear_cache()


def rename_standard_doc(doctype: str, old_name: str, new_name: str):
	if old_name == new_name or not frappe.db.exists(doctype, old_name):
		return

	if frappe.db.exists(doctype, new_name):
		frappe.delete_doc(doctype, old_name, ignore_permissions=True)
		return

	frappe.rename_doc(doctype, old_name, new_name, force=True)


def sync_workspace(old_name: str, new_name: str):
	if not frappe.db.exists("Workspace", new_name):
		return

	workspace = frappe.get_doc("Workspace", new_name)
	changed = False

	for fieldname in ("label", "title"):
		if workspace.get(fieldname) != new_name:
			workspace.set(fieldname, new_name)
			changed = True

	content = workspace.content or ""
	if content:
		updated_content = content.replace(old_name, new_name)
		if updated_content != content:
			try:
				json.loads(updated_content)
			except json.JSONDecodeError:
				pass
			else:
				workspace.content = updated_content
				changed = True

	if changed:
		workspace.save(ignore_permissions=True)


def sync_workspace_sidebar(old_name: str, new_name: str, icon_name: str):
	delete_stale_doc("Workspace Sidebar", old_name, new_name)
	if not frappe.db.exists("Workspace", new_name):
		return

	workspace = frappe.get_doc("Workspace", new_name)
	sidebar = get_or_create_doc(
		"Workspace Sidebar",
		new_name,
		{
			"doctype": "Workspace Sidebar",
			"name": new_name,
			"title": new_name,
		},
	)

	update_doc(
		sidebar,
		{
			"app": "fab",
			"for_user": "",
			"header_icon": icon_name,
			"items": build_workspace_sidebar_items(workspace),
			"module": FAB_MODULE,
			"standard": 1,
			"title": new_name,
		},
	)


def build_workspace_sidebar_items(workspace) -> list[dict[str, object]]:
	items = [make_workspace_home_item(workspace.name)]
	shortcut_label_map = {
		shortcut.link_to: shortcut.label
		for shortcut in (workspace.shortcuts or [])
		if getattr(shortcut, "link_to", None) and getattr(shortcut, "label", None)
	}
	link_items = [link for link in (workspace.links or []) if not getattr(link, "hidden", 0)]
	current_section = None

	for link in link_items:
		if link.type == "Card Break":
			current_section = link.label
			items.append(make_section_break_item(link.label))
		elif link.type == "Link" and getattr(link, "link_to", None) and getattr(link, "link_type", None):
			items.append(
				make_sidebar_link_item(
					label=shortcut_label_map.get(link.link_to, link.label),
					link_to=link.link_to,
					link_type=link.link_type,
					child=1 if current_section else 0,
					doc_view="List" if link.link_type == "DocType" else None,
				)
			)

	if len(items) > 1:
		return items

	for shortcut in workspace.shortcuts or []:
		if not getattr(shortcut, "link_to", None) or not getattr(shortcut, "label", None):
			continue

		items.append(
			make_sidebar_link_item(
				label=shortcut.label,
				link_to=shortcut.link_to,
				link_type=shortcut.type,
				child=0,
				doc_view=getattr(shortcut, "doc_view", None),
				filters=getattr(shortcut, "stats_filter", None),
			)
		)

	return items


def make_workspace_home_item(workspace_name: str) -> dict[str, object]:
	return {
		"child": 0,
		"collapsible": 1,
		"icon": "home",
		"indent": 0,
		"keep_closed": 0,
		"label": "Home",
		"link_to": workspace_name,
		"link_type": "Workspace",
		"show_arrow": 0,
		"type": "Link",
	}


def make_section_break_item(label: str) -> dict[str, object]:
	return {
		"child": 0,
		"collapsible": 1,
		"indent": 1,
		"keep_closed": 0,
		"label": label,
		"show_arrow": 0,
		"type": "Section Break",
	}


def make_sidebar_link_item(
	label: str,
	link_to: str,
	link_type: str,
	child: int,
	doc_view: str | None = None,
	filters: str | None = None,
) -> dict[str, object]:
	item = {
		"child": child,
		"collapsible": 1,
		"indent": 0,
		"keep_closed": 0,
		"label": label,
		"link_to": link_to,
		"link_type": link_type,
		"show_arrow": 0,
		"type": "Link",
	}

	if doc_view:
		item["doc_view"] = doc_view
	if filters and filters != "[]":
		item["filters"] = filters

	return item


def delete_stale_doc(doctype: str, old_name: str, new_name: str):
	if old_name == new_name:
		return

	if frappe.db.exists(doctype, old_name) and frappe.db.exists(doctype, new_name):
		frappe.delete_doc(doctype, old_name, ignore_permissions=True)


def sync_desktop_icon(icon_name: str, old_name: str, new_name: str):
	if not frappe.db.exists("Desktop Icon", new_name) and frappe.db.exists("Desktop Icon", old_name):
		rename_standard_doc("Desktop Icon", old_name, new_name)

	icon = get_or_create_doc(
		"Desktop Icon",
		new_name,
		{
			"doctype": "Desktop Icon",
			"label": new_name,
			"name": new_name,
		},
	)

	update_doc(
		icon,
		{
			# the container owns the child icons: the sidebar menu filters them by
			# the current app, which resolves to fab, and the artwork lookup builds
			# its path from this field, and the files ship with fab
			"app": "fab",
			"icon": icon_name,
			"icon_type": "Link",
			"label": new_name,
			"link": None,
			"link_to": new_name,
			"link_type": "Workspace Sidebar",
			"parent_icon": FAB_PARENT_ICON,
			"standard": 1,
		},
	)


def get_or_create_doc(doctype: str, name: str, payload: dict[str, object]):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)

	return frappe.get_doc(payload)


def update_doc(doc, values: dict[str, object]):
	changed = doc.is_new()
	for fieldname, value in values.items():
		if doc.get(fieldname) != value:
			doc.set(fieldname, value)
			changed = True

	if not changed:
		return

	if doc.is_new() or not frappe.db.exists(doc.doctype, doc.name):
		doc.insert(ignore_permissions=True)
		return

	doc.save(ignore_permissions=True)

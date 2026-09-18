// Optional quotation lines, Odoo style: quoted with their price, left out of the
// totals, and picked one by one when the quotation becomes an order. ERPNext only
// knows alternative rows, so the two client behaviours are widened here instead of
// in a fork of erpnext.
(() => {
	const controller = erpnext.selling.QuotationController;
	if (!controller || controller.prototype.fab_optional_items) return;
	controller.prototype.fab_optional_items = true;

	const standard_make_sales_order = controller.prototype.make_sales_order;

	const is_optional = (item) => Boolean(item.fab_is_optional);
	const in_a_set = (item) => Boolean(item.is_alternative || item.has_alternative_item);

	// erpnext.taxes_and_totals drops the alternative rows before it sums the items,
	// so the optional rows leave the on-screen totals the same way the server leaves
	// them out of net_total, the taxes and the grand total.
	controller.prototype.filtered_items = function () {
		return this.frm.doc.items.filter((item) => !item.is_alternative && !is_optional(item));
	};

	// script_manager triggers the controller method named after the grid field: ticking
	// Optional moves the row out of the totals, so the form has to add up again
	controller.prototype.fab_is_optional = function () {
		this.calculate_taxes_and_totals();
	};

	controller.prototype.make_sales_order = function () {
		if (!(this.frm.doc.items || []).some(is_optional)) {
			return standard_make_sales_order.call(this);
		}
		this.fab_show_optional_items_dialog();
	};

	// Same contract as the standard alternative dialog: the ticked rows travel as
	// selected_items and the server maps those, plus every plain row.
	controller.prototype.fab_show_optional_items_dialog = function () {
		const frm = this.frm;
		const rows = frm.doc.items
			.filter((item) => in_a_set(item) || is_optional(item))
			.map((item) => ({
				name: item.name,
				item_code: item.item_code,
				description: item.description,
				amount: item.amount,
				is_alternative: item.is_alternative,
				fab_is_optional: item.fab_is_optional,
			}));

		const fields = [
			{ fieldtype: "Data", fieldname: "name", label: __("Name"), read_only: 1 },
			{
				fieldtype: "Link",
				fieldname: "item_code",
				options: "Item",
				label: __("Item Code"),
				read_only: 1,
				in_list_view: 1,
				columns: 2,
				formatter: (value, df, options, doc) => {
					if (doc.fab_is_optional) return `<span class="indicator blue">${value}</span>`;
					return doc.is_alternative ? `<span class="indicator yellow">${value}</span>` : value;
				},
			},
			{
				fieldtype: "Text Editor",
				fieldname: "description",
				label: __("Description"),
				in_list_view: 1,
				read_only: 1,
			},
			{
				fieldtype: "Currency",
				fieldname: "amount",
				label: __("Amount"),
				options: "currency",
				in_list_view: 1,
				read_only: 1,
			},
			{ fieldtype: "Check", fieldname: "is_alternative", label: __("Is Alternative"), read_only: 1 },
			{ fieldtype: "Check", fieldname: "fab_is_optional", label: __("Optional"), read_only: 1 },
		];

		const dialog = new frappe.ui.Dialog({
			title: __("Select Items for Sales Order"),
			fields: [
				{ fieldname: "info", fieldtype: "HTML", read_only: 1 },
				{
					fieldname: "selectable_items",
					fieldtype: "Table",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					in_place_edit: true,
					data: rows,
					description: __(
						"Tick one item from each alternatives set and every optional item the customer ordered."
					),
					get_data: () => rows,
					fields: fields,
				},
			],
			primary_action: () => {
				frappe.model.open_mapped_doc({
					method: "erpnext.selling.doctype.quotation.quotation.make_sales_order",
					frm: frm,
					args: {
						selected_items: dialog.fields_dict.selectable_items.grid.get_selected_children(),
					},
				});
				dialog.hide();
			},
			primary_action_label: __("Continue"),
		});

		dialog.fields_dict.info.$wrapper.html(
			`<p class="small text-muted">
				<span class="indicator yellow"></span> ${__("Alternative Items")}
				<span class="indicator blue" style="margin-left: 12px"></span> ${__("Optional Items")}
			</p>`
		);
		dialog.show();
	};
})();

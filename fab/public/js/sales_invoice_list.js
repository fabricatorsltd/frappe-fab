// returns live under the Credit Note entry: keep them out of the invoice
// list unless the operator filters for them explicitly
(() => {
	const settings = (frappe.listview_settings["Sales Invoice"] ??= {});
	const previous_onload = settings.onload;
	settings.onload = function (listview) {
		previous_onload?.(listview);
		const filters = listview.filter_area.get();
		if (!filters.some((f) => f[1] === "is_return")) {
			listview.filter_area.add([["Sales Invoice", "is_return", "=", 0]]);
		}
	};
})();

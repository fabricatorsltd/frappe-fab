// The user is not told about an impersonation any more (see
// fab.overrides.user.impersonate): the dialog says where the reason goes.
frappe.ui.form.on("User", {
	setup_impersonation(frm) {
		if (!frm.custom_buttons[__("Impersonate")] || frm.doc.restrict_ip) return;
		frm.remove_custom_button(__("Impersonate"));
		frm.add_custom_button(__("Impersonate"), () => {
			frappe.prompt(
				[
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Reason for impersonating"),
						description: __("Kept in the Activity Log. The user is not notified."),
						reqd: 1,
					},
				],
				(values) =>
					frappe
						.xcall("frappe.core.doctype.user.user.impersonate", {
							user: frm.doc.name,
							reason: values.reason,
						})
						.then(() => window.location.reload()),
				__("Impersonate as {0}", [frm.doc.name]),
				__("Confirm")
			);
		});
	},
});

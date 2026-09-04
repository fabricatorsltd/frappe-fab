// The email composer fetches the signature before it knows the sender, so it
// always inserts the default outgoing account's signature (support) even when
// the "From" field is already preselected on another account (billing). Feed it
// the selected sender so the signature matches the account the mail leaves from.
(() => {
	const composer = frappe.views.CommunicationComposer?.prototype;
	if (!composer) return;
	const set_content = composer.set_content;
	composer.set_content = function (sender_email) {
		const sender = sender_email || this.dialog?.get_value("sender") || "";
		return set_content.call(this, sender);
	};
})();

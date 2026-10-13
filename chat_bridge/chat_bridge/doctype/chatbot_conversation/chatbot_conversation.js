// Copyright (c) 2026, saad and contributors
// For license information, please see license.txt

frappe.ui.form.on("Chatbot Conversation", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.message_count) return;

		frm.add_custom_button(frm.doc.summary ? __("Regenerate Summary") : __("Summarize"), () =>
			frm
				.call({
					method: "generate_summary",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Summarizing conversation..."),
				})
				.then(() => frm.reload_doc())
				.then(() => frm.scroll_to_field("summary")),
		);
	},
});

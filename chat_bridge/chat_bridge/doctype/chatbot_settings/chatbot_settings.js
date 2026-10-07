// Copyright (c) 2026, saad and contributors
// For license information, please see license.txt

const DEFAULT_MODELS = {
	Anthropic: "claude-opus-5-5",
};

// [model, dimensions] suggested for each embedding provider.
const EMBEDDING_DEFAULTS = {
	OpenAI: ["text-embedding-3-small", 512],
	"Google Gemini": ["gemini-embedding-001", 768],
	"Voyage AI": ["voyage-3.5", 1024],
	"OpenAI Compatible": ["nomic-embed-text", 0],
};

frappe.ui.form.on("Chatbot Settings", {
	onload(frm) {
		// Allow typing a model ID that is not in the loaded list (new or fine-tuned models).
		frm.fields_dict.model.df.ignore_validation = 1;
	},

	refresh(frm) {
		frm.add_custom_button(__("Refresh Model List"), () =>
			load_models(frm, { refresh: true, notify: true }),
		);
		load_models(frm);

		if (frm.doc.enable_rag) {
			frm.add_custom_button(
				__("Full Re-index"),
				() =>
					frappe.confirm(
						__(
							"Delete all stored vectors and embed everything again? This calls the embedding API for every product.",
						),
						() => rebuild_index(frm, true),
					),
				__("Semantic Search"),
			);
			load_index_status(frm);
		}
		frappe.realtime.off("chat_bridge_index_progress");
		frappe.realtime.on("chat_bridge_index_progress", () => load_index_status(frm));
	},

	embedding_provider(frm) {
		const known = Object.values(EMBEDDING_DEFAULTS).map(([model]) => model);
		const [model, dimensions] = EMBEDDING_DEFAULTS[frm.doc.embedding_provider] || ["", 0];
		if (!frm.doc.embedding_model || known.includes(frm.doc.embedding_model)) {
			frm.set_value("embedding_model", model);
			frm.set_value("embedding_dimensions", dimensions);
		}
	},

	rebuild_index_button(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the settings first."));
			return;
		}
		rebuild_index(frm, false);
	},

	provider(frm) {
		// Only pre-fill models we know; for other providers the admin picks from the loaded list.
		const known = Object.values(DEFAULT_MODELS);
		if (!frm.doc.model || known.includes(frm.doc.model)) {
			frm.set_value("model", DEFAULT_MODELS[frm.doc.provider] || "");
		}
		set_model_options(frm, []);
		load_models(frm, { notify: true });
	},

	api_key(frm) {
		load_models(frm, { notify: true });
	},

	base_url(frm) {
		load_models(frm, { notify: true });
	},

	test_connection_button(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the settings before testing the connection."));
			return;
		}
		frappe.call({
			method: "chat_bridge.api.chat.test_connection",
			freeze: true,
			freeze_message: __("Contacting {0}...", [frm.doc.provider]),
			callback(r) {
				if (!r.message) return;
				frappe.msgprint({
					title: r.message.ok ? __("Connection Successful") : __("Connection Failed"),
					indicator: r.message.ok ? "green" : "red",
					message: frappe.utils.escape_html(r.message.message),
				});
			},
		});
	},
});

function load_models(frm, { refresh = false, notify = false } = {}) {
	const needs_key = frm.doc.provider !== "OpenAI Compatible";
	if ((needs_key && !frm.doc.api_key) || (!needs_key && !frm.doc.base_url)) return;

	// Ignore responses from older requests if the provider or key changed meanwhile.
	const request_id = (frm._model_request_id = (frm._model_request_id || 0) + 1);
	frappe.call({
		method: "chat_bridge.api.chat.get_models",
		args: {
			provider: frm.doc.provider,
			api_key: frm.doc.api_key,
			base_url: frm.doc.base_url,
			refresh: refresh ? 1 : 0,
		},
		callback(r) {
			if (request_id !== frm._model_request_id || !r.message) return;
			const { ok, models, message } = r.message;
			if (!ok) {
				set_model_options(frm, []);
				if (notify)
					frappe.show_alert(
						{ message: __("Could not load models: {0}", [message]), indicator: "red" },
						8,
					);
				return;
			}
			set_model_options(frm, models);
			if (notify) {
				frappe.show_alert({
					message: __("{0} models available", [models.length]),
					indicator: "green",
				});
			}
		},
	});
}

function set_model_options(frm, models) {
	const field = frm.fields_dict.model;
	field.df.options = models;
	field.set_data(models);
	if (field.awesomplete) field.awesomplete.maxItems = Math.max(models.length, 10);
}

function rebuild_index(frm, full) {
	frappe.call({
		method: "chat_bridge.api.chat.rebuild_index",
		args: { full: full ? 1 : 0 },
		callback() {
			frappe.show_alert({ message: __("Indexing started in the background"), indicator: "blue" });
			load_index_status(frm);
		},
	});
}

function load_index_status(frm) {
	frappe.call({
		method: "chat_bridge.api.chat.get_index_status",
		callback(r) {
			if (r.message) render_index_status(frm, r.message);
		},
	});
}

function render_index_status(frm, status) {
	const esc = frappe.utils.escape_html;
	const counts = Object.entries(status.indexed || {});
	const total = counts.reduce((sum, [, n]) => sum + n, 0);
	const states = {
		indexing: [
			"blue",
			__("Indexing {0} of {1} changed chunks...", [status.done || 0, status.total || 0]),
		],
		ready: ["green", __("Ready")],
		error: ["red", __("Last indexing failed")],
	};
	const [color, label] = states[status.state] || ["gray", __("Not indexed yet")];

	let html = `<div class="text-muted small" style="line-height: 1.8">
		<div><span class="indicator-pill ${color}">${esc(label)}</span></div>
		<div>${__("Indexed chunks")}: <b>${total}</b>
			${counts.length ? "(" + counts.map(([type, n]) => `${esc(__(type))}: ${n}`).join(", ") + ")" : ""}</div>`;
	if (status.synced_on) {
		html += `<div>${__("Last synced")}: ${esc(frappe.datetime.str_to_user(status.synced_on))}</div>`;
	}
	if (status.state === "error" && status.error) {
		html += `<div class="text-danger">${esc(status.error)}</div>`;
	}
	html += `<div>${__("Changes to products, policies and pages are picked up every 30 minutes; FAQ changes right away.")}</div></div>`;
	frm.fields_dict.rag_status_html.$wrapper.html(html);
}

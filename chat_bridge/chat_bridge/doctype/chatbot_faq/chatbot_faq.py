# Copyright (c) 2026, saad and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ChatbotFAQ(Document):
	def on_update(self):
		_reindex()

	def on_trash(self):
		_reindex()


def _reindex():
	from chat_bridge.chatbot.rag import enqueue_sync, is_enabled

	# Only changed chunks are re-embedded, so this is cheap.
	if is_enabled(frappe.get_cached_doc("Chatbot Settings")):
		enqueue_sync()

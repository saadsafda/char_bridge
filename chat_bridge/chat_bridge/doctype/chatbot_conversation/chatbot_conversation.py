# Copyright (c) 2026, saad and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from chat_bridge.chatbot.engine import summarize_conversation


class ChatbotConversation(Document):
	@frappe.whitelist()
	def generate_summary(self):
		self.db_set({"summary": summarize_conversation(self), "summarized_at": now_datetime()})

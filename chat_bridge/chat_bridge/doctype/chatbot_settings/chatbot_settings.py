# Copyright (c) 2026, saad and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

RAG_FIELDS = (
	"enable_rag",
	"embedding_provider",
	"embedding_api_key",
	"embedding_model",
	"embedding_base_url",
	"embedding_dimensions",
)


class ChatbotSettings(Document):
	def validate(self):
		self.model = (self.model or "").strip()
		self.base_url = (self.base_url or "").strip().rstrip("/")

		if self.provider == "OpenAI Compatible" and not self.base_url:
			frappe.throw(_("Base URL is required for OpenAI Compatible providers."))

		if self.enabled and not self.get_password("api_key", raise_exception=False):
			# Ollama and other local servers accept any key, so only the hosted providers need one.
			if self.provider != "OpenAI Compatible":
				frappe.throw(_("Please set the API Key before enabling the chatbot."))

		if self.product_url_template and "{slug}" not in self.product_url_template:
			frappe.throw(_("Product URL Template must contain the {slug} placeholder."))

		self.validate_rag()

	def validate_rag(self):
		if not self.enable_rag:
			return
		self.embedding_model = (self.embedding_model or "").strip()
		self.embedding_base_url = (self.embedding_base_url or "").strip().rstrip("/")
		has_key = self.get_password("embedding_api_key", raise_exception=False) or (
			self.embedding_provider == self.provider and self.get_password("api_key", raise_exception=False)
		)
		if not has_key and self.embedding_provider != "OpenAI Compatible":
			frappe.throw(_("Please set the Embedding API Key to use semantic search."))

	def on_update(self):
		# Index (or re-index for a new model) as soon as semantic search is configured.
		if self.enable_rag and any(self.has_value_changed(f) for f in RAG_FIELDS):
			from chat_bridge.chatbot.rag import enqueue_sync

			enqueue_sync()

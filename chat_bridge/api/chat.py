"""Public chatbot API for the e-commerce website.

Endpoints (all under /api/method/chat_bridge.api.chat.<name>):
	get_config     GET   widget settings (bot name, welcome message, suggested questions)
	send_message   POST  send a customer message, returns the bot reply
	get_history    GET   messages of an existing session
	test_connection      used by the Chatbot Settings form (System Manager only)
	get_models           model list for the Chatbot Settings form (System Manager only)
	rebuild_index / get_index_status   semantic search index (System Manager only)

Send the website's customer token as `Customer-Authorization: Bearer <token>` to let the bot see
the customer's own prices and orders. Without it the customer is treated as a guest.
"""

import hashlib

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, validate_email_address, validate_phone_number

from chat_bridge.chatbot import rag
from chat_bridge.chatbot.engine import get_settings, run_turn
from chat_bridge.chatbot.providers import LLMError, get_provider


def _rate_limit_per_hour():
	return cint(frappe.db.get_single_value("Chatbot Settings", "rate_limit_per_hour")) or 60


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_config(company=None):
	settings = get_settings()
	return {
		"enabled": bool(settings.enabled),
		"bot_name": settings.bot_name,
		"welcome_message": settings.welcome_message,
		"suggested_questions": [
			q.strip() for q in (settings.suggested_questions or "").splitlines() if q.strip()
		],
		"max_message_length": cint(settings.max_message_length) or 1000,
		"require_guest_details": bool(settings.require_guest_details),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=_rate_limit_per_hour, seconds=60 * 60)
def send_message(
	message,
	session_id=None,
	company=None,
	guest_name=None,
	guest_email=None,
	guest_phone=None,
	guest_company=None,
):
	settings = get_settings()
	if not settings.enabled:
		frappe.throw(_("The chatbot is currently unavailable."), frappe.ValidationError)

	message = (message or "").strip()
	if not message:
		frappe.throw(_("Message cannot be empty."))
	max_length = cint(settings.max_message_length) or 1000
	if len(message) > max_length:
		frappe.throw(_("Message is too long. Please keep it under {0} characters.").format(max_length))

	customer = _get_customer()
	session_id = _clean_session_id(session_id)
	guest = None if customer else _clean_guest_details(guest_name, guest_email, guest_phone, guest_company)
	if not customer and not guest and settings.require_guest_details and not _has_guest_details(session_id):
		frappe.throw(_("Please enter your name, email, phone and company to start chatting."))

	return run_turn(
		settings,
		message=message,
		company=_resolve_company(company, settings),
		customer=customer,
		session_id=session_id,
		guest=guest,
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_history(session_id, company=None):
	session_id = _clean_session_id(session_id)
	if not session_id:
		return {"session_id": None, "messages": []}

	conversation = frappe.db.get_value(
		"Chatbot Conversation", {"session_id": session_id}, ["name", "company", "customer"], as_dict=True
	)
	customer = _get_customer()
	if (
		not conversation
		or conversation.company != _resolve_company(company, get_settings())
		or (conversation.customer and conversation.customer != customer)
	):
		return {"session_id": None, "messages": []}

	messages = frappe.get_all(
		"Chatbot Message",
		filters={"parent": conversation.name, "parenttype": "Chatbot Conversation"},
		fields=["role", "content", "timestamp"],
		order_by="idx asc",
	)
	return {"session_id": session_id, "messages": messages}


@frappe.whitelist(methods=["POST"])
def test_connection():
	frappe.only_for("System Manager")
	settings = frappe.get_doc("Chatbot Settings")
	try:
		provider = get_provider(settings)
		provider.start(
			"You are a connection test. Reply with exactly: OK", [{"role": "user", "content": "Test"}]
		)
		response = provider.complete(None)
	except LLMError as e:
		return {"ok": False, "message": str(e)}
	except Exception as e:
		return {"ok": False, "message": f"{type(e).__name__}: {e}"}
	return {
		"ok": True,
		"message": _("{0} replied: {1}").format(settings.model, (response.text or "").strip()[:200]),
	}


@frappe.whitelist(methods=["POST"])
def get_models(provider=None, api_key=None, base_url=None, refresh=False):
	"""Models available to the API key, for the Model field in Chatbot Settings.

	Uses the values currently in the form, so the list loads before the settings are saved.
	"""
	frappe.only_for("System Manager")
	settings = frappe.get_doc("Chatbot Settings")
	settings.provider = provider or settings.provider
	settings.base_url = (base_url or settings.base_url or "").strip().rstrip("/")
	# A saved Password field comes back to the form masked as asterisks.
	if not api_key or set(api_key) == {"*"}:
		api_key = settings.get_password("api_key", raise_exception=False)
	if not api_key and settings.provider != "OpenAI Compatible":
		return {"ok": False, "message": _("Enter the API key to load the model list."), "models": []}

	key_hash = hashlib.sha256(f"{settings.provider}|{settings.base_url}|{api_key}".encode()).hexdigest()
	cache_key = f"chat_bridge:models:{key_hash}"
	if not cint(refresh) and (cached := frappe.cache.get_value(cache_key)):
		return {"ok": True, "models": cached}

	try:
		models = get_provider(settings, api_key=api_key).list_models()
	except LLMError as e:
		return {"ok": False, "message": str(e), "models": []}
	except Exception as e:
		frappe.log_error(title="Chatbot model list error")
		return {"ok": False, "message": f"{type(e).__name__}: {e}", "models": []}

	frappe.cache.set_value(cache_key, models, expires_in_sec=60 * 60)
	return {"ok": True, "models": models}


@frappe.whitelist(methods=["POST"])
def rebuild_index(full=False):
	"""Start indexing for semantic search. `full` drops all vectors first; otherwise only changes are embedded."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc("Chatbot Settings")
	if not rag.is_enabled(settings):
		frappe.throw(_("Enable Semantic Search and save the settings first."))
	rag.enqueue_sync(rebuild=bool(cint(full)))
	return {"ok": True}


@frappe.whitelist()
def get_index_status():
	frappe.only_for("System Manager")
	return rag.get_status(frappe.get_doc("Chatbot Settings"))


def _resolve_company(company, settings):
	if company and company != "null" and frappe.db.exists("Company", company):
		return company
	return settings.default_company or "Cotton Valley"


def _get_customer():
	"""Customer from the website's Customer-Authorization token, or None for guests."""
	from cotton_valley.api.common import check_customer_token, get_customer_from_token

	if not check_customer_token():
		return None
	return get_customer_from_token()


def _clean_guest_details(name, email, phone, company):
	"""Validated contact details a guest entered in the chat widget, or None if none were sent."""
	details = {
		"guest_name": (name or "").strip()[:140],
		"guest_email": (email or "").strip()[:140],
		"guest_phone": (phone or "").strip()[:30],
		"guest_company": (company or "").strip()[:140],
	}
	if not any(details.values()):
		return None
	if not all(details.values()):
		frappe.throw(_("Please enter your name, email, phone and company."))
	if not validate_email_address(details["guest_email"]):
		frappe.throw(_("Please enter a valid email address."))
	if not validate_phone_number(details["guest_phone"]) or sum(c.isdigit() for c in details["guest_phone"]) < 7:
		frappe.throw(_("Please enter a valid phone number."))
	return details


def _has_guest_details(session_id):
	"""True if this guest already gave their details earlier in the conversation."""
	return bool(
		session_id and frappe.db.get_value("Chatbot Conversation", {"session_id": session_id}, "guest_email")
	)


def _clean_session_id(session_id):
	if not session_id or session_id == "null":
		return None
	session_id = str(session_id).strip()
	return session_id if session_id.isalnum() and len(session_id) <= 64 else None

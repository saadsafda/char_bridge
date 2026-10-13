import frappe


def execute():
	# New Check fields don't pick up their default on an existing Single, so switch it on explicitly
	frappe.db.set_single_value("Chatbot Settings", "require_guest_details", 1)

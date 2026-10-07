"""Store tools the chatbot can call. Data comes from the Cotton Valley app's doctypes.

Every tool returns a JSON-serialisable dict. Tools never expose exact stock levels, and prices and
orders are only returned in the context of the logged-in customer.
"""

import frappe
from frappe.core.utils import html2text
from frappe.utils import cint, flt

MAX_POLICY_CHARS = 12000
MAX_DESCRIPTION_CHARS = 1500
CANDIDATES = 50

POLICY_DOCTYPES = {
	"privacy_policy": "Privacy Policy Page",
	"shipping_and_returns": "Shipping and Return Page",
	"terms_and_conditions": "Terms and Condition Page",
}


class ChatContext:
	"""Who is chatting and for which storefront."""

	def __init__(self, company, customer=None, settings=None):
		self.company = company
		self.customer = customer
		self.settings = settings
		self.seen_products = {}  # product id -> card, used to attach product cards to the reply

	@property
	def is_logged_in(self):
		return bool(self.customer)


def get_tool_definitions(ctx):
	tools = [
		{
			"name": "search_products",
			"description": (
				"Search the store catalogue by keywords (product name, code, brand or description). "
				"Use it for any question about what products the store sells, availability or prices. "
				"Returns at most `limit` products."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"query": {"type": "string", "description": "Keywords, e.g. 'cotton towel white'."},
					"category": {"type": "string", "description": "Optional category name to filter by."},
					"in_stock_only": {"type": "boolean", "description": "Only return products in stock."},
					"limit": {"type": "integer", "description": "Max results (1-10). Default 6."},
				},
				"required": ["query"],
			},
		},
		{
			"name": "get_product_details",
			"description": "Get full details of one product (description, case pack, dimensions, stock status, price, categories) by its product id from search results.",
			"parameters": {
				"type": "object",
				"properties": {"product_id": {"type": "string", "description": "The product id."}},
				"required": ["product_id"],
			},
		},
		{
			"name": "list_categories",
			"description": "List the store's product categories and their subcategories.",
			"parameters": {"type": "object", "properties": {}},
		},
		{
			"name": "get_store_policy",
			"description": (
				"Get the full text of a store policy. Use it for any question about privacy and personal data, "
				"shipping, delivery, returns, refunds, exchanges, or terms and conditions."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"policy": {
						"type": "string",
						"enum": list(POLICY_DOCTYPES),
						"description": "Which policy to fetch.",
					}
				},
				"required": ["policy"],
			},
		},
		{
			"name": "get_store_info",
			"description": "Get store contact details, customer service info, pickup hours, address, minimum order amount and free-shipping threshold.",
			"parameters": {"type": "object", "properties": {}},
		},
	]
	from chat_bridge.chatbot import rag

	if rag.is_enabled(ctx.settings):
		tools.append(
			{
				"name": "search_store_knowledge",
				"description": (
					"Search the store's FAQ, policies and store pages by meaning. Use it for questions the "
					"knowledge already provided with the message does not answer, e.g. a follow-up about a "
					"different topic. Returns the most relevant passages."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"query": {
							"type": "string",
							"description": "What to look for, as a question or keywords.",
						}
					},
					"required": ["query"],
				},
			}
		)
	if ctx.is_logged_in:
		tools += [
			{
				"name": "get_my_orders",
				"description": "List the logged-in customer's most recent orders with their status.",
				"parameters": {
					"type": "object",
					"properties": {
						"limit": {"type": "integer", "description": "Max orders (1-10). Default 5."}
					},
				},
			},
			{
				"name": "get_order_status",
				"description": "Get status, items and totals for one of the logged-in customer's orders.",
				"parameters": {
					"type": "object",
					"properties": {"order_number": {"type": "string", "description": "The order number."}},
					"required": ["order_number"],
				},
			},
		]
	return tools


def run_tool(ctx, name, arguments):
	handler = TOOL_HANDLERS.get(name)
	if not handler or (name in AUTH_TOOLS and not ctx.is_logged_in):
		return {"error": f"Unknown tool '{name}'."}
	return handler(ctx, **{k: v for k, v in (arguments or {}).items() if v is not None})


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def search_products(ctx, query="", category=None, in_stock_only=False, limit=6, **_):
	"""Hybrid search: keyword matches and semantic (meaning) matches, merged by rank."""
	limit = max(1, min(cint(limit) or 6, 10))
	filters, values = _product_filters(ctx, category, in_stock_only)
	keyword_ids = _keyword_candidates(query, filters, dict(values))
	semantic_ids = _semantic_candidates(ctx, query, filters, dict(values))
	ranked = _fuse([keyword_ids, semantic_ids])[:limit]
	if not ranked:
		return {"products": [], "note": "No matching products. Try fewer or different keywords."}

	rows = frappe.db.sql(
		"""
		SELECT i.name, i.item_name, i.brand, i.item_group, i.custom_case_pack, i.image,
			i.custom_short_description, i.custom_coming_soon, i.custom_new_arrivals,
			CASE WHEN i.set_threshold = 1 THEN i.threshold_stock ELSE i.available_stock END AS stock
		FROM `tabItem` i WHERE i.name IN %(ids)s
		""",
		{"ids": ranked},
		as_dict=True,
	)
	items = sorted(rows, key=lambda r: ranked.index(r.name))
	prices = _get_prices(ctx, ranked)
	keyword_set = set(keyword_ids)
	products = []
	for item in items:
		product = {
			"id": item.name,
			"name": item.item_name,
			"brand": item.brand,
			"type": item.item_group,
			"case_pack": item.custom_case_pack,
			"short_description": _clean(item.custom_short_description, 200),
			"stock_status": _stock_status(item),
			"new_arrival": bool(item.custom_new_arrivals),
			**_price_fields(ctx, prices.get(item.name)),
			"url": _product_url(ctx, item.name),
			"match": "keyword" if item.name in keyword_set else "similar",
		}
		products.append(product)
		_remember(ctx, item, product)

	result = {"products": products}
	if not keyword_ids:
		result["note"] = (
			"No exact keyword match; these are the most similar products. Check they fit what the customer asked."
		)
	return result


def _product_filters(ctx, category, in_stock_only):
	conditions = ["i.hide = 0", "i.company = %(company)s"]
	values = {"company": ctx.company}
	if category:
		values["category"] = f"%{category}%"
		conditions.append(
			"""i.name IN (
				SELECT c.parent FROM `tabProduct Categoris` c
				INNER JOIN `tabProduct Category` pc ON pc.name = c.product_category
				WHERE pc.title LIKE %(category)s OR pc.name LIKE %(category)s
			)"""
		)
	if in_stock_only:
		conditions.append(
			"(CASE WHEN i.set_threshold = 1 THEN i.threshold_stock ELSE i.available_stock END) > 0"
		)
	return conditions, values


def _keyword_candidates(query, filters, values):
	conditions = list(filters)
	scores = []
	# Every keyword must match at least one searchable field; name matches rank highest, because
	# descriptions often mention other things ("dry with a soft towel").
	for idx, word in enumerate(w for w in str(query).split()[:8] if len(w) > 1):
		key = f"w{idx}"
		values[key] = f"%{word}%"
		subcategory_match = (
			f"i.custom_sub_category IN (SELECT name FROM `tabProduct Subcategory` WHERE title LIKE %({key})s)"
		)
		conditions.append(
			f"(i.item_name LIKE %({key})s OR i.name LIKE %({key})s OR i.brand LIKE %({key})s "
			f"OR {subcategory_match} "
			f"OR i.custom_short_description LIKE %({key})s OR i.description LIKE %({key})s)"
		)
		scores.append(
			f"(i.item_name LIKE %({key})s OR i.name LIKE %({key})s) * 4 + ({subcategory_match}) * 2 "
			f"+ (i.brand LIKE %({key})s) * 2 + (i.custom_short_description LIKE %({key})s)"
		)
	if not scores:
		return []
	return frappe.db.sql_list(
		f"""
		SELECT i.name FROM `tabItem` i
		WHERE {" AND ".join(conditions)}
		ORDER BY {" + ".join(scores)} DESC,
			CASE WHEN i.website_ranking IS NULL OR i.website_ranking = 0 THEN 1 ELSE 0 END,
			i.website_ranking ASC, i.custom_no_of_clicks DESC
		LIMIT {CANDIDATES}
		""",
		values,
	)


def _semantic_candidates(ctx, query, filters, values):
	from chat_bridge.chatbot import rag

	if not query or not rag.is_enabled(ctx.settings):
		return []
	try:
		hits = rag.search_products(ctx.settings, ctx.company, query, top_k=CANDIDATES * 3)
	except Exception:
		# Embedding API down: keyword search still works.
		frappe.log_error(title="Chatbot semantic search error")
		return []
	if not hits:
		return []
	ids = [item_code for item_code, _score in hits]
	values["ids"] = ids
	allowed = set(
		frappe.db.sql_list(
			f"SELECT i.name FROM `tabItem` i WHERE i.name IN %(ids)s AND {' AND '.join(filters)}", values
		)
	)
	return [i for i in ids if i in allowed][:CANDIDATES]


def _fuse(rankings, k=60):
	"""Reciprocal rank fusion: items ranked high in either list come first."""
	scores = {}
	for ranking in rankings:
		for rank, item in enumerate(ranking):
			scores[item] = scores.get(item, 0) + 1 / (k + rank + 1)
	return sorted(scores, key=scores.get, reverse=True)


def search_store_knowledge(ctx, query="", **_):
	from chat_bridge.chatbot import rag

	try:
		results = rag.search_knowledge(
			ctx.settings, ctx.company, query, top_k=cint(ctx.settings.knowledge_results) or 4
		)
	except Exception:
		frappe.log_error(title="Chatbot knowledge search error")
		return {"error": "Knowledge search is unavailable. Use get_store_policy or get_store_info instead."}
	if not results:
		return {"results": [], "note": "Nothing relevant found. Try get_store_policy or get_store_info."}
	return {"results": [{k: r[k] for k in ("type", "title", "text")} for r in results]}


def get_product_details(ctx, product_id="", **_):
	item = frappe.db.get_value(
		"Item",
		{"name": product_id, "hide": 0, "company": ctx.company},
		[
			"name",
			"item_name",
			"brand",
			"item_group",
			"description",
			"custom_short_description",
			"custom_case_pack",
			"stock_uom",
			"image",
			"custom_sub_category",
			"custom_upc",
			"custom_item_width_inch",
			"custom_item_length_inch",
			"custom_item_height_inch",
			"custom_item_weight_lbs",
			"custom_package_width_inch",
			"custom_package_length_inch",
			"custom_package_height_inch",
			"custom_weight_lbs",
			"custom_coming_soon",
			"custom_new_arrivals",
			"set_threshold",
			"threshold_stock",
			"available_stock",
		],
		as_dict=True,
	)
	if not item:
		return {"error": f"Product '{product_id}' not found."}

	item.stock = item.threshold_stock if item.set_threshold else item.available_stock
	categories = frappe.db.sql(
		"""
		SELECT pc.title FROM `tabProduct Categoris` c
		INNER JOIN `tabProduct Category` pc ON pc.name = c.product_category
		WHERE c.parent = %s
		""",
		item.name,
		pluck=True,
	)
	subcategory = (
		frappe.db.get_value("Product Subcategory", item.custom_sub_category, "title")
		if item.custom_sub_category
		else None
	)
	related = frappe.get_all(
		"Recommended Products", filters={"parent": item.name}, pluck="product_name", limit=5
	)

	product = {
		"id": item.name,
		"name": item.item_name,
		"brand": item.brand,
		"type": item.item_group,
		"short_description": _clean(item.custom_short_description, 300),
		"description": _clean(item.description, MAX_DESCRIPTION_CHARS),
		"categories": categories,
		"subcategory": subcategory,
		"unit": item.stock_uom,
		"case_pack": item.custom_case_pack,
		"upc": item.custom_upc,
		"item_dimensions_inch": _dims(
			item.custom_item_length_inch, item.custom_item_width_inch, item.custom_item_height_inch
		),
		"item_weight_lbs": item.custom_item_weight_lbs,
		"package_dimensions_inch": _dims(
			item.custom_package_length_inch, item.custom_package_width_inch, item.custom_package_height_inch
		),
		"package_weight_lbs": item.custom_weight_lbs,
		"stock_status": _stock_status(item),
		"new_arrival": bool(item.custom_new_arrivals),
		**_price_fields(ctx, _get_prices(ctx, [item.name]).get(item.name)),
		"related_products": related,
		"url": _product_url(ctx, item.name),
	}
	_remember(ctx, item, product)
	return product


def list_categories(ctx, **_):
	categories = frappe.db.sql(
		"""
		SELECT name, title FROM `tabProduct Category`
		WHERE company = %s
		ORDER BY CASE WHEN web_ranking IS NULL OR web_ranking = 0 THEN 1 ELSE 0 END, web_ranking ASC
		""",
		ctx.company,
		as_dict=True,
	)
	subcategories = frappe.db.sql(
		"""
		SELECT sc.parent, COALESCE(sub.title, sc.product_subcategory) AS title
		FROM `tabSubCategories` sc
		LEFT JOIN `tabProduct Subcategory` sub ON sub.name = sc.product_subcategory
		WHERE sc.parenttype = 'Product Category'
		ORDER BY sc.idx
		""",
		as_dict=True,
	)
	sub_map = {}
	for row in subcategories:
		sub_map.setdefault(row.parent, []).append(row.title)
	return {
		"categories": [
			{"name": c.title or c.name, "subcategories": sub_map.get(c.name, [])} for c in categories
		]
	}


# ---------------------------------------------------------------------------
# Policies and store info
# ---------------------------------------------------------------------------


def get_store_policy(ctx, policy="", **_):
	doctype = POLICY_DOCTYPES.get(policy)
	if not doctype:
		return {"error": f"Unknown policy '{policy}'. Use one of: {', '.join(POLICY_DOCTYPES)}."}
	text = _clean(frappe.db.get_single_value(_company_doctype(ctx, doctype), "details"), MAX_POLICY_CHARS)
	if not text:
		return {"policy": policy, "text": "", "note": "This policy has not been published yet."}
	return {"policy": policy, "text": text}


def get_store_info(ctx, **_):
	contact = frappe.get_cached_doc(_company_doctype(ctx, "Contact Page"))
	theme = frappe.get_cached_doc(_company_doctype(ctx, "Website Theme Settings"))
	info = {
		"store_name": theme.get("site_title") or ctx.company,
		"phone": contact.get("phone") or theme.get("support_number"),
		"email": contact.get("email") or theme.get("support_email") or theme.get("about_email"),
		"address": _clean(contact.get("address"), 800) or theme.get("about_address"),
		"customer_service": _clean(contact.get("customer_service"), 1500),
		"ordering_info": _clean(contact.get("product_order"), 1500),
		"pickup_hours": _clean(contact.get("pickup_hours"), 800),
		"minimum_order_amount": flt(theme.get("min_order_amount")) or None,
		"free_shipping_minimum_order": flt(theme.get("min_order_free_shipping")) or None,
		"currency": frappe.get_cached_value("Company", ctx.company, "default_currency"),
	}
	return {k: v for k, v in info.items() if v}


# ---------------------------------------------------------------------------
# Orders (logged-in customers only)
# ---------------------------------------------------------------------------


def get_my_orders(ctx, limit=5, **_):
	limit = max(1, min(cint(limit) or 5, 10))
	orders = frappe.get_all(
		"Sales Order",
		filters={"customer": ctx.customer, "company": ctx.company, "docstatus": 1},
		fields=[
			"name as order_number",
			"submit_datetime as placed_on",
			"grand_total as total",
			"currency",
			"order_status as status",
			"custom_mode_of_payment as payment_method",
			"custom_shipping_method as shipping_method",
		],
		order_by="creation desc",
		limit_page_length=limit,
	)
	if not orders:
		return {"orders": [], "note": "This customer has no submitted orders."}
	return {"orders": orders}


def get_order_status(ctx, order_number="", **_):
	order_number = str(order_number).strip().lstrip("#")
	name = frappe.db.get_value(
		"Sales Order",
		{"name": order_number, "customer": ctx.customer, "docstatus": 1},
		"name",
	)
	if not name:
		return {"error": f"No order '{order_number}' found for this customer."}

	so = frappe.get_doc("Sales Order", name)
	invoiced = frappe.db.exists(
		"Sales Invoice Item", {"sales_order": so.name, "docstatus": 1, "parenttype": "Sales Invoice"}
	)
	return {
		"order_number": so.name,
		"placed_on": str(so.get("submit_datetime") or so.transaction_date),
		"status": so.get("order_status"),
		"payment_method": so.get("custom_mode_of_payment"),
		"shipping_method": so.get("custom_shipping_method"),
		"shipping_city_state": ", ".join(
			filter(None, [so.get("shipping_state"), so.get("shipping_country")])
		),
		"currency": so.currency,
		"total": so.grand_total,
		"invoiced": bool(invoiced),
		"items": [
			{"name": i.item_name, "code": i.item_code, "qty": i.qty, "amount": i.amount}
			for i in so.items[:30]
		],
		"item_count": len(so.items),
	}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _company_doctype(ctx, doctype):
	return f"UDC {doctype}" if ctx.company == "UDC" else doctype


def _clean(value, limit):
	if not value:
		return ""
	text = html2text(value, wrap=False) if "<" in value else value
	text = "\n".join(line.rstrip() for line in text.splitlines())
	while "\n\n\n" in text:
		text = text.replace("\n\n\n", "\n\n")
	text = text.strip()
	return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def _dims(length, width, height):
	if not any([flt(length), flt(width), flt(height)]):
		return None
	return f"{flt(length):g} x {flt(width):g} x {flt(height):g} (L x W x H)"


def _stock_status(item):
	if item.get("custom_coming_soon"):
		return "coming_soon"
	return "in_stock" if flt(item.get("stock")) > 0 else "out_of_stock"


def _get_prices(ctx, item_codes):
	"""Same price rules as the website: customer's price list, falling back to Retail."""
	if not item_codes:
		return {}
	if not ctx.is_logged_in and not ctx.settings.show_prices_to_guests:
		return {}

	price_lists = ["Retail"]
	if ctx.is_logged_in:
		field = "price_list_for_udc" if ctx.company == "UDC" else "price_list_for_cv"
		customer_list = frappe.db.get_value("Customer", ctx.customer, field)
		if customer_list and customer_list != "Retail":
			price_lists.insert(0, customer_list)

	rows = frappe.get_all(
		"Item Price",
		filters={"item_code": ["in", item_codes], "price_list": ["in", price_lists]},
		fields=["item_code", "price_list", "price_list_rate", "currency"],
	)
	prices = {}
	for price_list in reversed(price_lists):  # customer price list overrides Retail
		for row in rows:
			if row.price_list == price_list and flt(row.price_list_rate) > 0:
				prices[row.item_code] = {"price": flt(row.price_list_rate), "currency": row.currency}
	return prices


def _price_fields(ctx, price):
	if price:
		return price
	if not ctx.is_logged_in and not ctx.settings.show_prices_to_guests:
		return {"price": None, "price_note": "Customer must log in to see prices."}
	return {"price": None, "price_note": "Price not available, contact the store."}


def _product_url(ctx, product_id):
	template = ctx.settings.product_url_template
	return template.replace("{slug}", product_id) if template else None


def _remember(ctx, item, product):
	from cotton_valley.api.website_theme_setting import get_file

	ctx.seen_products[product["id"]] = {
		"id": product["id"],
		"name": product["name"],
		"slug": product["id"],
		"thumbnail": get_file(item.image).get("original_url") if item.get("image") else "",
		"price": product.get("price"),
		"currency": product.get("currency"),
		"stock_status": product["stock_status"],
		"url": product.get("url"),
	}


TOOL_HANDLERS = {
	"search_products": search_products,
	"get_product_details": get_product_details,
	"list_categories": list_categories,
	"get_store_policy": get_store_policy,
	"get_store_info": get_store_info,
	"search_store_knowledge": search_store_knowledge,
	"get_my_orders": get_my_orders,
	"get_order_status": get_order_status,
}
AUTH_TOOLS = {"get_my_orders", "get_order_status"}

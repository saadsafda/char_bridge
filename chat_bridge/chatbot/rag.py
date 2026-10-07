"""Semantic search (RAG) over the store's products and knowledge.

Indexing: products, FAQs, policies and store pages are split into chunks, embedded, and stored in
"Chatbot Embedding". `sync_index` only re-embeds chunks whose text changed, so it is cheap to run
on a schedule.

Search: each worker keeps the vectors in memory as a numpy matrix per company and reloads them when
the index version (in Redis) changes. With a few thousand products this takes milliseconds.
"""

import base64
import hashlib
import time
from dataclasses import dataclass

import frappe
import numpy as np
from frappe.utils import cint, now

from chat_bridge.chatbot.embeddings import Embedder, EmbeddingError

PRODUCT = "Product"
FAQ = "FAQ"
POLICY = "Policy"
STORE_PAGE = "Store Page"
KNOWLEDGE_TYPES = (FAQ, POLICY, STORE_PAGE)

CHUNK_CHARS = 1000
VERSION_KEY = "chat_bridge:rag_version"
STATUS_KEY = "chat_bridge:rag_status"
SYNC_JOB_ID = "chat_bridge_rag_sync"

POLICIES = {
	"Privacy Policy Page": "Privacy Policy",
	"Shipping and Return Page": "Shipping and Returns Policy",
	"Terms and Condition Page": "Terms and Conditions",
}

# (site, signature, company, group) -> (version, row names, source names, normalised matrix)
_VECTORS = {}


@dataclass
class Chunk:
	source_type: str
	source_doctype: str
	source_name: str
	company: str
	chunk: int
	title: str
	content: str

	@property
	def key(self):
		return (self.source_type, self.source_name, self.company, self.chunk)

	def hash(self, signature):
		return hashlib.sha1(f"{signature}\n{self.title}\n{self.content}".encode()).hexdigest()


def is_enabled(settings):
	return bool(settings.enable_rag and settings.embedding_provider and settings.embedding_model)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def enqueue_sync(rebuild=False):
	frappe.enqueue(
		"chat_bridge.chatbot.rag.sync_index",
		queue="long",
		timeout=3 * 60 * 60,
		job_id=SYNC_JOB_ID,
		deduplicate=True,
		enqueue_after_commit=True,
		rebuild=rebuild,
	)


def scheduled_sync():
	if is_enabled(frappe.get_cached_doc("Chatbot Settings")):
		enqueue_sync()


def sync_index(rebuild=False):
	settings = frappe.get_doc("Chatbot Settings")
	if not is_enabled(settings):
		return
	embedder = Embedder(settings)
	signature = embedder.signature

	if rebuild:
		frappe.db.delete("Chatbot Embedding")
		frappe.db.commit()

	desired = build_chunks()
	existing = {
		(r.source_type, r.source_name, r.company or "", r.chunk): r
		for r in frappe.db.sql(
			"select name, source_type, source_name, company, chunk, content_hash from `tabChatbot Embedding`",
			as_dict=True,
		)
	}

	desired_keys = {c.key for c in desired}
	stale = [row.name for key, row in existing.items() if key not in desired_keys]
	_delete(stale)

	todo = [c for c in desired if c.key not in existing or existing[c.key].content_hash != c.hash(signature)]
	changed = bool(stale)
	_set_status(state="indexing", done=0, total=len(todo), chunks=len(desired))

	try:
		for start in range(0, len(todo), 50):
			batch = todo[start : start + 50]
			vectors = embedder.embed([f"{c.title}\n{c.content}" for c in batch], kind="document")
			_delete([existing[c.key].name for c in batch if c.key in existing])
			_insert(batch, vectors, signature)
			frappe.db.commit()
			changed = True
			_set_status(state="indexing", done=start + len(batch), total=len(todo), chunks=len(desired))
	except EmbeddingError as e:
		frappe.db.commit()
		frappe.log_error(title="Chatbot index error", message=str(e))
		_set_status(state="error", error=str(e), chunks=len(desired))
		_bump_version()
		return

	if changed:
		_bump_version()
	_set_status(state="ready", done=len(todo), total=len(todo), chunks=len(desired), synced_on=now())


def build_chunks():
	return _product_chunks() + _faq_chunks() + _policy_and_page_chunks()


def _product_chunks():
	items = frappe.db.sql(
		"""
		SELECT name, item_name, brand, item_group, company, custom_sub_category,
			custom_short_description, description
		FROM `tabItem`
		WHERE hide = 0 AND disabled = 0 AND IFNULL(company, '') != ''
		""",
		as_dict=True,
	)
	categories = {}
	for row in frappe.db.sql(
		"""
		SELECT c.parent, pc.title FROM `tabProduct Categoris` c
		INNER JOIN `tabProduct Category` pc ON pc.name = c.product_category
		""",
		as_dict=True,
	):
		categories.setdefault(row.parent, []).append(row.title)
	subcategories = dict(frappe.db.sql("SELECT name, title FROM `tabProduct Subcategory`"))

	chunks = []
	for item in items:
		lines = [
			item.item_name,
			f"Brand: {item.brand}" if item.brand else "",
			f"Type: {item.item_group}" if item.item_group else "",
			f"Category: {', '.join(categories[item.name])}" if categories.get(item.name) else "",
			f"Subcategory: {subcategories.get(item.custom_sub_category)}" if item.custom_sub_category else "",
			_clean(item.custom_short_description, 400),
			_clean(item.description, 1200),
		]
		content = "\n".join(line for line in lines if line)
		chunks.append(
			Chunk(PRODUCT, "Item", item.name, item.company, 0, item.item_name or item.name, content)
		)
	return chunks


def _faq_chunks():
	return [
		Chunk(FAQ, "Chatbot FAQ", f.name, f.company or "", 0, f.question, _clean(f.answer, 4000))
		for f in frappe.get_all(
			"Chatbot FAQ", filters={"enabled": 1}, fields=["name", "question", "answer", "company"]
		)
	]


def _policy_and_page_chunks():
	chunks = []
	for company in _store_companies():
		prefix = "UDC " if company == "UDC" else ""

		for doctype, title in POLICIES.items():
			text = _clean(frappe.db.get_single_value(prefix + doctype, "details"), 100_000)
			for i, part in enumerate(split_text(text)):
				chunks.append(Chunk(POLICY, prefix + doctype, prefix + doctype, company, i, title, part))

		about = frappe.get_cached_doc(prefix + "About Page")
		text = _clean(about.get("description"), 20_000)
		for i, part in enumerate(split_text(text)):
			chunks.append(
				Chunk(STORE_PAGE, prefix + "About Page", prefix + "About Page", company, i, "About Us", part)
			)

		contact = frappe.get_cached_doc(prefix + "Contact Page")
		theme = frappe.get_cached_doc(prefix + "Website Theme Settings")
		lines = [
			f"Store: {theme.get('site_title') or company}",
			f"Phone: {contact.get('phone') or theme.get('support_number') or ''}",
			f"Email: {contact.get('email') or theme.get('support_email') or ''}",
			_clean(contact.get("address"), 800),
			_clean(contact.get("customer_service"), 800),
			_clean(contact.get("product_order"), 800),
			_clean(contact.get("pickup_hours"), 800),
			f"Minimum order amount: {theme.get('min_order_amount')}" if theme.get("min_order_amount") else "",
			f"Free shipping on orders over: {theme.get('min_order_free_shipping')}"
			if theme.get("min_order_free_shipping")
			else "",
			_clean(theme.get("footer_about"), 800),
		]
		content = "\n".join(line for line in lines if line and not line.endswith(": "))
		chunks.append(
			Chunk(
				STORE_PAGE,
				prefix + "Contact Page",
				prefix + "Contact Page",
				company,
				0,
				"Contact Us",
				content,
			)
		)
	return chunks


def split_text(text, max_chars=CHUNK_CHARS):
	"""Split on paragraphs, keeping the nearest heading at the top of each chunk for context."""
	if not text:
		return []
	chunks, current, heading = [], "", ""
	for para in (p.strip() for p in text.split("\n\n")):
		if not para:
			continue
		if para.startswith("#"):
			heading = para.lstrip("# ").strip()
		pieces = [para[i : i + max_chars] for i in range(0, len(para), max_chars)]
		for piece in pieces:
			if current and len(current) + len(piece) + 2 > max_chars:
				chunks.append(current)
				current = f"{heading}\n{piece}" if heading and not piece.startswith("#") else piece
			else:
				current = f"{current}\n\n{piece}" if current else piece
	if current:
		chunks.append(current)
	return chunks


def _store_companies():
	return frappe.db.sql_list(
		"SELECT DISTINCT company FROM `tabItem` WHERE hide = 0 AND IFNULL(company, '') != ''"
	) or ["Cotton Valley"]


def _insert(chunks, vectors, signature):
	timestamp = now()
	user = frappe.session.user
	rows = []
	for chunk, vector in zip(chunks, vectors, strict=True):
		rows.append(
			(
				frappe.generate_hash(length=12),
				timestamp,
				timestamp,
				user,
				user,
				chunk.source_type,
				chunk.source_doctype,
				chunk.source_name,
				chunk.company,
				chunk.chunk,
				chunk.title[:140],
				chunk.content,
				chunk.hash(signature),
				signature,
				_encode(vector),
			)
		)
	frappe.db.bulk_insert(
		"Chatbot Embedding",
		fields=[
			"name",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"source_type",
			"source_doctype",
			"source_name",
			"company",
			"chunk",
			"title",
			"content",
			"content_hash",
			"embedding_signature",
			"embedding",
		],
		values=rows,
	)


def _delete(names):
	for start in range(0, len(names), 500):
		frappe.db.delete("Chatbot Embedding", {"name": ["in", names[start : start + 500]]})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_products(settings, company, query, top_k=100):
	"""[(item_code, score)] best first. Products have a single chunk, so source name = item code."""
	return [(source, score) for _name, source, score in _search(settings, company, "product", query, top_k)]


def search_knowledge(settings, company, query, top_k=5):
	"""FAQ / policy / store page chunks most related to the query, best first."""
	hits = _search(settings, company, "knowledge", query, top_k)
	if not hits:
		return []
	rows = {
		r.name: r
		for r in frappe.get_all(
			"Chatbot Embedding",
			filters={"name": ["in", [h[0] for h in hits]]},
			fields=["name", "source_type", "title", "content"],
		)
	}
	return [
		{
			"type": rows[name].source_type,
			"title": rows[name].title,
			"text": rows[name].content,
			"score": round(score, 3),
		}
		for name, _source, score in hits
		if name in rows
	]


def has_knowledge_index(settings, company):
	return (
		is_enabled(settings) and len(_get_vectors(Embedder(settings).signature, company, "knowledge")[0]) > 0
	)


def _search(settings, company, group, query, top_k):
	embedder = Embedder(settings)
	names, sources, matrix = _get_vectors(embedder.signature, company, group)
	if not names or not query:
		return []
	vector = _query_vector(embedder, query)
	if vector.shape[0] != matrix.shape[1]:
		return []
	scores = matrix @ vector
	top_k = min(top_k, len(names))
	best = np.argpartition(-scores, top_k - 1)[:top_k]
	best = best[np.argsort(-scores[best])]
	return [(names[i], sources[i], float(scores[i])) for i in best]


def _get_vectors(signature, company, group):
	version = frappe.cache.get_value(VERSION_KEY) or "0"
	key = (frappe.local.site, signature, company, group)
	cached = _VECTORS.get(key)
	if cached and cached[0] == version:
		return cached[1:]

	types = (PRODUCT,) if group == "product" else KNOWLEDGE_TYPES
	rows = frappe.db.sql(
		"""
		SELECT name, source_name, embedding FROM `tabChatbot Embedding`
		WHERE embedding_signature = %s AND source_type IN %s AND (company = %s OR IFNULL(company, '') = '')
		""",
		(signature, types, company),
	)
	names, sources, vectors = [], [], []
	for name, source, encoded in rows:
		vector = _decode(encoded)
		if vectors and vector.shape[0] != vectors[0].shape[0]:
			continue
		names.append(name)
		sources.append(source)
		vectors.append(vector)
	matrix = _normalise(np.vstack(vectors)) if vectors else np.zeros((0, 0), dtype=np.float32)
	_VECTORS[key] = (version, names, sources, matrix)
	return names, sources, matrix


def _query_vector(embedder, query):
	cache_key = (
		"chat_bridge:query_vector:" + hashlib.sha1(f"{embedder.signature}\n{query}".encode()).hexdigest()
	)
	cached = frappe.cache.get_value(cache_key)
	if cached:
		return _decode(cached)
	vector = _normalise(np.asarray(embedder.embed([query[:2000]], kind="query")[0], dtype=np.float32))
	frappe.cache.set_value(cache_key, _encode(vector), expires_in_sec=24 * 60 * 60)
	return vector


# ---------------------------------------------------------------------------
# Status and helpers
# ---------------------------------------------------------------------------


def get_status(settings):
	status = frappe.cache.get_value(STATUS_KEY) or {}
	signature = Embedder(settings).signature if is_enabled(settings) else None
	counts = (
		dict(
			frappe.db.sql(
				"SELECT source_type, COUNT(*) FROM `tabChatbot Embedding` WHERE embedding_signature = %s GROUP BY source_type",
				signature,
			)
		)
		if signature
		else {}
	)
	return {**status, "indexed": {k: cint(v) for k, v in counts.items()}, "enabled": is_enabled(settings)}


def _set_status(**status):
	frappe.cache.set_value(STATUS_KEY, {**status, "updated_on": now()})
	frappe.publish_realtime(
		"chat_bridge_index_progress", status, doctype="Chatbot Settings", docname="Chatbot Settings"
	)


def _bump_version():
	frappe.cache.set_value(VERSION_KEY, str(time.time()))


def _encode(vector):
	return base64.b64encode(np.asarray(vector, dtype=np.float32).tobytes()).decode()


def _decode(encoded):
	return np.frombuffer(base64.b64decode(encoded), dtype=np.float32)


def _normalise(array):
	norms = np.linalg.norm(array, axis=-1, keepdims=True)
	return array / np.where(norms == 0, 1, norms)


def _clean(value, limit):
	from chat_bridge.chatbot.tools import _clean

	return _clean(value, limit)

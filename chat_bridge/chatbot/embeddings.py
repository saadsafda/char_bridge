"""Embedding providers used for semantic search (RAG).

OpenAI and OpenAI-compatible servers (e.g. Ollama) go through the OpenAI SDK. Gemini and Voyage use
their native REST APIs because those accept a task type (document vs query), which improves retrieval.
"""

import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
BATCH_SIZE = 50


class EmbeddingError(Exception):
	"""Raised when the embedding call fails; the message is safe to log, not to show customers."""


class Embedder:
	def __init__(self, settings):
		self.provider = settings.embedding_provider
		self.model = (settings.embedding_model or "").strip()
		self.dimensions = settings.embedding_dimensions or None
		self.base_url = (settings.embedding_base_url or "").strip().rstrip("/")
		self.timeout = settings.request_timeout or 60
		self.api_key = settings.get_password("embedding_api_key", raise_exception=False)
		if not self.api_key and self.provider == settings.provider:
			# Same provider as the chat model: reuse its key.
			self.api_key = settings.get_password("api_key", raise_exception=False)
		self.api_key = self.api_key or ""

	@property
	def signature(self):
		"""Vectors are only comparable when made by the same provider, model and size."""
		return f"{self.provider}:{self.model}:{self.dimensions or 'default'}"

	def embed(self, texts, kind="document"):
		"""Return one vector per text. `kind` is "document" for indexed content, "query" for searches."""
		vectors = []
		for start in range(0, len(texts), BATCH_SIZE):
			batch = texts[start : start + BATCH_SIZE]
			if self.provider == "Google Gemini":
				vectors += self._gemini(batch, kind)
			elif self.provider == "Voyage AI":
				vectors += self._voyage(batch, kind)
			else:
				vectors += self._openai(batch)
		return vectors

	def _openai(self, texts):
		import openai

		client = openai.OpenAI(
			api_key=self.api_key or "not-needed",
			base_url=self.base_url if self.provider == "OpenAI Compatible" else None,
			timeout=self.timeout,
			max_retries=5,
		)
		kwargs = {"model": self.model, "input": texts, "encoding_format": "float"}
		if self.dimensions:
			kwargs["dimensions"] = self.dimensions
		try:
			response = client.embeddings.create(**kwargs)
		except openai.AuthenticationError:
			raise EmbeddingError(f"{self.provider} rejected the embedding API key.")
		except openai.NotFoundError:
			raise EmbeddingError(f"{self.provider} embedding model '{self.model}' was not found.")
		except openai.APIStatusError as e:
			raise EmbeddingError(f"{self.provider} embedding error {e.status_code}: {e.message}")
		except openai.APIConnectionError as e:
			raise EmbeddingError(f"Could not reach {self.provider}: {e}")
		return [d.embedding for d in sorted(response.data, key=lambda d: d.index)]

	def _gemini(self, texts, kind):
		task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
		body = {
			"requests": [
				{
					"model": f"models/{self.model}",
					"content": {"parts": [{"text": text}]},
					"taskType": task,
					**({"outputDimensionality": self.dimensions} if self.dimensions else {}),
				}
				for text in texts
			]
		}
		data = self._post(GEMINI_URL.format(model=self.model), body, {"x-goog-api-key": self.api_key})
		return [e["values"] for e in data["embeddings"]]

	def _voyage(self, texts, kind):
		body = {"input": texts, "model": self.model, "input_type": kind}
		if self.dimensions:
			body["output_dimension"] = self.dimensions
		data = self._post(VOYAGE_URL, body, {"Authorization": f"Bearer {self.api_key}"})
		return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]

	def _post(self, url, body, headers):
		for attempt in range(5):
			try:
				response = requests.post(url, json=body, headers=headers, timeout=self.timeout)
			except requests.RequestException as e:
				raise EmbeddingError(f"Could not reach {self.provider}: {e}")
			# Back off on rate limits and transient server errors (the initial index makes many calls).
			if response.status_code not in (429, 500, 502, 503, 504) or attempt == 4:
				break
			time.sleep(2**attempt)
		if response.status_code in (401, 403):
			raise EmbeddingError(f"{self.provider} rejected the embedding API key.")
		if response.status_code != 200:
			raise EmbeddingError(
				f"{self.provider} embedding error {response.status_code}: {response.text[:300]}"
			)
		return response.json()

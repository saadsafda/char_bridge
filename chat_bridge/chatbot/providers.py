"""LLM provider adapters.

Each provider keeps the conversation in its own native message format and exposes the same
three calls, so the engine can run one tool-calling loop for every provider:

	provider.start(system_prompt, history)
	response = provider.complete(tools, allow_tools)   # appends the assistant turn
	provider.add_tool_results(results)           # appends the tool results
"""

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Claude models that accept server-side refusal fallbacks (`fallbacks: "default"`).
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Model IDs that can't be used for chat, hidden from the model list.
NON_CHAT_MODELS = re.compile(
	r"embed|tts|whisper|transcribe|dall-e|image|imagen|veo|sora|moderation|audio|realtime|aqa|"
	r"davinci|babbage|computer-use|search",
	re.IGNORECASE,
)
# Claude models that reject `output_config.effort`.
CLAUDE_NO_EFFORT = re.compile(r"haiku|claude-3|sonnet-4-5|sonnet-4-2|opus-4-1|opus-4-2|opus-4-0|sonnet-4-0")


class LLMError(Exception):
	"""Raised when the provider call fails; the message is safe to log, not to show customers."""


@dataclass
class ToolCall:
	id: str
	name: str
	arguments: dict
	parse_error: str | None = None


@dataclass
class LLMResponse:
	text: str = ""
	tool_calls: list[ToolCall] = field(default_factory=list)
	refused: bool = False
	truncated: bool = False


@dataclass
class ToolResult:
	call: ToolCall
	content: str
	is_error: bool = False


def get_provider(settings, api_key=None):
	"""`api_key` overrides the saved key, e.g. one typed in the settings form but not saved yet."""
	if settings.provider == "Anthropic":
		return AnthropicProvider(settings, api_key)
	return OpenAIProvider(settings, api_key)


class BaseProvider:
	def __init__(self, settings, api_key=None):
		self.settings = settings
		self.name = settings.provider
		self.model = settings.model
		self.api_key = api_key or settings.get_password("api_key", raise_exception=False) or ""
		self.max_tokens = settings.max_tokens or 4096
		self.timeout = settings.request_timeout or 60
		self.messages = []

	@contextmanager
	def _api_errors(self, not_found):
		"""Turn SDK exceptions (same class names in both SDKs) into LLMError."""
		sdk = self._sdk
		try:
			yield
		except sdk.AuthenticationError:
			raise LLMError(f"{self.name} rejected the API key.")
		except sdk.NotFoundError:
			raise LLMError(not_found)
		except sdk.RateLimitError:
			raise LLMError(f"{self.name} rate limit reached.")
		except sdk.APIStatusError as e:
			raise LLMError(f"{self.name} API error {e.status_code}: {e.message}")
		except sdk.APIConnectionError as e:
			raise LLMError(f"Could not reach {self.name}: {e}")


class AnthropicProvider(BaseProvider):
	def __init__(self, settings, api_key=None):
		super().__init__(settings, api_key)
		import anthropic

		self._sdk = anthropic
		self.client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=2)
		self.system = ""

	def start(self, system_prompt, history):
		# The system prompt is stable per store, so cache it to cut cost on every turn.
		self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
		self.messages = [{"role": m["role"], "content": m["content"]} for m in history]

	def complete(self, tools, allow_tools=True):
		kwargs = {
			"model": self.model,
			"max_tokens": self.max_tokens,
			"system": self.system,
			"messages": self.messages,
		}
		if tools:
			kwargs["tools"] = [
				{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
				for t in tools
			]
			if not allow_tools:
				kwargs["tool_choice"] = {"type": "none"}
		if self.settings.effort and not CLAUDE_NO_EFFORT.search(self.model):
			kwargs["output_config"] = {"effort": self.settings.effort}

		with self._api_errors(f"Anthropic model '{self.model}' was not found."):
			if self.model in CLAUDE_FALLBACK_MODELS:
				# If a safety classifier declines, the API retries on Anthropic's recommended fallback model.
				response = self.client.beta.messages.create(
					betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
				)
			else:
				response = self.client.messages.create(**kwargs)

		if response.stop_reason == "refusal":
			return LLMResponse(refused=True)

		# Echo the assistant turn back unchanged (thinking blocks included) so the tool loop stays valid.
		self.messages.append({"role": "assistant", "content": _echo_claude_content(response.content)})

		result = LLMResponse(truncated=response.stop_reason == "max_tokens")
		for block in response.content:
			if block.type == "text":
				result.text += block.text
			elif block.type == "tool_use":
				result.tool_calls.append(
					ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
				)
		return result

	def list_models(self):
		"""Newest first, as returned by the Models API."""
		with self._api_errors("Anthropic models endpoint was not found."):
			return [
				{"value": m.id, "description": m.display_name} for m in self.client.models.list(limit=100)
			]

	def add_tool_results(self, results):
		# All results for one assistant turn go back in a single user message.
		self.messages.append(
			{
				"role": "user",
				"content": [
					{
						"type": "tool_result",
						"tool_use_id": r.call.id,
						"content": r.content,
						**({"is_error": True} if r.is_error else {}),
					}
					for r in results
				],
			}
		)


def _echo_claude_content(content):
	blocks = [block.to_dict() for block in content]
	# After a fallback, model-internal blocks from before the last `fallback` marker must be dropped.
	last_fallback = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
	if last_fallback < 0:
		return blocks
	internal = {"thinking", "redacted_thinking", "tool_use"}
	return [
		b for i, b in enumerate(blocks) if i > last_fallback or b.get("type") not in internal | {"fallback"}
	]


class OpenAIProvider(BaseProvider):
	"""OpenAI, Google Gemini (via its OpenAI-compatible endpoint) and any OpenAI-compatible server."""

	def __init__(self, settings, api_key=None):
		super().__init__(settings, api_key)
		import openai

		self._sdk = openai
		base_url = None
		if settings.provider == "Google Gemini":
			base_url = GEMINI_BASE_URL
		elif settings.provider == "OpenAI Compatible":
			base_url = settings.base_url
		self.client = openai.OpenAI(
			api_key=self.api_key or "not-needed", base_url=base_url, timeout=self.timeout, max_retries=2
		)

	def start(self, system_prompt, history):
		self.messages = [{"role": "system", "content": system_prompt}]
		self.messages += [{"role": m["role"], "content": m["content"]} for m in history]

	def complete(self, tools, allow_tools=True):
		kwargs = {"model": self.model, "messages": self.messages}
		# OpenAI's reasoning models only accept max_completion_tokens; most compatible servers only max_tokens.
		if self.settings.provider == "OpenAI":
			kwargs["max_completion_tokens"] = self.max_tokens
		else:
			kwargs["max_tokens"] = self.max_tokens
		if tools:
			kwargs["tools"] = [
				{
					"type": "function",
					"function": {
						"name": t["name"],
						"description": t["description"],
						"parameters": t["parameters"],
					},
				}
				for t in tools
			]
			if not allow_tools:
				kwargs["tool_choice"] = "none"

		with self._api_errors(f"{self.name} model '{self.model}' was not found."):
			response = self.client.chat.completions.create(**kwargs)

		if not response.choices:
			raise LLMError(f"{self.name} returned no choices.")

		choice = response.choices[0]
		message = choice.message
		assistant = {"role": "assistant", "content": message.content}
		if message.tool_calls:
			# model_dump keeps provider extras (e.g. Gemini thought signatures) that must be echoed back.
			assistant["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in message.tool_calls]
		self.messages.append(assistant)

		result = LLMResponse(
			text=message.content or "",
			refused=bool(getattr(message, "refusal", None)),
			truncated=choice.finish_reason == "length",
		)
		for tc in message.tool_calls or []:
			call = ToolCall(id=tc.id, name=tc.function.name, arguments={})
			try:
				call.arguments = json.loads(tc.function.arguments or "{}")
			except json.JSONDecodeError as e:
				call.parse_error = f"Invalid JSON arguments: {e}"
			result.tool_calls.append(call)
		return result

	def list_models(self):
		"""Chat models, newest first."""
		with self._api_errors(f"{self.name} does not provide a models list. Type the model ID instead."):
			models = list(self.client.models.list())
		models.sort(key=lambda m: getattr(m, "created", 0) or 0, reverse=True)
		result = []
		for m in models:
			# Gemini lists models as "models/gemini-..."; the chat endpoint takes the bare ID.
			model_id = m.id.removeprefix("models/")
			if not NON_CHAT_MODELS.search(model_id):
				result.append({"value": model_id, "description": getattr(m, "owned_by", None) or ""})
		return result

	def add_tool_results(self, results):
		for r in results:
			self.messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.content})

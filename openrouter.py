import requests
import json
import random
import os
import glob

ICON_DIR = os.path.join(os.path.dirname(__file__), "@@", "models")

def api_key():
	with open(os.path.join(os.path.dirname(__file__), "key.txt"), "r") as f:
		return f.read().strip()


def http_error(response):
	"""Return an {"error": ...} event if `response` is not a usable SSE stream.

	An error body (4xx/5xx) is not SSE, so the parse loop below skips every one
	of its lines and the provider falls through to yield a well-formed result
	with no content. Downstream that is indistinguishable from the model
	replying with nothing: no error block, no exception, no failed request. A
	rejected request (bad history, rate limit, bad key) therefore looked like a
	silent stall that repeated forever, because nothing could surface it.

	Reads a bounded amount of the body so the provider's own diagnostic
	survives, and returns None when the response is fine.
	"""
	if response.status_code == 200:
		return None

	raw = b""
	try:
		for chunk in response.iter_content(1024):
			raw += chunk
			if len(raw) >= 4096:
				break
	except Exception:
		pass
	response.close()

	text = raw.decode("utf-8", "replace").strip()
	try:
		body = json.loads(text)
	except Exception:
		body = {}
	if isinstance(body, dict):
		body = body.get("error") or body
	if not isinstance(body, dict):
		body = {"message": str(body)}

	message = body.get("message") or text[:500] or response.reason or "request failed"
	return {"error": {"message": f"HTTP {response.status_code}: {message}",
			"code": body.get("code") or response.status_code}}


class Model:
	def __init__(self, provider, id, free, **fields):
		self.provider = provider
		self.id = id
		self.free = free
		self.__dict__.update(fields)

	@property
	def brand(self):
		parts = self.id.split('/')
		if parts[0] == "local":
			return "qwen"
		return parts[1]

	@property
	def icon(self):
		matches = glob.glob(os.path.join(ICON_DIR, f"{self.brand}.*"))
		return os.path.basename(matches[0]) if matches else None

	@property
	def price(self):
		fields = ('prompt', 'completion', 'internal_reasoning')
		total = sum(float(self.pricing.get(k) or 0) for k in fields)
		return round(total * 1_000_000, 2)

	def message(self, messages, think, tools):
		real_id = self.id.split('/', 1)[1]
		return self.provider.message(messages, real_id, think, tools)


class OpenRouter:
	timeout = 120

	def __init__(self):
		self._cache = None

	def load_models(self):
		print("\033[94mhttps://openrouter.ai/api/v1/models\033[0m", flush=True)
		headers = {"Authorization": f"Bearer {api_key()}"}
		response = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=30)
		self._cache = response.json()["data"]

	@property
	def models(self):
		if self._cache is None:
			return []
		result = []

		CONDITION_KEYS = {'utc_days', 'utc_start', 'utc_end', 'min_prompt_tokens'}

		for model in self._cache:
			raw_pricing = model.get("pricing", {})

			# Base pricing: all keys except 'overrides'
			base_pricing = {
				k: (None if v in (-1, "-1") else v)
				for k, v in raw_pricing.items()
				if k != 'overrides'
			}

			# Overrides handling
			overrides_raw = raw_pricing.get('overrides', [])
			overrides_conditions = []
			overridden_pricing = []

			for override in overrides_raw:
				cond = {}
				price_ov = {}
				for k, v in override.items():
					if k in CONDITION_KEYS:
						cond[k] = v
					else:
						# Normalize -1 to None for price fields
						price_ov[k] = None if v in (-1, "-1") else v
				overrides_conditions.append(cond)
				overridden_pricing.append(price_ov)

			result.append(Model(self, f"openrouter/{model.get('id')}", model.get("id", "").endswith(":free"),
				name=model.get("name"),
				description=model.get("description"),
				context_length=model.get("context_length"),
				supported_parameters=model.get("supported_parameters"),
				input_modalities=model.get("architecture", {}).get("input_modalities"),
				output_modalities=model.get("architecture", {}).get("output_modalities"),
				pricing=base_pricing,
				overrides=overrides_conditions,
				overridden_pricing=overridden_pricing,
			))

		result.sort(key=lambda m: m.id)
		return result

	@property
	def free_models(self):
		return [m for m in self.models if m.free]

	def message(self, messages, model=None, think=False, tools=None):
		print("\033[94mhttps://openrouter.ai/api/v1/chat/completions\033[0m", flush=True)
		if model is None:
			model = random.choice(self.models).id.split('/', 1)[1]
		url = "https://openrouter.ai/api/v1/chat/completions"
		headers = {
			"Authorization": f"Bearer {api_key()}",
			"Content-Type": "application/json"
		}
		payload = {
			"model": model,
			"messages": messages,
			"reasoning": {"enabled": think},
			"stream": True,
			"stream_options": {"include_usage": True},
		}
		if tools:
			payload["tools"] = tools

		# reasoning_content is DeepSeek's field for replaying thinking on tool
		# calls. OpenRouter uses the `reasoning` field and a delta.reasoning
		# stream, and a strict gateway may reject the unknown key, so drop it.
		payload["messages"] = [{k: v for k, v in m.items() if k != "reasoning_content"}
				for m in payload["messages"]]

		content_parts = []
		reasoning_parts = []
		tool_calls = {}
		usage = {}
		finish_reason = None

		try:
			response = requests.post(url, headers=headers, json=payload, timeout=(10, OpenRouter.timeout), stream=True)
			try:
				failure = http_error(response)
				if failure:
					yield failure
					return
				for raw_line in response.iter_lines():
					if not raw_line:
						continue
					line = raw_line.decode("utf-8")
					if not line.startswith("data: "):
						continue
					data = line[len("data: "):]
					if data == "[DONE]":
						break
					chunk = json.loads(data)
					if "error" in chunk:
						yield {"error": chunk["error"]}
						return
					if chunk.get("usage"):
						usage = chunk["usage"]
					choices = chunk.get("choices") or []
					if not choices:
						continue
					choice = choices[0]
					if choice.get("finish_reason"):
						finish_reason = choice["finish_reason"]
					delta = choice.get("delta") or {}
					if delta.get("reasoning"):
						reasoning_parts.append(delta["reasoning"])
						yield {"delta": "reasoning", "text": delta["reasoning"]}
					if delta.get("content"):
						content_parts.append(delta["content"])
						yield {"delta": "content", "text": delta["content"]}
					if delta.get("tool_calls"):
						for tc in delta["tool_calls"]:
							idx = tc.get("index", 0)
							call = tool_calls.setdefault(idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
							if tc.get("id"):
								call["id"] = tc["id"]
							fn = tc.get("function") or {}
							if fn.get("name"):
								call["function"]["name"] += fn["name"]
							if fn.get("arguments"):
								call["function"]["arguments"] += fn["arguments"]
			finally:
				response.close()
		except Exception as e:
			yield {"error": {"message": f"{type(e).__name__}: {e}", "code": None}}
			return

		message = {"role": "assistant", "content": "".join(content_parts) or None}
		if reasoning_parts:
			message["reasoning"] = "".join(reasoning_parts)
		if tool_calls:
			message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
		result = {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage}
		yield {"result": result}

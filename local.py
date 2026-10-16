import json
import os
import subprocess
import threading
import time
import requests

from openrouter import Model, http_error

## Talks to a llama-server running on the same device (see localhost:8080).
## The provider is deliberately API-compatible with OpenRouter/DeepSeek so it
## can drop into Chat.models alongside them, but a few things differ:
##   - thinking is switched with the chat template kwarg `enable_thinking`
##     rather than OpenRouter's `reasoning.enabled`, and the streamed thinking
##     arrives as `reasoning_content` (llama.cpp's "deepseek" reasoning format)
##   - there is no billing, so usage never carries a cost
##   - the phone this runs on cannot hold more than one model in RAM at once,
##     so only one of the models below is ever actually loaded. Qwen manages
##     a single llama-server subprocess: a message for a model that isn't
##     loaded triggers a (slow, 5-30s) load, swapping out whatever was loaded
##     before. After IDLE_TIMEOUT seconds with no request, the loaded model is
##     shut down to free the RAM; the next request pays the load cost again.

LLAMA_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama-server")
MODELS_DIR = "/data/data/com.termux/files/home/models"
HOST = "127.0.0.1"
PORT = 8080
BASE_URL = f"http://{HOST}:{PORT}"

IDLE_TIMEOUT = 120   # seconds of no requests before the loaded model is shut down
READY_TIMEOUT = 60   # seconds to wait for a freshly-started server to accept requests
WATCHDOG_INTERVAL = 15

MODEL_CONFIGS = [
	{"name": "Qwen2.5-0.5B-Instruct-Q2_K", "file": "Qwen2.5-0.5B-Instruct-Q2_K.gguf", "context_length": 4096},
	{"name": "Qwen3-0.6B-Q4_K_M", "file": "Qwen3-0.6B-Q4_K_M.gguf", "context_length": 4096},
	{"name": "Qwen3.5-0.8B-Q4_K_M", "file": "Qwen3.5-0.8B-Q4_K_M.gguf", "context_length": 4096},
	{"name": "Qwen3.5-2B-Q4_K_M", "file": "Qwen3.5-2B-Q4_K_M.gguf", "context_length": 4096},
	{"name": "Qwen3.5-4B-Q6_K", "file": "Qwen3.5-4B-Q6_K.gguf", "context_length": 4096},
]


class Qwen:
	## Local CPU generation is slow and thinking traces can run for minutes,
	## so this is far more generous than the hosted providers' 120s.
	timeout = 300

	def __init__(self, base_url=BASE_URL):
		self.base_url = base_url.rstrip("/")
		self._lock = threading.Lock()
		self._process = None
		self._loaded_name = None
		self._last_used = 0.0
		self._active = 0
		self._watchdog_started = False

	def load_models(self):
		## The model list is hardcoded (see models property) since only one
		## can ever be running at a time, so there's nothing to query here --
		## just a sanity check that the files this class promises actually exist.
		for cfg in MODEL_CONFIGS:
			path = os.path.join(MODELS_DIR, cfg["file"])
			if not os.path.isfile(path):
				print(f"\033[91mlocal model file missing: {path}\033[0m", flush=True)

	@property
	def models(self):
		result = []
		for cfg in MODEL_CONFIGS:
			result.append(Model(self, f"local/{cfg['name']}", True,
				name=cfg["name"],
				description="Local model, loaded on demand by llama-server",
				context_length=cfg["context_length"],
				supported_parameters=["tools", "reasoning"],
				input_modalities=["text"],
				output_modalities=["text"],
				pricing={},
				overrides=[],
				overridden_pricing=[],
			))
		result.sort(key=lambda m: m.id)
		return result

	@property
	def free_models(self):
		## Everything is local, so nothing bills.
		return self.models

	def _config_for(self, name):
		for cfg in MODEL_CONFIGS:
			if cfg["name"] == name:
				return cfg
		return None

	def _is_running(self):
		return self._process is not None and self._process.poll() is None

	def _stop_locked(self):
		if self._process is not None:
			if self._process.poll() is None:
				print(f"\033[93mstopping local model '{self._loaded_name}'\033[0m", flush=True)
				self._process.terminate()
				try:
					self._process.wait(timeout=10)
				except subprocess.TimeoutExpired:
					self._process.kill()
					self._process.wait(timeout=10)
			self._process = None
			self._loaded_name = None

	def _start_locked(self, cfg):
		model_path = os.path.join(MODELS_DIR, cfg["file"])
		log_path = os.path.join(MODELS_DIR, "server.log")
		log_file = open(log_path, "a")
		print(f"\033[94mloading local model '{cfg['name']}'\033[0m", flush=True)
		self._process = subprocess.Popen(
			[LLAMA_SERVER, "-m", model_path, "-c", str(cfg["context_length"]),
			 "-t", "4", "-np", "1", "--host", HOST, "--port", str(PORT)],
			stdout=log_file, stderr=subprocess.STDOUT,
		)
		self._loaded_name = cfg["name"]
		## Loading the weights takes anywhere from ~5s (0.8B) to ~30s (2B) --
		## poll until the server actually answers instead of guessing a sleep.
		deadline = time.monotonic() + READY_TIMEOUT
		while time.monotonic() < deadline:
			if self._process.poll() is not None:
				raise RuntimeError(f"llama-server exited during startup (see {log_path})")
			try:
				r = requests.get(f"{self.base_url}/v1/models", timeout=2)
				if r.status_code == 200:
					print(f"\033[92mlocal model '{cfg['name']}' ready\033[0m", flush=True)
					return
			except Exception:
				pass
			time.sleep(0.5)
		raise TimeoutError(f"llama-server did not become ready within {READY_TIMEOUT}s")

	def _ensure_loaded(self, name):
		cfg = self._config_for(name)
		if cfg is None:
			raise ValueError(f"unknown local model: {name}")
		with self._lock:
			if self._loaded_name == name and self._is_running():
				self._last_used = time.time()
				return
			self._stop_locked()
			self._start_locked(cfg)
			self._last_used = time.time()
			self._start_watchdog()

	def _start_watchdog(self):
		if self._watchdog_started:
			return
		self._watchdog_started = True
		threading.Thread(target=self._watchdog_loop, daemon=True).start()

	def _watchdog_loop(self):
		while True:
			time.sleep(WATCHDOG_INTERVAL)
			with self._lock:
				if (self._process is not None and self._active == 0
						and time.time() - self._last_used >= IDLE_TIMEOUT):
					print(f"\033[93mlocal model '{self._loaded_name}' idle for {IDLE_TIMEOUT}s\033[0m", flush=True)
					self._stop_locked()

	def message(self, messages, model=None, think=False, tools=None):
		try:
			self._ensure_loaded(model)
		except Exception as e:
			yield {"error": {"message": f"{type(e).__name__}: {e}", "code": None}}
			return

		with self._lock:
			self._active += 1
		try:
			yield from self._stream(messages, model, think, tools)
		finally:
			with self._lock:
				self._active -= 1
				self._last_used = time.time()

	def _stream(self, messages, model, think, tools):
		print(f"\033[94m{self.base_url}/v1/chat/completions\033[0m", flush=True)
		url = f"{self.base_url}/v1/chat/completions"
		payload = {
			"messages": messages,
			"stream": True,
			"stream_options": {"include_usage": True},
			"chat_template_kwargs": {"enable_thinking": bool(think)},
		}
		if model:
			payload["model"] = model
		if tools:
			payload["tools"] = tools

		content_parts = []
		reasoning_parts = []
		tool_calls = {}
		usage = {}
		finish_reason = None

		try:
			response = requests.post(url, json=payload, timeout=(10, Qwen.timeout), stream=True)
			try:
				failure = http_error(response)
				if failure:
					yield failure
					return
				deadline = time.monotonic() + Qwen.timeout
				for raw_line in response.iter_lines():
					if time.monotonic() > deadline:
						raise TimeoutError(f"no complete reply after {Qwen.timeout} seconds")
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
						error = chunk["error"]
						yield {"error": {"message": error.get("message", str(error)), "code": error.get("code")}}
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
					if delta.get("reasoning_content"):
						reasoning_parts.append(delta["reasoning_content"])
						yield {"delta": "reasoning", "text": delta["reasoning_content"]}
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


if __name__ == "__main__":
	qwen = Qwen()
	qwen.load_models()
	print()
	for m in qwen.models:
		print(f"  {m.id}   ctx={m.context_length}   free={m.free}")
	print()

	for cfg in MODEL_CONFIGS:
		print(f"=== message(model={cfg['name']!r}) ===")
		events = qwen.message([{"role": "user", "content": "What is 2+2?"}], cfg["name"], False, None)
		content = 0
		for event in events:
			if event.get("delta") == "content":
				content += len(event["text"])
			elif "result" in event:
				r = event["result"]
				print(f"  content={r['choices'][0]['message']['content']!r}")
				print(f"  finish={r['choices'][0]['finish_reason']}  usage={r['usage'].get('completion_tokens')} completion tokens")
			elif "error" in event:
				print("  error:", event["error"])

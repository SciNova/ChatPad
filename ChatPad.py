from datetime import datetime, timezone, timedelta
import bz2
import csv
import os
import re
import threading
import uuid
import uuid7
import json
import shutil
import time
import markdown
from diff_match_patch import diff_match_patch

from openrouter import OpenRouter
from deepseek import DeepSeek
from local import Qwen
from Tools import Toolbox

MARKDOWN_EXTENSIONS = ['tables', 'fenced_code', 'codehilite', 'nl2br']

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FOLDER = os.path.join(APP_DIR, "sessions")
SESSIONS_CSV = os.path.join(SESSIONS_FOLDER, "sessions.csv")
INDEX_FIELDS = ("uuid", "created_at", "archived", "title", "models")
SESSION_SUFFIX = ".jsonl"

MAX_STEPS = 50
PATCH_INTERVAL = 0.08

class ChatPad:
	def __init__(self):
		self.openrouter = OpenRouter()
		self.deepseek = DeepSeek()
		self.local = Qwen()
		self.toolbox = Toolbox()

	@property
	def models(self):
		return self.openrouter.models + self.deepseek.models + self.local.models

	def model(self, id):
		return next((m for m in self.models if m.id == id), None)

	@property
	def free_models(self):
		return self.openrouter.free_models + self.deepseek.free_models + self.local.free_models

	def list(self, archived=False):
		if not os.path.isfile(SESSIONS_CSV):
			return []
		with open(SESSIONS_CSV, newline="", encoding="utf-8") as f:
			rows = [row for row in csv.DictReader(f) if row["archived"] == ("1" if archived else "0")]
		rows.sort(key=lambda row: row["created_at"], reverse=True)
		return [(row["uuid"], row["created_at"], row["title"], [m for m in row["models"].split("|") if m]) for row in rows]

	def group_by_day(self, archived=False):
		groups = []
		for session in self.list(archived=archived):
			day = session[1][:8]
			if groups and groups[-1][0] == day:
				groups[-1][1].append(session)
			else:
				groups.append((day, [session]))
		return groups

	def archive_all(self):
		archived = 0
		skipped = 0
		for session_id, _, _, _ in self.list(archived=False):
			session = Session(uuid=session_id)
			try:
				session.archive()
				archived += 1
			except ValueError:
				skipped += 1
		return archived, skipped

	def message(self, prompt, model, reasoning=True, session=None, tools=None, mode="auto", pause_requested=None):
		session_obj = Session(uuid=session)
		model_obj = next((m for m in self.models if m.id == model), None)
		if model_obj is None:
			raise ValueError(f"unknown model: {model}")
		active_tools = [t.schema() for t in self.toolbox.tools if tools is not None and t.name in tools]

		msg = Message(session=session_obj)
		msg.uuid = msg.next_uuid()
		turn = MessageTurn(message=msg, uuid=msg.uuid, model=model_obj.id, think=reasoning, prompt=prompt)
		msg.turns.append(turn)
		yield {"type": "prompt", "content": prompt, "model": model_obj.id, "uuid": turn.uuid,
				"timestamp": turn.timestamp, "session": session}
		turn.save()

		should_pause = (lambda: session in pause_requested) if pause_requested is not None else None
		yield from msg.run(model_obj, reasoning, active_tools, tools, mode, self.toolbox, turn, should_pause=should_pause)

	def continue_message(self, session_id, model, reasoning=True, tools=None, mode="auto", pause_requested=None):
		session_obj = Session(uuid=session_id)
		model_obj = next((m for m in self.models if m.id == model), None)
		if model_obj is None:
			raise ValueError(f"unknown model: {model}")
		active_tools = [t.schema() for t in self.toolbox.tools if tools is not None and t.name in tools]

		messages = session_obj.messages
		if not messages:
			return
		msg = messages[-1]
		should_pause = (lambda: session_id in pause_requested) if pause_requested is not None else None
		yield from msg.run(model_obj, reasoning, active_tools, tools, mode, self.toolbox, should_pause=should_pause)

	def resume(self, session_id, action, model, reasoning=True, tools=None, mode="auto"):
		session_obj = Session(uuid=session_id)
		model_obj = next((m for m in self.models if m.id == model), None)
		active_tools = [t.schema() for t in self.toolbox.tools if tools is not None and t.name in tools]

		messages = session_obj.messages
		if not messages:
			return
		msg = messages[-1]
		pending = msg.pending
		if not pending:
			return

		if action == "stop":
			for tool_turn in pending:
				tool_turn.error = {"message": "Stopped by user."}
				tool_turn.save()
			yield {"type": "tool_stopped", "session": session_id}
			return

		for tool_turn in pending:
			if action == "deny":
				tool_turn.result = "User denied this tool call."
			else:
				output, tool_error = self.toolbox.run(tool_turn.tool, tool_turn.arguments, tools or [])
				tool_turn.result = output
				if tool_error:
					tool_turn.error = {"message": tool_error}
			tool_turn.save()
			yield {"type": "tool_result", "tool_call_id": tool_turn.uuid, "name": tool_turn.tool,
					"content": tool_turn.result, "error": tool_turn.error,
					"uuid": tool_turn.uuid, "timestamp": tool_turn.timestamp, "session": session_id}

		if model_obj is None:
			return
		yield from msg.run(model_obj, reasoning, active_tools, tools, mode, self.toolbox)


class Settings:
	def __init__(self, session):
		self.session = session
		record = session.record()
		self.model = record.get("model")
		self.tools = record.get("tools")
		self.mode = record.get("mode")
		self.think = record.get("think")
		self.max_steps = record.get("max_steps")

	def save(self):
		values = {k: v for k, v in self.to_dict().items() if v is not None}
		self.session.set_settings(values)

	def to_dict(self):
		return {"model": self.model, "tools": self.tools,
				"mode": self.mode, "think": self.think, "max_steps": self.max_steps}


class Session:
	def __init__(self, uuid=None):
		self.uuid = uuid

	def __repr__(self):
		return self.timestamp

	@property
	def timestamp(self):
		u = uuid.UUID(self.uuid)
		dt = uuid7.time(u)
		ms = dt.microsecond // 1000
		return dt.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"

	@property
	def path(self):
		return os.path.join(SESSIONS_FOLDER, self.uuid + SESSION_SUFFIX)

	@property
	def archive_path(self):
		return self.path + ".bz2"

	@property
	def archived(self):
		return os.path.isfile(self.archive_path) and not os.path.isfile(self.path)

	def text(self):
		if self.archived:
			with bz2.open(self.archive_path, "rt", encoding="utf-8") as f:
				return f.read()
		elif os.path.isfile(self.path):
			with open(self.path, "r", encoding="utf-8") as f:
				return f.read()
		return ""

	def write_live(self, text):
		os.makedirs(SESSIONS_FOLDER, exist_ok=True)
		tmp = self.path + ".tmp"
		with open(tmp, "w", encoding="utf-8") as f:
			f.write(text)
		os.replace(tmp, self.path)

	def write_archive(self, text):
		os.makedirs(SESSIONS_FOLDER, exist_ok=True)
		tmp = self.archive_path + ".tmp"
		with bz2.open(tmp, "wt", encoding="utf-8") as f:
			f.write(text)
		os.replace(tmp, self.archive_path)

	def record(self):
		text = self.text()
		if not text:
			return {"uuid": self.uuid}
		first_line = text.split("\n", 1)[0]
		try:
			return json.loads(first_line)
		except Exception:
			return {"uuid": self.uuid}

	def set_settings(self, values):
		record = self.record()
		record.update(values)
		record["uuid"] = self.uuid
		rest = self.text().split("\n", 1)
		rest = rest[1] if len(rest) > 1 else ""
		
		new_text = json.dumps(record, separators=(",", ":")) + "\n" + rest
		if self.archived:
			self.write_archive(new_text)
		else:
			self.write_live(new_text)

	@property
	def status(self):
		return self.record().get("status", "idle")

	@status.setter
	def status(self, value):
		self.set_settings({"status": value})

	@property
	def settings(self):
		return Settings(self)

	def unpack(self):
		if not self.archived:
			return
		self.write_live(self.text())
		os.remove(self.archive_path)

	def pack(self):
		if self.archived or not os.path.isfile(self.path):
			return
		self.write_archive(self.text())
		os.remove(self.path)

	def archive(self):
		if self.status == "busy":
			raise ValueError("cannot archive a busy session")
		self.pack()
		self.write_index_row(archived=True)

	def unarchive(self):
		self.unpack()
		self.write_index_row(archived=False)

	def read_index_row(self):
		if not os.path.isfile(SESSIONS_CSV):
			return None
		with open(SESSIONS_CSV, newline="", encoding="utf-8") as f:
			for row in csv.DictReader(f):
				if row["uuid"] == self.uuid:
					return row
		return None

	def write_index_row(self, row=None, archived=None):
		if row is None:
			row = self.read_index_row()
		row = dict(row) if row else {"archived": "0"}
		if archived is not None:
			row["archived"] = "1" if archived else "0"
		row["uuid"] = self.uuid
		if not row.get("created_at"):
			row["created_at"] = self.timestamp
		os.makedirs(SESSIONS_FOLDER, exist_ok=True)
		rows = []
		if os.path.isfile(SESSIONS_CSV):
			with open(SESSIONS_CSV, newline="", encoding="utf-8") as f:
				rows = [{k: r.get(k, "") for k in INDEX_FIELDS} for r in csv.DictReader(f)]
		for i, r in enumerate(rows):
			if r["uuid"] == self.uuid:
				rows[i] = {k: row.get(k, "") for k in INDEX_FIELDS}
				break
		else:
			rows.append({k: row.get(k, "") for k in INDEX_FIELDS})
		tmp = SESSIONS_CSV + "." + str(threading.get_ident()) + ".tmp"
		with open(tmp, "w", newline="", encoding="utf-8") as f:
			writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
			writer.writeheader()
			writer.writerows(rows)
		os.replace(tmp, SESSIONS_CSV)

	def update_index_from(self, message):
		row = self.read_index_row() or {"uuid": self.uuid, "created_at": self.timestamp, "archived": "0"}
		models = set(row.get("models", "").split("|")) - {""}
		changed = False
		if not row.get("title"):
			for turn in message.turns:
				if isinstance(turn, MessageTurn) and turn.prompt:
					prompt = turn.prompt
					row["title"] = prompt[:30] + ("..." if len(prompt) > 30 else "")
					changed = True
					break
		for turn in message.turns:
			if isinstance(turn, MessageTurn) and turn.model and turn.model not in models:
				models.add(turn.model)
				changed = True
		if changed:
			row["models"] = "|".join(sorted(models))
			self.write_index_row(row)
		else:
			self.write_index_row(row)

	@property
	def messages(self):
		messages = []
		lines = self.text().splitlines()
		for line in lines[1:]:
			if not line.strip():
				continue
			try:
				data = json.loads(line)
			except Exception:
				break
			messages.append(Message.from_dict(self, data))
		return messages

	def save_message(self, message):
		if self.archived:
			raise ValueError("cannot save a message on an archived session")
		
		# Fast path for streaming: replace the last line if it matches this message
		if os.path.isfile(self.path):
			with open(self.path, "r+b") as f:
				f.seek(0, 2)
				pos = f.tell()
				if pos > 0:
					f.seek(pos - 1)
					if f.read(1) == b"\n":
						start = pos - 1
						step = 8192
						found = False
						while start > 0:
							step = min(8192, start)
							start -= step
							f.seek(start)
							chunk = f.read(step)
							idx = chunk.rfind(b"\n")
							if idx != -1:
								start = start + idx + 1
								found = True
								break
						if found:
							f.seek(start)
							last_line = f.read().decode("utf-8")
							if f'"{message.uuid}"' in last_line:
								new_line = json.dumps(message.to_dict(), separators=(",", ":")) + "\n"
								f.seek(start)
								f.write(new_line.encode("utf-8"))
								f.truncate()
								self.update_index_from(message)
								return

		# Slow path: full rewrite
		lines = []
		if os.path.isfile(self.path):
			with open(self.path, "r", encoding="utf-8") as f:
				lines = f.read().splitlines()

		record = self.record()
		if not lines:
			lines = [json.dumps(record, separators=(",", ":"))]

		others = []
		for line in lines[1:]:
			if not line.strip():
				continue
			try:
				data = json.loads(line)
			except Exception:
				continue
			if data.get("uuid") != message.uuid:
				others.append(data)

		others.append(message.to_dict())
		others.sort(key=lambda m: m["uuid"])

		new_text = lines[0] + "\n"
		for m in others:
			new_text += json.dumps(m, separators=(",", ":")) + "\n"

		self.write_live(new_text)
		self.update_index_from(message)

	@property
	def blocks(self):
		return [b for m in self.messages for b in m.blocks]

	@property
	def models(self):
		models = []
		for message in self.messages:
			for turn in message.turns:
				if isinstance(turn, MessageTurn) and turn.model and turn.model not in models:
					models.append(turn.model)
		return models

	def history(self, think=False):
		history = []
		for message in self.messages:
			if message.hidden:
				continue
			blocks = message.blocks
			types = {b["type"] for b in blocks}
			if "error" in types and not types & {"response", "tool_call"}:
				continue
			reasoning_by_uuid = {b["uuid"]: b["content"] for b in blocks
					if b["type"] == "reasoning" and b.get("content")}
			entries = []
			skip_round = False
			for b in blocks:
				t = b["type"]
				if t == "prompt":
					skip_round = False
					entries.append({"role": "user", "content": b["content"]})
				elif t == "tool_call":
					skip_round = False
					reasoning = reasoning_by_uuid.get(b["uuid"]) if think else None
					if think and not reasoning:
						skip_round = True
						continue
					entry = {"role": "assistant", "content": b.get("content"), "tool_calls": b["tool_calls"]}
					if reasoning:
						entry["reasoning_content"] = reasoning
					entries.append(entry)
				elif t == "tool_result":
					if skip_round:
						continue
					entries.append({"role": "tool", "tool_call_id": b["tool_call_id"], "content": b["content"]})
				elif t == "response" and b.get("content"):
					skip_round = False
					entries.append({"role": "assistant", "content": b["content"]})
			while entries and entries[-1]["role"] == "tool":
				entries.pop()
			if entries and entries[-1]["role"] == "assistant" and entries[-1].get("tool_calls"):
				entries.pop()
			history.extend(entries)
		return history


class Message:
	def __init__(self, uuid=None, session=None, hidden=False, input_tokens=None, output_tokens=None):
		self.uuid = uuid
		self.session = session
		self.hidden = hidden
		self.input_tokens = input_tokens
		self.output_tokens = output_tokens
		self.turns = []

	def to_dict(self):
		return {"type": "message", "uuid": self.uuid, "session": self.session.uuid, "hidden": self.hidden,
				"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
				"turns": [t.to_dict() for t in self.turns]}

	@staticmethod
	def from_dict(session, data):
		message = Message(uuid=data.get("uuid"), session=session, hidden=data.get("hidden", False),
				input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"))
		message.turns = [Turn.load(message, t) for t in data.get("turns", [])]
		return message

	def save(self):
		self.session.save_message(self)

	def next_uuid(self):
		dt = datetime.now(timezone.utc)
		if self.turns and self.turns[-1].uuid:
			last = uuid7.time(uuid.UUID(self.turns[-1].uuid))
			if dt < last + timedelta(milliseconds=1):
				dt = last + timedelta(milliseconds=1)
		return str(uuid7.create(dt))

	def hide(self):
		self.hidden = True
		self.save()

	def unhide(self):
		self.hidden = False
		self.save()

	@property
	def pending(self):
		pending = []
		for turn in reversed(self.turns):
			if isinstance(turn, ToolTurn) and turn.result is None and turn.error is None:
				pending.insert(0, turn)
			else:
				break
		return pending or None

	@property
	def blocks(self):
		blocks = []
		turns = self.turns
		i = 0
		while i < len(turns):
			turn = turns[i]
			i += 1
			if not isinstance(turn, MessageTurn):
				continue
			if turn.prompt is not None:
				blocks.append({"type": "prompt", "content": turn.prompt, "model": turn.model,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "hidden": self.hidden, "session": self.session.uuid})
			if turn.reasoning:
				blocks.append({"type": "reasoning", "content": turn.reasoning,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": self.session.uuid})
			if turn.error:
				blocks.append({"type": "error", "error": turn.error, "model": turn.model,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": self.session.uuid})
				continue
			run = []
			while i < len(turns) and isinstance(turns[i], ToolTurn):
				run.append(turns[i])
				i += 1
			if run:
				blocks.append({"type": "tool_call", "content": turn.response,
						"tool_calls": [{"id": t.uuid, "type": "function", "function": {"name": t.tool, "arguments": t.arguments}} for t in run],
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": self.session.uuid})
				for t in run:
					blocks.append({"type": "tool_result", "tool_call_id": t.uuid, "name": t.tool,
							"content": t.result, "error": t.error,
							"uuid": t.uuid, "timestamp": t.timestamp, "session": self.session.uuid})
			elif turn.response:
				usage = (turn.raw or {}).get("usage") or {}
				blocks.append({"type": "response", "content": turn.response, "model": turn.model,
						"cost": usage.get("cost"), "usage": usage,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": self.session.uuid})
		return blocks

	def run(self, model, reasoning, active_tools, tool_names, mode, toolbox, turn=None, should_pause=None):
		session_uuid = self.session.uuid
		for step in range(MAX_STEPS):
			if turn is None:
				turn = MessageTurn(message=self, uuid=self.next_uuid(), model=model.id, think=reasoning)
				self.turns.append(turn)
			reasoning_parts = []
			content_parts = []
			reasoning_html = ""
			content_html = ""
			tool_calls = None
			finish_reason = None
			usage = {}
			error = None
			dmp = diff_match_patch()
			last_flush = time.monotonic()

			def flush(target):
				nonlocal reasoning_html, content_html
				parts = reasoning_parts if target == "reasoning" else content_parts
				old_html = reasoning_html if target == "reasoning" else content_html
				new_html = Turn.render_markdown("".join(parts))
				if new_html == old_html:
					return None
				if target == "reasoning":
					reasoning_html = new_html
					turn.reasoning = "".join(parts)
				else:
					content_html = new_html
					turn.response = "".join(parts)
				turn.save()
				return {"type": "html_patch", "target": target,
						"patch": dmp.patch_toText(dmp.patch_make(old_html, new_html)), "session": session_uuid}

			for event in model.message(self.session.history(reasoning), reasoning, active_tools):
				if "error" in event:
					error = event["error"]
					break
				kind = event.get("delta")
				if kind == "reasoning":
					reasoning_parts.append(event["text"])
					if time.monotonic() - last_flush >= PATCH_INTERVAL:
						last_flush = time.monotonic()
						patch = flush("reasoning")
						if patch:
							yield patch
				elif kind == "content":
					content_parts.append(event["text"])
					if time.monotonic() - last_flush >= PATCH_INTERVAL:
						last_flush = time.monotonic()
						patch = flush("response")
						if patch:
							yield patch
				elif "result" in event:
					result = event["result"]
					message = result["choices"][0]["message"]
					finish_reason = result["choices"][0].get("finish_reason")
					usage = result.get("usage") or {}
					tool_calls = message.get("tool_calls")

			if error:
				if reasoning_parts:
					turn.reasoning = "".join(reasoning_parts)
					turn.save()
					yield {"type": "reasoning", "content": turn.reasoning,
							"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
				if content_parts:
					turn.response = "".join(content_parts)
					turn.save()
					yield {"type": "response", "content": turn.response, "model": model.id,
							"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
				turn.error = error
				turn.save()
				yield {"type": "error", "error": error, "model": model.id,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
				return

			if reasoning_parts:
				turn.reasoning = "".join(reasoning_parts)
				turn.save()
				yield {"type": "reasoning", "content": turn.reasoning,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}

			if tool_calls:
				for call in tool_calls:
					try:
						json.loads(call["function"]["arguments"])
					except Exception:
						call["function"]["arguments"] = "{}"
				if content_parts:
					patch = flush("response")
					if patch:
						yield patch
					yield {"type": "finalize_block", "target": "response", "session": session_uuid}
				turn.response = "".join(content_parts) or None
				turn.save()

				run_turns = []
				for call in tool_calls:
					tool_turn = ToolTurn(message=self, uuid=self.next_uuid(), tool=call["function"]["name"], arguments=call["function"]["arguments"])
					self.turns.append(tool_turn)
					run_turns.append(tool_turn)
					tool_turn.save()

				yield {"type": "tool_call", "content": turn.response,
						"tool_calls": [{"id": t.uuid, "type": "function", "function": {"name": t.tool, "arguments": t.arguments}} for t in run_turns],
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}

				if mode == "manual":
					yield {"type": "await_approval", "session": session_uuid}
					return

				for tool_turn in run_turns:
					output, tool_error = toolbox.run(tool_turn.tool, tool_turn.arguments, tool_names or [])
					tool_turn.result = output
					if tool_error:
						tool_turn.error = {"message": tool_error}
					tool_turn.save()
					yield {"type": "tool_result", "tool_call_id": tool_turn.uuid, "name": tool_turn.tool,
							"content": tool_turn.result, "error": tool_turn.error,
							"uuid": tool_turn.uuid, "timestamp": tool_turn.timestamp, "session": session_uuid}
				turn = None
				if should_pause and should_pause():
					yield {"type": "paused", "session": session_uuid}
					return
				continue

			if not content_parts:
				turn.save()
				yield {"type": "end", "model": model.id,
						"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
				return

			turn.response = "".join(content_parts)
			raw = json.loads(json.dumps(result))
			raw_message = raw["choices"][0]["message"]
			for key in ("content", "reasoning", "reasoning_content"):
				if isinstance(raw_message.get(key), str):
					raw_message[key] = ""
			turn.raw = raw
			self.input_tokens = usage.get("prompt_tokens")
			self.output_tokens = usage.get("completion_tokens")
			turn.save()
			yield {"type": "response", "content": turn.response, "model": model.id,
					"cost": usage.get("cost"), "usage": usage,
					"context_length": model.context_length,
					"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
			return

		turn = MessageTurn(message=self, uuid=self.next_uuid(), model=model.id,
				error={"message": f"Stopped after {MAX_STEPS} steps", "code": None})
		self.turns.append(turn)
		turn.save()
		yield {"type": "error", "error": turn.error, "model": model.id,
				"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}


class Turn:
	def __init__(self, message=None, uuid=None, error=None, hidden=False):
		self.message = message
		self.uuid = uuid
		self.error = error
		self.hidden = hidden

	@property
	def timestamp(self):
		u = uuid.UUID(self.uuid)
		dt = uuid7.time(u)
		ms = dt.microsecond // 1000
		return dt.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"

	@property
	def session(self):
		return self.message.session

	def save(self):
		if self.uuid is None:
			self.uuid = str(uuid7.create(datetime.now(timezone.utc)))
		self.message.save()

	@staticmethod
	def load(message, data):
		if "tool" in data:
			return ToolTurn.from_dict(message, data)
		return MessageTurn.from_dict(message, data)

	@staticmethod
	def render_markdown(text):
		if text is None:
			return ""
		return markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)


class MessageTurn(Turn):
	def __init__(self, message=None, uuid=None, model=None, think=False, prompt=None, reasoning=None,
			response=None, error=None, raw=None, hidden=False):
		super().__init__(message=message, uuid=uuid, error=error, hidden=hidden)
		self.model = model
		self.think = think
		self.prompt = prompt
		self.reasoning = reasoning
		self.response = response
		self.raw = raw

	def to_dict(self):
		return {"uuid": self.uuid, "message": self.message.uuid, "hidden": self.hidden, "error": self.error,
				"model": self.model, "think": self.think, "prompt": self.prompt, "reasoning": self.reasoning,
				"response": self.response, "raw": self.raw}

	@staticmethod
	def from_dict(message, data):
		return MessageTurn(message=message, uuid=data.get("uuid"), model=data.get("model"),
				think=data.get("think", False), prompt=data.get("prompt"), reasoning=data.get("reasoning"),
				response=data.get("response"), error=data.get("error"), raw=data.get("raw"), hidden=data.get("hidden", False))


class ToolTurn(Turn):
	def __init__(self, message=None, uuid=None, tool=None, arguments=None,
			result=None, error=None, hidden=False):
		super().__init__(message=message, uuid=uuid, error=error, hidden=hidden)
		self.tool = tool
		self.arguments = arguments
		self.result = result

	def to_dict(self):
		return {"uuid": self.uuid, "message": self.message.uuid, "hidden": self.hidden, "error": self.error,
				"tool": self.tool, "arguments": self.arguments, "result": self.result}

	@staticmethod
	def from_dict(message, data):
		return ToolTurn(message=message, uuid=data.get("uuid"), tool=data.get("tool"), arguments=data.get("arguments"),
				result=data.get("result"), error=data.get("error"), hidden=data.get("hidden", False))
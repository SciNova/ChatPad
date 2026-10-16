from datetime import datetime, timezone, timedelta
import csv
import os
import re
import threading
import uuid
import uuid7
import json
import tarfile
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import ast
import math
import operator
import difflib
import requests
import markdown
from diff_match_patch import diff_match_patch

from openrouter import OpenRouter
from deepseek import DeepSeek
from local import Qwen

MARKDOWN_EXTENSIONS = ['tables', 'fenced_code', 'codehilite', 'nl2br']

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FOLDER = os.path.join(APP_DIR, "sessions")
SESSIONS_CSV = os.path.join(SESSIONS_FOLDER, "sessions.csv")
INDEX_FIELDS = ("uuid", "created_at", "archived", "title", "models")
SESSION_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$')
MESSAGE_PATTERN = re.compile(r'^([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12})\.json$')

MAX_STEPS = 50
PATCH_INTERVAL = 0.08

SYSTEM_DIR = os.getcwd()
COMMAND_TIMEOUT = 30
TODO_FILE = "todo.md"

# tools whose result carries no content, so their block stays collapsed and
# has nothing to expand to
NO_CONTENT_TOOLS = {"rename_file", "copy_file", "delete_file", "make_folder", "remove_folder", "light_on", "light_off"}

TOOL_SUMMARY = {
	"read_file": ("read", "path"),
	"write_file": ("write", "path"),
	"edit_file": ("edit", "path"),
	"edit_file_range": ("replace lines", "path", "start_line", "end_line"),
	"append_file": ("append", "path"),
	"rename_file": ("rename", "path", "new_path"),
	"copy_file": ("copy", "path", "new_path"),
	"get_url": ("get", "url"),
	"download_url": ("download", "url", "path"),
	"delete_file": ("delete", "path"),
	"current_time": ("current time",),
	"calculator": ("calculate", "expression"),
	"todo": ("todo",),
	"run_python": ("run python",),
	"run_command": ("run", "command"),
	"make_folder": ("make folder", "path"),
	"remove_folder": ("remove folder", "path"),
	"light_status": ("light status",),
	"light_on": ("turn light on",),
	"light_off": ("turn light off",),
}


def tool_summary(name, arguments=None):
	spec = TOOL_SUMMARY.get(name)
	if spec is None:
		return name
	verb, *keys = spec
	if not keys:
		return verb
	try:
		args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
	except Exception:
		args = {}
	if len(keys) == 1:
		return f"{verb} {args.get(keys[0], '?')}"
	return f"{verb} {args.get(keys[0], '?')} to {args.get(keys[1], '?')}"

CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
CALC_NAMES = {"pi": math.pi, "e": math.e}
CALC_FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil")}
CALC_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max})


class ToolError(Exception):
	pass


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
		with open(SESSIONS_CSV, newline="", encoding="utf-8") as f:
			rows = [row for row in csv.DictReader(f)
					if row["archived"] == ("1" if archived else "0")]
		rows.sort(key=lambda row: row["created_at"], reverse=True)
		return [(row["uuid"], row["created_at"], row["title"],
				[m for m in row["models"].split("|") if m]) for row in rows]

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
				skipped += 1  # busy sessions can't be packed mid-stream
		return archived, skipped

	def message(self, prompt, model, reasoning=True, session=None, tools=None, mode="auto", pause_requested=None):
		session_obj = Session(uuid=session)
		model_obj = next((m for m in self.models if m.id == model), None)
		if model_obj is None:
			raise ValueError(f"unknown model: {model}")
		active_tools = [t.schema for t in self.toolbox.tools if tools is not None and t.name in tools]

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
		active_tools = [t.schema for t in self.toolbox.tools if tools is not None and t.name in tools]

		messages = session_obj.messages
		if not messages:
			return
		msg = messages[-1]
		should_pause = (lambda: session_id in pause_requested) if pause_requested is not None else None
		yield from msg.run(model_obj, reasoning, active_tools, tools, mode, self.toolbox, should_pause=should_pause)

	def resume(self, session_id, action, model, reasoning=True, tools=None, mode="auto"):
		session_obj = Session(uuid=session_id)
		model_obj = next((m for m in self.models if m.id == model), None)
		active_tools = [t.schema for t in self.toolbox.tools if tools is not None and t.name in tools]

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
		self.model = session.get_setting("model")
		self.tools = session.get_setting("tools")
		self.mode = session.get_setting("mode")
		self.think = session.get_setting("think")
		self.max_steps = session.get_setting("max_steps")

	def save(self):
		for key in ("model", "tools", "mode", "think", "max_steps"):
			value = getattr(self, key)
			if value is not None:
				self.session.set_setting(key, value)

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
	def archived(self):
		return os.path.isfile(self.archive_path) and not os.path.isdir(self.folder)

	@property
	def folder(self):
		return os.path.join(SESSIONS_FOLDER, self.uuid)

	@property
	def archive_path(self):
		return self.folder + ".tar.bz2"

	@property
	def status_path(self):
		return os.path.join(self.folder, "session.json")

	def get_setting(self, key, default=None):
		if not os.path.isfile(self.status_path):
			return default
		with open(self.status_path) as f:
			return json.load(f).get(key, default)

	def set_setting(self, key, value):
		os.makedirs(self.folder, exist_ok=True)
		data = {}
		if os.path.isfile(self.status_path):
			with open(self.status_path) as f:
				data = json.load(f)
		data[key] = value
		tmp = self.status_path + "." + str(threading.get_ident()) + ".tmp"
		with open(tmp, "w") as f:
			json.dump(data, f)
		os.replace(tmp, self.status_path)

	@property
	def status(self):
		return self.get_setting("status", "idle")

	@status.setter
	def status(self, value):
		self.set_setting("status", value)

	@property
	def settings(self):
		return Settings(self)

	def unpack(self):
		if not self.archived:
			return
		os.makedirs(self.folder, exist_ok=True)
		with tarfile.open(self.archive_path, "r:bz2") as tar:
			tar.extractall(self.folder)
		os.remove(self.archive_path)

	def pack(self):
		if self.archived or not os.path.isdir(self.folder):
			return
		tmp = self.archive_path + ".tmp"
		with tarfile.open(tmp, "w:bz2") as tar:
			for name in sorted(os.listdir(self.folder)):
				tar.add(os.path.join(self.folder, name), arcname=name)
		os.replace(tmp, self.archive_path)
		shutil.rmtree(self.folder)

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
			# Title and models already current; still make sure the row exists
			# (first save on a fresh install has nothing to change).
			self.write_index_row(row)

	@property
	def messages(self):
		if self.archived:
			return self.archived_messages()
		if not os.path.isdir(self.folder):
			return []
		filenames = sorted(f for f in os.listdir(self.folder) if MESSAGE_PATTERN.match(f))
		messages = []
		for filename in filenames:
			with open(os.path.join(self.folder, filename)) as f:
				data = json.load(f)
			messages.append(Message.from_dict(self, data))
		return messages

	def archived_messages(self):
		with tarfile.open(self.archive_path, "r:bz2") as tar:
			members = sorted((m for m in tar.getmembers() if MESSAGE_PATTERN.match(m.name)),
					key=lambda m: m.name)
			messages = [Message.from_dict(self, json.load(tar.extractfile(m))) for m in members]
		return messages

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
				continue  # the prompt got no answer, don't send it
			# DeepSeek's thinking mode rejects any replayed assistant message that
			# carries tool_calls unless it also carries the reasoning_content that
			# produced them: "The `reasoning_content` in the thinking mode must be
			# passed back to the API" (HTTP 400). Reasoning blocks share their turn
			# uuid with the tool_call block, so the two can be paired up here.
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
						# The model called a tool without emitting any reasoning.
						# There is nothing to pass back, so this round cannot be
						# replayed in thinking mode. Drop the call together with
						# its results: a tool result with no call is invalid too.
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
			# A tool call whose follow-up reply came back empty never got closed
			# out by a response or another tool call; drop that trailing,
			# unfinished round so it doesn't sit there unanswered forever.
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

	@property
	def path(self):
		return os.path.join(self.session.folder, f"{self.uuid}.json")

	def to_dict(self):
		return {"uuid": self.uuid, "session": self.session.uuid, "hidden": self.hidden,
				"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
				"turns": [t.to_dict() for t in self.turns]}

	@staticmethod
	def from_dict(session, data):
		message = Message(uuid=data.get("uuid"), session=session, hidden=data.get("hidden", False),
				input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"))
		message.turns = [Turn.load(message, t) for t in data.get("turns", [])]
		return message

	def save(self):
		if self.session.archived:
			raise ValueError("cannot save a message on an archived session")
		os.makedirs(self.session.folder, exist_ok=True)
		tmp = self.path + ".tmp"
		with open(tmp, "w") as f:
			json.dump(self.to_dict(), f, indent=2)
		os.replace(tmp, self.path)
		self.session.update_index_from(self)

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
						"labels": {t.uuid: tool_summary(t.tool, t.arguments) for t in run},
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
				# Re-rendering the whole accumulated text and diffing it is O(length),
				# so doing it on every token makes a long reply quadratic. Rate-limit
				# it instead; each flush also persists the turn, so a reload loses at
				# most the unflushed tail.
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
				# Preserve partial content (reasoning and response) even when an error occurs
				# This allows the frontend to display what was generated before the error
				if reasoning_parts:
					# Don't discard reasoning - keep it for display
					turn.reasoning = "".join(reasoning_parts)
					turn.save()
					yield {"type": "reasoning", "content": turn.reasoning,
							"uuid": turn.uuid, "timestamp": turn.timestamp, "session": session_uuid}
				
				if content_parts:
					# Don't discard response - keep it for display
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
						"labels": {t.uuid: tool_summary(t.tool, t.arguments) for t in run_turns},
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


class Tool:
	def __init__(self, name, label, description, properties, handler, required=None):
		self.name = name
		self.label = label
		self.description = description
		self.properties = properties
		self.required = list(properties) if required is None else required
		self.handler = handler

	@property
	def schema(self):
		return {"type": "function", "function": {"name": self.name, "description": self.description,
				"parameters": {"type": "object", "properties": self.properties, "required": self.required}}}

	def run(self, arguments):
		try:
			args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
			return self.handler(**args), None
		except ToolError as e:
			return str(e), str(e)
		except Exception as e:
			message = f"error: {type(e).__name__}: {e}"
			return message, message


class Toolbox:
	def __init__(self):
		self.tools = [
			Tool("read_file", "Read file", "Read a file, or list a folder.",
					{"path": {"type": "string"}}, self.read_file),
			Tool("write_file", "Write file", "Create a file, or replace it if it exists.",
					{"path": {"type": "string"}, "content": {"type": "string"}}, self.write_file),
			Tool("edit_file", "Edit file", "Replace one piece of text in a file. `old` must appear exactly once; include enough surrounding text to make it unique. Read the file first.",
					{"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, self.edit_file),
			Tool("edit_file_range", "Replace lines", "Replace an inclusive, 1-based range of lines with new text. Use this instead of edit_file when the change spans whole lines.",
					{"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "new": {"type": "string"}}, self.edit_file_range),
			Tool("append_file", "Append file", "Add text to the end of a file, creating it if needed.",
					{"path": {"type": "string"}, "content": {"type": "string"}}, self.append_file),
			Tool("rename_file", "Rename file", "Rename a file. Fails if the new name already exists.",
					{"path": {"type": "string"}, "new_path": {"type": "string"}}, self.rename_file),
			Tool("copy_file", "Copy file", "Copy a file. Fails if the new name already exists.",
					{"path": {"type": "string"}, "new_path": {"type": "string"}}, self.copy_file),
			Tool("get_url", "Get URL", "Fetch an http or https URL and return the content as text.",
					{"url": {"type": "string"}}, self.get_url),
			Tool("download_url", "Download URL", "Download an http or https URL and save it to a path exactly as downloaded.",
					{"url": {"type": "string"}, "path": {"type": "string"}}, self.download_url),
			Tool("delete_file", "Delete file", "Delete a file.",
					{"path": {"type": "string"}}, self.delete_file),
			Tool("current_time", "Current time", "Get the current date and time on the server.",
					{}, self.current_time),
			Tool("calculator", "Calculator", "Evaluate an arithmetic expression exactly. Supports + - * / // % **, parentheses, pi, e, and sqrt sin cos tan log log10 exp floor ceil abs round min max.",
					{"expression": {"type": "string"}}, self.calculator),
			Tool("todo", "Todo", "Keep a todo list (stored in todo.md). Actions: add (item is the text), done (item is the number), remove (item is the number), list. Returns the updated list.",
					{"action": {"type": "string", "enum": ["add", "done", "remove", "list"]}, "item": {"type": "string"}}, self.todo, required=["action"]),
			Tool("run_python", "Run Python", "Run Python 3 code and return what it prints, including errors. Each call is a fresh process, so print anything you want to see.",
					{"code": {"type": "string"}}, self.run_python),
			Tool("run_command", "Run command", "Run a shell command and return its output, including errors. Each call is a fresh shell, so cd does not carry over; pipes and && work.",
					{"command": {"type": "string"}}, self.run_command),
			Tool("make_folder", "Make folder", "Create a folder, including any missing parent folders.",
					{"path": {"type": "string"}}, self.make_folder),
			Tool("remove_folder", "Remove folder", "Remove an empty folder.",
					{"path": {"type": "string"}}, self.remove_folder),
			Tool("light_status", "Light status", "Get whether the light is currently on.",
					{}, self.light_status),
			Tool("light_on", "Light on", "Turn the light on.",
					{}, self.light_on),
			Tool("light_off", "Light off", "Turn the light off.",
					{}, self.light_off),
		]
		self._light_state = False

	def find(self, name):
		return next((t for t in self.tools if t.name == name), None)

	def run(self, name, arguments, allowed):
		if name not in allowed:
			message = f"tool not enabled: {name}"
			return message, message
		tool = self.find(name)
		if tool is None:
			message = f"unknown tool: {name}"
			return message, message
		return tool.run(arguments)

	def _resolve(self, path):
		path = path.lstrip("~").lstrip("/")
		if path in ("", "."):
			return SYSTEM_DIR
		return os.path.normpath(os.path.join(SYSTEM_DIR, path))

	def read_file(self, path):
		full = self._resolve(path)
		if os.path.isdir(full):
			return "\n".join(sorted(os.listdir(full)))
		with open(full, "r", encoding="utf-8") as f:
			return f.read()

	def write_file(self, path, content):
		full = self._resolve(path)
		with open(full, "w", encoding="utf-8") as f:
			f.write(content)
		return f"wrote {len(content)} characters to {path}"

	def edit_file(self, path, old, new):
		full = self._resolve(path)
		with open(full, "r", encoding="utf-8") as f:
			text = f.read()
		count = text.count(old)
		if count == 0:
			lines = text.splitlines()
			best_ratio, best_line, best_text = 0.0, None, ""
			for number, line in enumerate(lines, 1):
				ratio = difflib.SequenceMatcher(None, old.strip(), line.strip()).quick_ratio()
				if ratio > best_ratio:
					best_ratio, best_line, best_text = ratio, number, line
			if best_line is not None and best_ratio > 0.6:
				raise ToolError(f"text not found in {path}. Closest match is line {best_line}: `{best_text.strip()}`. Read the file and copy the exact text for `old`.")
			raise ToolError(f"text not found in {path}. Read the file and copy the exact text for `old`.")
		if count > 1:
			locations = []
			start = 0
			while True:
				index = text.find(old, start)
				if index == -1:
					break
				locations.append(text.count("\n", 0, index) + 1)
				start = index + 1
			raise ToolError(f"text found {count} times in {path} (lines {', '.join(str(n) for n in locations)}); it must appear exactly once. Include more surrounding text to make it unique, or use edit_file_range.")
		with open(full, "w", encoding="utf-8") as f:
			f.write(text.replace(old, new, 1))
		return f"edited {path}"

	def edit_file_range(self, path, start_line, end_line, new):
		full = self._resolve(path)
		with open(full, "r", encoding="utf-8") as f:
			lines = f.readlines()
		total = len(lines)
		if start_line < 1 or end_line < start_line or end_line > total:
			raise ToolError(f"invalid line range: {path} has {total} lines, got {start_line}-{end_line}")
		newline = "\n"
		for line in lines:
			if line.endswith("\r\n"):
				newline = "\r\n"
			elif line.endswith("\n"):
				newline = "\n"
			break
		body = newline.join(new.split("\n"))
		head = "".join(lines[:start_line - 1])
		tail = "".join(lines[end_line:])
		if tail and body and not body.endswith("\n"):
			body += newline
		with open(full, "w", encoding="utf-8") as f:
			f.write(head + body + tail)
		return f"replaced lines {start_line}-{end_line} in {path}"

	def append_file(self, path, content):
		full = self._resolve(path)
		with open(full, "a", encoding="utf-8") as f:
			f.write(content)
		return f"appended {len(content)} characters to {path}"

	def _move_or_copy(self, path, new_path, action):
		source = self._resolve(path)
		target = self._resolve(new_path)
		if os.path.lexists(target):
			raise ToolError(f"already exists: {new_path}")
		if action == "rename":
			os.rename(source, target)
		else:
			shutil.copyfile(source, target)
		return f"{'renamed' if action == 'rename' else 'copied'} {path} to {new_path}"

	def rename_file(self, path, new_path):
		return self._move_or_copy(path, new_path, "rename")

	def copy_file(self, path, new_path):
		return self._move_or_copy(path, new_path, "copy")

	def get_url(self, url):
		response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ChatPad"}, timeout=20, verify=False)
		if response.status_code != 200:
			raise ToolError(f"HTTP {response.status_code}")
		return response.content.decode("utf-8")

	def download_url(self, url, path):
		response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ChatPad"}, timeout=20, verify=False)
		if response.status_code != 200:
			raise ToolError(f"HTTP {response.status_code}")
		data = response.content
		with open(self._resolve(path), "wb") as f:
			f.write(data)
		return data.decode("utf-8")

	def delete_file(self, path):
		os.remove(self._resolve(path))
		return f"deleted {path}"

	def make_folder(self, path):
		os.makedirs(self._resolve(path), exist_ok=True)
		return f"created folder {path}"

	def remove_folder(self, path):
		os.rmdir(self._resolve(path))
		return f"removed folder {path}"

	def light_status(self):
		return "true" if self._light_state else "false"

	def light_on(self):
		self._light_state = True
		return "light turned on"

	def light_off(self):
		self._light_state = False
		return "light turned off"

	def current_time(self):
		return datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)")

	def _calc(self, node):
		if isinstance(node, ast.Expression):
			return self._calc(node.body)
		if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
			return node.value
		if isinstance(node, ast.Name) and node.id in CALC_NAMES:
			return CALC_NAMES[node.id]
		if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
			value = self._calc(node.operand)
			return value if isinstance(node.op, ast.UAdd) else -value
		if isinstance(node, ast.BinOp) and type(node.op) in CALC_OPS:
			left, right = self._calc(node.left), self._calc(node.right)
			if isinstance(node.op, ast.Pow) and isinstance(left, int) and isinstance(right, int) and right > 0 and left.bit_length() * right > 100000:
				raise ToolError("result too large")
			return CALC_OPS[type(node.op)](left, right)
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CALC_FUNCS and not node.keywords:
			return CALC_FUNCS[node.func.id](*[self._calc(a) for a in node.args])
		raise ToolError("unsupported expression")

	def calculator(self, expression):
		if len(expression) > 500:
			raise ToolError("expression too long")
		return str(self._calc(ast.parse(expression.strip(), mode="eval")))

	def _todo_load(self):
		full = os.path.join(SYSTEM_DIR, TODO_FILE)
		if not os.path.exists(full):
			return []
		items = []
		with open(full, "r", encoding="utf-8") as f:
			for line in f.read().splitlines():
				if line.startswith("- [x] "):
					items.append([True, line[6:]])
				elif line.startswith("- [ ] "):
					items.append([False, line[6:]])
		return items

	def _todo_show(self, items):
		lines = [f"{i}. [{'x' if done else ' '}] {text}" for i, (done, text) in enumerate(items, 1)]
		return "\n".join(lines) or "(empty)"

	def todo(self, action, item=None):
		items = self._todo_load()
		if action == "list":
			return self._todo_show(items)
		if action == "add":
			if not item:
				raise ToolError("item text required")
			items.append([False, str(item).replace("\n", " ")])
		elif action in ("done", "remove"):
			try:
				number = int(item)
			except (TypeError, ValueError):
				raise ToolError("item must be the number from the list")
			if not 1 <= number <= len(items):
				raise ToolError(f"no item {number}")
			if action == "done":
				items[number - 1][0] = True
			else:
				del items[number - 1]
		else:
			raise ToolError(f"unknown action: {action} (add, done, remove, list)")
		os.makedirs(SYSTEM_DIR, exist_ok=True)
		with open(os.path.join(SYSTEM_DIR, TODO_FILE), "w", encoding="utf-8") as f:
			f.write("".join(f"- [{'x' if done else ' '}] {text}\n" for done, text in items))
		return self._todo_show(items)

	def _execute(self, command, shell=False):
		os.makedirs(SYSTEM_DIR, exist_ok=True)
		process = subprocess.Popen(command, shell=shell, cwd=SYSTEM_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
		try:
			stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT)
		except subprocess.TimeoutExpired:
			os.killpg(process.pid, signal.SIGKILL)
			process.communicate()
			raise ToolError(f"timed out after {COMMAND_TIMEOUT} seconds")
		output = stdout
		if stderr:
			output += ("\n" if output else "") + "[stderr]\n" + stderr
		if process.returncode != 0:
			output += f"\n[exit code {process.returncode}]"
		return output or "(no output)"

	def run_python(self, code):
		with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
			f.write(code)
		try:
			return self._execute([sys.executable, f.name])
		finally:
			os.remove(f.name)

	def run_command(self, command):
		return self._execute(command, shell=True)

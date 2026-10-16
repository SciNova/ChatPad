from datetime import datetime
import ast
import difflib
import json
import math
import operator
import os
import shutil
import signal
import subprocess
import sys
import tempfile

import requests

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

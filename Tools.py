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
from datetime import datetime

import requests

COMMAND_TIMEOUT = 30
LABEL_TRUNCATE_LENGTH = 30

CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
			ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
			ast.Mod: operator.mod, ast.Pow: operator.pow}
CALC_NAMES = {"pi": math.pi, "e": math.e}
CALC_FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil")}
CALC_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max})


def truncate(text, length):
	return text if len(text) <= length else text[:length] + "…"


class ToolError(Exception):
	pass


class Tool:
	name = ""
	description = ""
	properties = {}
	required = None
	result = None

	def __init__(self, toolbox):
		self.toolbox = toolbox

	@classmethod
	def schema(cls):
		required = list(cls.properties) if cls.required is None else cls.required
		return {"type": "function", "function": {
			"name": cls.name,
			"description": cls.description,
			"parameters": {"type": "object",
						   "properties": cls.properties,
						   "required": required}}}

	@property
	def summary(self):
		return self.name

	def execute(self):
		raise NotImplementedError

	def run(self):
		try:
			self.result = self.execute()
			return self.result, None
		except ToolError as e:
			self.result = str(e)
			return self.result, str(e)
		except Exception as e:
			self.result = f"error: {type(e).__name__}: {e}"
			return self.result, self.result


class ProcessTool(Tool):
	def run_process(self, command, shell=False):
		process = subprocess.Popen(
			command, shell=shell,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			start_new_session=True,
		)
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


class ReadFileTool(Tool):
	name = "read_file"
	description = ("Read a file, or list a folder. `offset` is the 1-based line "
				   "to start at (default 1); `limit` is the number of lines to "
				   "read (default: the whole file).")
	properties = {"file_path": {"type": "string"},
				  "offset": {"type": "integer"},
				  "limit": {"type": "integer"}}
	required = ["file_path"]

	def __init__(self, toolbox, file_path, offset=1, limit=None):
		super().__init__(toolbox)
		self.file_path = file_path
		self.offset = offset
		self.limit = limit

	@property
	def summary(self):
		return f"read {self.file_path}"

	def execute(self):
		start = max(0, (self.offset or 1) - 1)
		end = None if self.limit is None else start + self.limit
		if os.path.isdir(self.file_path):
			return "\n".join(sorted(os.listdir(self.file_path))[start:end])
		with open(self.file_path, "r", encoding="utf-8") as f:
			lines = f.readlines()
		return "".join(lines[start:end])


class WriteFileTool(Tool):
	name = "write_file"
	description = "Create a file, or replace it if it exists."
	properties = {"file_path": {"type": "string"}, "content": {"type": "string"}}

	def __init__(self, toolbox, file_path, content):
		super().__init__(toolbox)
		self.file_path = file_path
		self.content = content

	@property
	def summary(self):
		return f"write {self.file_path}"

	def execute(self):
		with open(self.file_path, "w", encoding="utf-8") as f:
			f.write(self.content)
		return f"wrote {len(self.content)} characters to {self.file_path}"


class EditFileTool(Tool):
	name = "edit_file"
	description = ("Replace one piece of text in a file. `old` must appear exactly "
				   "once; include enough surrounding text to make it unique. Read "
				   "the file first.")
	properties = {"file_path": {"type": "string"},
				  "old": {"type": "string"},
				  "new": {"type": "string"}}

	def __init__(self, toolbox, file_path, old, new):
		super().__init__(toolbox)
		self.file_path = file_path
		self.old = old
		self.new = new

	@property
	def summary(self):
		return f"edit {self.file_path}"

	def execute(self):
		with open(self.file_path, "r", encoding="utf-8") as f:
			text = f.read()
		count = text.count(self.old)
		if count == 0:
			lines = text.splitlines()
			best_ratio, best_line, best_text = 0.0, None, ""
			for number, line in enumerate(lines, 1):
				ratio = difflib.SequenceMatcher(None, self.old.strip(), line.strip()).quick_ratio()
				if ratio > best_ratio:
					best_ratio, best_line, best_text = ratio, number, line
			if best_line is not None and best_ratio > 0.6:
				raise ToolError(f"text not found in {self.file_path}. Closest match is "
								f"line {best_line}: `{best_text.strip()}`. Read the file "
								f"and copy the exact text for `old`.")
			raise ToolError(f"text not found in {self.file_path}. Read the file and "
							f"copy the exact text for `old`.")
		if count > 1:
			locations = []
			start = 0
			while True:
				index = text.find(self.old, start)
				if index == -1:
					break
				locations.append(text.count("\n", 0, index) + 1)
				start = index + 1
			raise ToolError(f"text found {count} times in {self.file_path} "
							f"(lines {', '.join(str(n) for n in locations)}); it must "
							f"appear exactly once. Include more surrounding text to "
							f"make it unique, or use edit_file_range.")
		with open(self.file_path, "w", encoding="utf-8") as f:
			f.write(text.replace(self.old, self.new, 1))
		return f"edited {self.file_path}"


class EditFileRangeTool(Tool):
	name = "edit_file_range"
	description = ("Replace an inclusive, 1-based range of lines with new text. "
				   "Use this instead of edit_file when the change spans whole lines.")
	properties = {"file_path": {"type": "string"},
				  "start_line": {"type": "integer"},
				  "end_line": {"type": "integer"},
				  "new": {"type": "string"}}

	def __init__(self, toolbox, file_path, start_line, end_line, new):
		super().__init__(toolbox)
		self.file_path = file_path
		self.start_line = start_line
		self.end_line = end_line
		self.new = new

	@property
	def summary(self):
		return f"replace lines {self.start_line}-{self.end_line} in {self.file_path}"

	def execute(self):
		with open(self.file_path, "r", encoding="utf-8") as f:
			lines = f.readlines()
		total = len(lines)
		if self.start_line < 1 or self.end_line < self.start_line or self.end_line > total:
			raise ToolError(f"invalid line range: {self.file_path} has {total} lines, "
							f"got {self.start_line}-{self.end_line}")
		newline = "\n"
		for line in lines:
			if line.endswith("\r\n"):
				newline = "\r\n"
			elif line.endswith("\n"):
				newline = "\n"
			break
		body = newline.join(self.new.split("\n"))
		head = "".join(lines[:self.start_line - 1])
		tail = "".join(lines[self.end_line:])
		if tail and body and not body.endswith("\n"):
			body += newline
		with open(self.file_path, "w", encoding="utf-8") as f:
			f.write(head + body + tail)
		return f"replaced lines {self.start_line}-{self.end_line} in {self.file_path}"


class AppendFileTool(Tool):
	name = "append_file"
	description = "Add text to the end of a file, creating it if needed."
	properties = {"file_path": {"type": "string"}, "content": {"type": "string"}}

	def __init__(self, toolbox, file_path, content):
		super().__init__(toolbox)
		self.file_path = file_path
		self.content = content

	@property
	def summary(self):
		return f"append {self.file_path}"

	def execute(self):
		with open(self.file_path, "a", encoding="utf-8") as f:
			f.write(self.content)
		return f"appended {len(self.content)} characters to {self.file_path}"


class RenameFileTool(Tool):
	name = "rename_file"
	description = "Rename a file. Fails if the new name already exists."
	properties = {"file_path": {"type": "string"},
				  "new_file_path": {"type": "string"}}

	def __init__(self, toolbox, file_path, new_file_path):
		super().__init__(toolbox)
		self.file_path = file_path
		self.new_file_path = new_file_path

	@property
	def summary(self):
		return f"rename {self.file_path} to {self.new_file_path}"

	def execute(self):
		if os.path.lexists(self.new_file_path):
			raise ToolError(f"already exists: {self.new_file_path}")
		os.rename(self.file_path, self.new_file_path)
		return f"renamed {self.file_path} to {self.new_file_path}"


class CopyFileTool(Tool):
	name = "copy_file"
	description = "Copy a file. Fails if the new name already exists."
	properties = {"file_path": {"type": "string"},
				  "new_file_path": {"type": "string"}}

	def __init__(self, toolbox, file_path, new_file_path):
		super().__init__(toolbox)
		self.file_path = file_path
		self.new_file_path = new_file_path

	@property
	def summary(self):
		return f"copy {self.file_path} to {self.new_file_path}"

	def execute(self):
		if os.path.lexists(self.new_file_path):
			raise ToolError(f"already exists: {self.new_file_path}")
		shutil.copyfile(self.file_path, self.new_file_path)
		return f"copied {self.file_path} to {self.new_file_path}"


class GetUrlTool(Tool):
	name = "get_url"
	description = "Fetch an http or https URL and return the content as text."
	properties = {"url": {"type": "string"}}

	def __init__(self, toolbox, url):
		super().__init__(toolbox)
		self.url = url

	@property
	def summary(self):
		return f"get {self.url}"

	def execute(self):
		response = requests.get(self.url, headers={"User-Agent": "Mozilla/5.0 ChatPad"},
								timeout=20, verify=False)
		if response.status_code != 200:
			raise ToolError(f"HTTP {response.status_code}")
		return response.content.decode("utf-8")


class DownloadUrlTool(Tool):
	name = "download_url"
	description = "Download an http or https URL and save it to a path exactly as downloaded."
	properties = {"url": {"type": "string"}, "file_path": {"type": "string"}}

	def __init__(self, toolbox, url, file_path):
		super().__init__(toolbox)
		self.url = url
		self.file_path = file_path

	@property
	def summary(self):
		return f"download {self.url} to {self.file_path}"

	def execute(self):
		response = requests.get(self.url, headers={"User-Agent": "Mozilla/5.0 ChatPad"},
								timeout=20, verify=False)
		if response.status_code != 200:
			raise ToolError(f"HTTP {response.status_code}")
		data = response.content
		with open(self.file_path, "wb") as f:
			f.write(data)
		return data.decode("utf-8")


class DeleteFileTool(Tool):
	name = "delete_file"
	description = "Delete a file."
	properties = {"file_path": {"type": "string"}}

	def __init__(self, toolbox, file_path):
		super().__init__(toolbox)
		self.file_path = file_path

	@property
	def summary(self):
		return f"delete {self.file_path}"

	def execute(self):
		os.remove(self.file_path)
		return f"deleted {self.file_path}"


class CurrentTimeTool(Tool):
	name = "current_time"
	description = "Get the current date and time on the server."
	properties = {}

	@property
	def summary(self):
		return "current time"

	def execute(self):
		return datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)")


class CalculatorTool(Tool):
	name = "calculator"
	description = ("Evaluate an arithmetic expression exactly. Supports + - * / "
				   "// % **, parentheses, pi, e, and sqrt sin cos tan log log10 "
				   "exp floor ceil abs round min max.")
	properties = {"expression": {"type": "string"}}

	def __init__(self, toolbox, expression):
		super().__init__(toolbox)
		self.expression = expression

	@property
	def summary(self):
		return f"calculate {self.expression}"

	def _calc(self, node):
		if isinstance(node, ast.Expression):
			return self._calc(node.body)
		if (isinstance(node, ast.Constant)
				and isinstance(node.value, (int, float))
				and not isinstance(node.value, bool)):
			return node.value
		if isinstance(node, ast.Name) and node.id in CALC_NAMES:
			return CALC_NAMES[node.id]
		if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
			value = self._calc(node.operand)
			return value if isinstance(node.op, ast.UAdd) else -value
		if isinstance(node, ast.BinOp) and type(node.op) in CALC_OPS:
			left, right = self._calc(node.left), self._calc(node.right)
			if (isinstance(node.op, ast.Pow) and isinstance(left, int)
					and isinstance(right, int) and right > 0
					and left.bit_length() * right > 100000):
				raise ToolError("result too large")
			return CALC_OPS[type(node.op)](left, right)
		if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
				and node.func.id in CALC_FUNCS and not node.keywords):
			return CALC_FUNCS[node.func.id](*[self._calc(a) for a in node.args])
		raise ToolError("unsupported expression")

	def execute(self):
		if len(self.expression) > 500:
			raise ToolError("expression too long")
		return str(self._calc(ast.parse(self.expression.strip(), mode="eval")))


class TodoTool(Tool):
	name = "todo"
	description = ("Keep a todo list (stored in todo.md). Actions: add (item is the "
				   "text), done (item is the number), remove (item is the number), "
				   "list. Returns the updated list.")
	properties = {"action": {"type": "string", "enum": ["add", "done", "remove", "list"]},
				  "item": {"type": "string"}}
	required = ["action"]

	TODO_FILE = "todo.md"

	def __init__(self, toolbox, action, item=None):
		super().__init__(toolbox)
		self.action = action
		self.item = item

	@property
	def summary(self):
		return "todo"

	def _load(self):
		if not os.path.exists(self.TODO_FILE):
			return []
		items = []
		with open(self.TODO_FILE, "r", encoding="utf-8") as f:
			for line in f.read().splitlines():
				if line.startswith("- [x] "):
					items.append([True, line[6:]])
				elif line.startswith("- [ ] "):
					items.append([False, line[6:]])
		return items

	def _show(self, items):
		lines = [f"{i}. [{'x' if done else ' '}] {text}"
				 for i, (done, text) in enumerate(items, 1)]
		return "\n".join(lines) or "(empty)"

	def execute(self):
		items = self._load()
		if self.action == "list":
			return self._show(items)
		if self.action == "add":
			if not self.item:
				raise ToolError("item text required")
			items.append([False, str(self.item).replace("\n", " ")])
		elif self.action in ("done", "remove"):
			try:
				number = int(self.item)
			except (TypeError, ValueError):
				raise ToolError("item must be the number from the list")
			if not 1 <= number <= len(items):
				raise ToolError(f"no item {number}")
			if self.action == "done":
				items[number - 1][0] = True
			else:
				del items[number - 1]
		else:
			raise ToolError(f"unknown action: {self.action} (add, done, remove, list)")
		with open(self.TODO_FILE, "w", encoding="utf-8") as f:
			f.write("".join(f"- [{'x' if done else ' '}] {text}\n" for done, text in items))
		return self._show(items)


class RunPythonTool(ProcessTool):
	name = "run_python"
	description = ("Run Python 3 code and return what it prints, including errors. "
				   "Each call is a fresh process, so print anything you want to see.")
	properties = {"code": {"type": "string"}}

	def __init__(self, toolbox, code):
		super().__init__(toolbox)
		self.code = code

	@property
	def summary(self):
		return f"run python {truncate(self.code, LABEL_TRUNCATE_LENGTH)}"

	def execute(self):
		with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
			f.write(self.code)
		try:
			return self.run_process([sys.executable, f.name])
		finally:
			os.remove(f.name)


class RunCommandTool(ProcessTool):
	name = "run_command"
	description = ("Run a shell command and return its output, including errors. "
				   "Each call is a fresh shell, so cd does not carry over; pipes "
				   "and && work.")
	properties = {"command": {"type": "string"}}

	def __init__(self, toolbox, command):
		super().__init__(toolbox)
		self.command = command

	@property
	def summary(self):
		return f"run {truncate(self.command, LABEL_TRUNCATE_LENGTH)}"

	def execute(self):
		return self.run_process(self.command, shell=True)


class MakeFolderTool(Tool):
	name = "make_folder"
	description = "Create a folder, including any missing parent folders."
	properties = {"folder_path": {"type": "string"}}

	def __init__(self, toolbox, folder_path):
		super().__init__(toolbox)
		self.folder_path = folder_path

	@property
	def summary(self):
		return f"make folder {self.folder_path}"

	def execute(self):
		os.makedirs(self.folder_path, exist_ok=True)
		return f"created folder {self.folder_path}"


class RemoveFolderTool(Tool):
	name = "remove_folder"
	description = "Remove an empty folder."
	properties = {"folder_path": {"type": "string"}}

	def __init__(self, toolbox, folder_path):
		super().__init__(toolbox)
		self.folder_path = folder_path

	@property
	def summary(self):
		return f"remove folder {self.folder_path}"

	def execute(self):
		os.rmdir(self.folder_path)
		return f"removed folder {self.folder_path}"


class LightStatusTool(Tool):
	name = "light_status"
	description = "Get whether the light is currently on."
	properties = {}

	@property
	def summary(self):
		return "light status"

	def execute(self):
		return "true" if self.toolbox.light.on else "false"


class LightOnTool(Tool):
	name = "light_on"
	description = "Turn the light on."
	properties = {}

	@property
	def summary(self):
		return "turn light on"

	def execute(self):
		self.toolbox.light.on = True
		return "light turned on"


class LightOffTool(Tool):
	name = "light_off"
	description = "Turn the light off."
	properties = {}

	@property
	def summary(self):
		return "turn light off"

	def execute(self):
		self.toolbox.light.on = False
		return "light turned off"


class Light:
	def __init__(self):
		self.on = False


class Toolbox:
	tools = [
		ReadFileTool, WriteFileTool, EditFileTool, EditFileRangeTool,
		AppendFileTool, RenameFileTool, CopyFileTool, GetUrlTool,
		DownloadUrlTool, DeleteFileTool, CurrentTimeTool, CalculatorTool,
		TodoTool, RunPythonTool, RunCommandTool, MakeFolderTool,
		RemoveFolderTool, LightStatusTool, LightOnTool, LightOffTool,
	]

	def __init__(self):
		self.light = Light()
		self.by_name = {cls.name: cls for cls in self.tools}

	def find(self, name):
		return self.by_name.get(name)

	def build(self, name, arguments):
		cls = self.find(name)
		if cls is None:
			return None
		try:
			args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
		except Exception:
			args = {}
		return cls(self, **args)

	def schemas(self):
		return [cls.schema() for cls in self.tools]

	def schemas_for(self, names):
		if names is None:
			return []
		return [cls.schema() for cls in self.tools if cls.name in names]

	def run(self, name, arguments, allowed):
		if name not in allowed:
			message = f"tool not enabled: {name}"
			return message, message
		tool = self.build(name, arguments)
		if tool is None:
			message = f"unknown tool: {name}"
			return message, message
		return tool.run()

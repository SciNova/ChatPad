import re

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ChatPad import ChatPad, Session as BackendSession
from Tools import tool_summary


def split_lines(fragments):
	"""[(style, text), ...] with embedded '\\n' -> [[(style, text), ...], ...] one list per line."""
	lines = [[]]
	for style, text in fragments:
		parts = text.split("\n")
		for i, part in enumerate(parts):
			if i > 0:
				lines.append([])
			if part:
				lines[-1].append((style, part))
	return lines


def strip_html(html):
	if not html:
		return ""
	text = re.sub(r"<[^>]+>", "", html)
	return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")


# --- Data model: mirrors chatpad.js's Turn/MessageTurn/ToolTurn/Message/Settings/Session ---

class Turn:
	def __init__(self, json, message):
		self.uuid = json.get("uuid")
		self.message = message
		self.hidden = json.get("hidden")
		self.error = json.get("error")


class MessageTurn(Turn):
	def __init__(self, json, message):
		super().__init__(json, message)
		self.model = json.get("model")
		self.think = json.get("think")
		self.prompt = json.get("prompt")
		self.reasoning = json.get("reasoning")
		self.response = json.get("response")
		self.raw = json.get("raw")


class ToolTurn(Turn):
	def __init__(self, json, message):
		super().__init__(json, message)
		self.tool = json.get("tool")
		self.arguments = json.get("arguments")
		self.result = json.get("result")


class Message:
	def __init__(self, json, session):
		self.uuid = json.get("uuid")
		self.session = session
		self.hidden = json.get("hidden")
		self.input_tokens = json.get("input_tokens")
		self.output_tokens = json.get("output_tokens")
		self.turns = [ToolTurn(t, self) if "tool" in t else MessageTurn(t, self) for t in json.get("turns", [])]


class Settings:
	def __init__(self, json, session):
		json = json or {}
		self.session = session
		self.model = json.get("model")
		self.tools = json.get("tools")
		self.mode = json.get("mode")
		self.think = json.get("think")
		self.max_steps = json.get("max_steps")


class Session:
	def __init__(self, json):
		self.uuid = json.get("uuid")
		self.archived = json.get("archived")
		self.status = json.get("status")
		self.settings = Settings(json.get("settings"), self)
		self.messages = [Message(m, self) for m in json.get("messages", [])]


# --- Views: mirrors chatpad.js's SessionView/MessageView/TurnView/ToolTurnView ---
# No streaming/update() here - the session is fetched once and rendered top to bottom.

class SessionView:
	def __init__(self, session):
		self.session = session

	def render(self):
		lines = [("class:day", f"Session {self.session.uuid}  [{self.session.status}]\n")]
		for message in self.session.messages:
			lines += MessageView(message).render()
		return lines


class MessageView:
	def __init__(self, message):
		self.message = message

	def render(self):
		lines = [("", "\n")]
		for i, turn in enumerate(self.message.turns):
			if i > 0:
				lines.append(("", "\n"))
			view = ToolTurnView(turn) if isinstance(turn, ToolTurn) else TurnView(turn)
			lines += view.render()
		return lines


class TurnView:
	def __init__(self, turn):
		self.turn = turn

	def render(self):
		blocks = []
		if self.turn.prompt:
			blocks.append(("class:prompt", f"{self.turn.prompt}\n"))
		if self.turn.reasoning:
			blocks.append(("class:reasoning", strip_html(self.turn.reasoning).strip() + "\n"))
		if self.turn.response:
			blocks.append(("class:response", strip_html(self.turn.response).strip() + "\n"))
		if self.turn.error:
			message = self.turn.error.get("message") if isinstance(self.turn.error, dict) else self.turn.error
			blocks.append(("class:error", f"Error: {message}\n"))

		lines = []
		for block in blocks:
			if lines:
				lines.append(("", "\n"))
			lines.append(block)
		return lines


class ToolTurnView:
	def __init__(self, turn):
		self.turn = turn

	def render(self):
		label = tool_summary(self.turn.tool, self.turn.arguments)
		lines = [("class:tool", f" {label} \n")]
		result = self.turn.result
		if isinstance(result, dict) and result.get("error"):
			lines.append(("class:error", f" {result['error'].get('message')} \n"))
		return lines


class View:
	title = ""
	selectable = False

	def __init__(self, chatpad):
		self.chatpad = chatpad

	def render(self):
		raise NotImplementedError

	def move(self, delta):
		pass

	def open(self):
		return None


class SessionListView(View):
	selectable = True
	archived = False

	def __init__(self, chatpad):
		super().__init__(chatpad)
		self.selected = 0
		self._entries = None

	def entries(self):
		if self._entries is None:
			groups = self.chatpad.group_by_day(archived=self.archived)
			flat = []
			for day, rows in groups:
				flat.append(("day", day, None))
				for session_id, timestamp, title in rows:
					flat.append(("row", (session_id, timestamp, title), None))
			self._entries = flat
		return self._entries

	def move(self, delta):
		rows = [e for e in self.entries() if e[0] == "row"]
		if not rows:
			return
		self.selected = max(0, min(self.selected + delta, len(rows) - 1))

	def open(self):
		rows = [e for e in self.entries() if e[0] == "row"]
		if not rows:
			return None
		session_id, timestamp, title = rows[self.selected][1]
		return session_id

	def render(self):
		lines = []
		rows = [e for e in self.entries() if e[0] == "row"]
		if not rows:
			return [("class:dim", "  (no sessions)")]
		row_index = 0
		for kind, payload, _ in self.entries():
			if kind == "day":
				lines.append(("class:day", f"\n{payload}\n"))
			else:
				session_id, timestamp, title = payload
				prefix = "> " if row_index == self.selected else "  "
				style = "class:selected" if row_index == self.selected else ""
				lines.append((style, f"{prefix}{timestamp[8:10]}:{timestamp[10:12]}  {title}\n"))
				row_index += 1
		return lines


class SessionsView(SessionListView):
	title = "Sessions"
	archived = False


class ArchiveView(SessionListView):
	title = "Archive"
	archived = True


class ModelsView(View):
	title = "Models"

	def render(self):
		models = self.chatpad.models
		free = [m for m in models if m.free]
		other = [m for m in models if not m.free]
		lines = [("class:day", f"\nFree Models ({len(free)})\n")]
		for m in free:
			lines.append(("", f"  {m.name}\n"))
		lines.append(("class:day", f"\nOther Models ({len(other)})\n"))
		for m in other:
			lines.append(("", f"  {m.name}\n"))
		return lines


class DetailView(View):
	title = "Session"

	def __init__(self, chatpad, session_id):
		super().__init__(chatpad)
		self.session_id = session_id
		backend_session = BackendSession(uuid=session_id)
		json = {"uuid": backend_session.uuid, "archived": backend_session.archived, "status": backend_session.status,
				"settings": backend_session.settings.to_dict(),
				"messages": [m.to_dict() for m in backend_session.messages]}
		self.session = Session(json)

	def render(self):
		if self.session is None:
			return [("", f"\n  Session {self.session_id} not found\n")]
		return SessionView(self.session).render()


class App:
	def __init__(self):
		self.chatpad = ChatPad()
		self.chatpad.openrouter.load_models()
		self.chatpad.deepseek.load_models()
		self.chatpad.local.load_models()
		self.nav_views = [SessionsView(self.chatpad), ArchiveView(self.chatpad), ModelsView(self.chatpad)]
		self.nav_index = 0
		self.detail_view = None
		self.scroll_offset = 0
		self.content_window = None

	@property
	def current_view(self):
		return self.detail_view or self.nav_views[self.nav_index]

	def render_nav(self):
		parts = []
		for i, view in enumerate(self.nav_views):
			active = self.detail_view is None and i == self.nav_index
			style = "class:nav-active" if active else "class:nav"
			parts.append((style, f" {view.title} "))
		if self.detail_view is not None:
			parts.append(("class:nav-active", f" {self.detail_view.title} "))
		return parts

	def render_content(self):
		fragments = self.current_view.render()
		if self.detail_view is None:
			return fragments
		lines = split_lines(fragments)
		visible = []
		for i, line in enumerate(lines[self.scroll_offset:]):
			if i > 0:
				visible.append(("", "\n"))
			visible.extend(line)
		return visible

	def scroll(self, delta):
		if self.detail_view is None:
			return
		total_lines = len(split_lines(self.detail_view.render()))
		window_height = total_lines
		if self.content_window is not None and self.content_window.render_info is not None:
			window_height = self.content_window.render_info.window_height
		max_offset = max(0, total_lines - window_height)
		self.scroll_offset = max(0, min(self.scroll_offset + delta, max_offset))

	def switch_nav(self, delta):
		if self.detail_view is not None:
			return
		self.nav_index = (self.nav_index + delta) % len(self.nav_views)

	def move_selection(self, delta):
		self.current_view.move(delta)

	def open_selection(self):
		if self.detail_view is not None:
			return
		view = self.current_view
		if not view.selectable:
			return
		session_id = view.open()
		if session_id:
			self.detail_view = DetailView(self.chatpad, session_id)
			self.scroll_offset = 0

	def go_back(self):
		self.detail_view = None
		self.scroll_offset = 0


def main():
	app = App()
	kb = KeyBindings()

	@kb.add("left")
	@kb.add("h")
	def _(event):
		app.switch_nav(-1)

	@kb.add("right")
	@kb.add("l")
	def _(event):
		app.switch_nav(1)

	@kb.add("up")
	@kb.add("k")
	def _(event):
		if app.detail_view is not None:
			app.scroll(-1)
		else:
			app.move_selection(-1)

	@kb.add("down")
	@kb.add("j")
	def _(event):
		if app.detail_view is not None:
			app.scroll(1)
		else:
			app.move_selection(1)

	@kb.add("enter")
	def _(event):
		app.open_selection()

	@kb.add("escape")
	def _(event):
		if app.detail_view is not None:
			app.go_back()
		else:
			event.app.exit()

	nav_window = Window(content=FormattedTextControl(app.render_nav), height=1, style="class:nav-bar")
	content_window = Window(content=FormattedTextControl(app.render_content), wrap_lines=True)
	app.content_window = content_window

	layout = Layout(HSplit([nav_window, content_window]))

	style = Style.from_dict({
		"nav-bar": "bg:#222222",
		"nav": "#888888",
		"nav-active": "#ffffff bold reverse",
		"day": "#888888 bold",
		"selected": "bg:#444444 #ffffff",
		"dim": "#666666",
		"prompt": "#e5c34a",
		"reasoning": "#7ba8d4 italic",
		"response": "#e0e0e0",
		"tool": "bg:#3a3a3a #e0e0e0",
		"error": "bg:#7a2a20 #ffffff",
	})

	application = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
	application.run()


if __name__ == "__main__":
	main()

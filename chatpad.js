class Tool {
	static name = ''

	get summary() {
		return this.constructor.name
	}
}

class ReadFileTool extends Tool {
	static name = 'read_file'

	constructor({ file_path, offset = 1, limit = null } = {}) {
		super()
		this.file_path = file_path
		this.offset = offset
		this.limit = limit
	}

	get summary() {
		return `read ${this.file_path}`
	}
}

class WriteFileTool extends Tool {
	static name = 'write_file'

	constructor({ file_path, content } = {}) {
		super()
		this.file_path = file_path
		this.content = content
	}

	get summary() {
		return `write ${this.file_path}`
	}
}

class EditFileTool extends Tool {
	static name = 'edit_file'

	constructor({ file_path, old, new: new_ } = {}) {
		super()
		this.file_path = file_path
		this.old = old
		this.new = new_
	}

	get summary() {
		return `edit ${this.file_path}`
	}
}

class EditFileRangeTool extends Tool {
	static name = 'edit_file_range'

	constructor({ file_path, start_line, end_line, new: new_ } = {}) {
		super()
		this.file_path = file_path
		this.start_line = start_line
		this.end_line = end_line
		this.new = new_
	}

	get summary() {
		return `replace lines ${this.start_line}-${this.end_line} in ${this.file_path}`
	}
}

class AppendFileTool extends Tool {
	static name = 'append_file'

	constructor({ file_path, content } = {}) {
		super()
		this.file_path = file_path
		this.content = content
	}

	get summary() {
		return `append ${this.file_path}`
	}
}

class RenameFileTool extends Tool {
	static name = 'rename_file'

	constructor({ file_path, new_file_path } = {}) {
		super()
		this.file_path = file_path
		this.new_file_path = new_file_path
	}

	get summary() {
		return `rename ${this.file_path} to ${this.new_file_path}`
	}
}

class CopyFileTool extends Tool {
	static name = 'copy_file'

	constructor({ file_path, new_file_path } = {}) {
		super()
		this.file_path = file_path
		this.new_file_path = new_file_path
	}

	get summary() {
		return `copy ${this.file_path} to ${this.new_file_path}`
	}
}

class GetUrlTool extends Tool {
	static name = 'get_url'

	constructor({ url } = {}) {
		super()
		this.url = url
	}

	get summary() {
		return `get ${this.url}`
	}
}

class DownloadUrlTool extends Tool {
	static name = 'download_url'

	constructor({ url, file_path } = {}) {
		super()
		this.url = url
		this.file_path = file_path
	}

	get summary() {
		return `download ${this.url} to ${this.file_path}`
	}
}

class DeleteFileTool extends Tool {
	static name = 'delete_file'

	constructor({ file_path } = {}) {
		super()
		this.file_path = file_path
	}

	get summary() {
		return `delete ${this.file_path}`
	}
}

class CurrentTimeTool extends Tool {
	static name = 'current_time'

	get summary() {
		return 'current time'
	}
}

class CalculatorTool extends Tool {
	static name = 'calculator'

	constructor({ expression } = {}) {
		super()
		this.expression = expression
	}

	get summary() {
		return `calculate ${this.expression}`
	}
}

class TodoTool extends Tool {
	static name = 'todo'

	constructor({ action, item = null } = {}) {
		super()
		this.action = action
		this.item = item
	}

	get summary() {
		return 'todo'
	}
}

class RunPythonTool extends Tool {
	static name = 'run_python'

	constructor({ code } = {}) {
		super()
		this.code = code
	}

	get summary() {
		return `run python ${truncate(this.code, LABEL_TRUNCATE_LENGTH)}`
	}
}

class RunCommandTool extends Tool {
	static name = 'run_command'

	constructor({ command } = {}) {
		super()
		this.command = command
	}

	get summary() {
		return `run ${truncate(this.command, LABEL_TRUNCATE_LENGTH)}`
	}
}

class MakeFolderTool extends Tool {
	static name = 'make_folder'

	constructor({ folder_path } = {}) {
		super()
		this.folder_path = folder_path
	}

	get summary() {
		return `make folder ${this.folder_path}`
	}
}

class RemoveFolderTool extends Tool {
	static name = 'remove_folder'

	constructor({ folder_path } = {}) {
		super()
		this.folder_path = folder_path
	}

	get summary() {
		return `remove folder ${this.folder_path}`
	}
}

class LightStatusTool extends Tool {
	static name = 'light_status'

	get summary() {
		return 'light status'
	}
}

class LightOnTool extends Tool {
	static name = 'light_on'

	get summary() {
		return 'turn light on'
	}
}

class LightOffTool extends Tool {
	static name = 'light_off'

	get summary() {
		return 'turn light off'
	}
}

class Toolbox {
	static tools = [
		ReadFileTool, WriteFileTool, EditFileTool, EditFileRangeTool,
		AppendFileTool, RenameFileTool, CopyFileTool, GetUrlTool,
		DownloadUrlTool, DeleteFileTool, CurrentTimeTool, CalculatorTool,
		TodoTool, RunPythonTool, RunCommandTool, MakeFolderTool,
		RemoveFolderTool, LightStatusTool, LightOnTool, LightOffTool,
	]

	constructor() {
		this.byName = Object.fromEntries(Toolbox.tools.map(cls => [cls.name, cls]))
	}

	find(name) {
		return this.byName[name]
	}

	build(name, rawArguments) {
		const cls = this.find(name)
		if (!cls) return null
		let args
		try {
			args = typeof rawArguments === 'string' ? JSON.parse(rawArguments || '{}') : (rawArguments || {})
		} catch {
			args = {}
		}
		return new cls(args)
	}
}

// fetch_url was split into get_url and download_url
const LEGACY_TOOLS = { 'fetch_url': ['get_url', 'download_url'] }

const LABEL_TRUNCATE_LENGTH = 30

function truncate(text, length) {
	return text.length > length ? text.slice(0, length) + '…' : text
}

class Turn {
	constructor(json, message) {
		this.uuid = json.uuid
		this.message = message
		this.hidden = json.hidden
		this.error = json.error
	}
}

class MessageTurn extends Turn {
	constructor(json, message) {
		super(json, message)
		this.model = json.model
		this.think = json.think
		this.prompt = json.prompt
		this.reasoning = json.reasoning
		this.response = json.response
		this.raw = json.raw
	}
}

class ToolTurn extends Turn {
	constructor(json, message) {
		super(json, message)
		this.tool = json.tool
		this.arguments = json.arguments
		this.result = json.result
		this.call_id = json.call_id
	}
}

class Message {
	constructor(json, session) {
		this.uuid = json.uuid
		this.session = session
		this.hidden = json.hidden
		this.input_tokens = json.input_tokens
		this.output_tokens = json.output_tokens
		this.turns = json.turns.map(t => "tool" in t ? new ToolTurn(t, this) : new MessageTurn(t, this))
	}
}

class Model {
	constructor(provider, id, free, fields) {
		this.provider = provider
		this.id = id
		this.free = free
		Object.assign(this, fields)
	}
}

class Provider {
	constructor(name) {
		this.name = name
		this.models = []
	}
}

class ChatPad {
	constructor(records) {
		this.providers = []
		this.toolbox = new Toolbox()
		const byName = {}
		for (const record of records) {
			const { provider: providerName, id, free, ...fields } = record
			let provider = byName[providerName]
			if (!provider) {
				provider = new Provider(providerName)
				byName[providerName] = provider
				this.providers.push(provider)
			}
			provider.models.push(new Model(provider, id, free, fields))
		}
	}

	get models() {
		return this.providers.flatMap(p => p.models)
	}

	get freeModels() {
		return this.models.filter(m => m.free)
	}

	get paidModels() {
		return this.models.filter(m => !m.free)
	}

	model(id) {
		return this.models.find(m => m.id === id)
	}
}

const MAX_STEPS = 50

class Settings {
	constructor(session = null, json = {}) {
		this.session = session
		this.model = json.model ?? ''
		this.tools = json.tools ?? []
		this.mode = json.mode ?? 'auto'
		this.think = json.think ?? true
		this.max_steps = json.max_steps ?? MAX_STEPS
	}

	async save() {
		for (const key of ['model', 'tools', 'mode', 'think', 'max_steps']) {
			const value = this[key]
			if (value === null || value === undefined) continue
			if (this.session) {
				await fetch(`/api/sessions/${this.session.uuid}/settings/${key}`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ value }),
				})
			}
		}
	}
}

class Session {
	constructor(json) {
		this.uuid = json.uuid
		this.archived = json.archived
		this.status = json.status
		this.settings = new Settings(this, json.settings)
		this.messages = json.messages.map(m => new Message(m, this))
	}

	get models() {
		const models = []
		for (const message of this.messages) {
			for (const turn of message.turns) {
				if (turn instanceof MessageTurn && turn.model && !models.includes(turn.model)) {
					models.push(turn.model)
				}
			}
		}
		return models
	}

	// action is omitted for a plain continue-after-pause, or 'approve'/'deny'/'stop'
	// to resolve a pending tool-call approval. formData carries the chatbox's
	// current model/tools/mode/reasoning, read fresh at the moment of the click.
	resume(action, formData) {
		this.status = action === 'stop' ? 'idle' : 'busy'
		if (action) formData.set('action', action)
		fetch(`/api/sessions/${this.uuid}/resume`, { method: 'POST', body: formData })
	}

	pause() {
		this.status = 'paused'
		fetch(`/api/sessions/${this.uuid}/pause`, { method: 'POST' })
	}

	stop() {
		this.status = 'idle'
		fetch(`/api/sessions/${this.uuid}/stop`, { method: 'POST' })
	}
}
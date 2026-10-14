Handlebars.registerHelper('eq', (a, b) => a === b)

function renderModelMenu() {
	const list = document.getElementById('model-menu-list')
	if (!list || !chatpad) return
	const source = document.getElementById('model-menu-template').innerHTML
	const selectedModelId = document.querySelector('[name="model"]')?.value || null
	const render = Handlebars.compile(source)
	list.innerHTML = render({ providers: chatpad.providers, selectedModelId })
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

class SessionView {
	constructor(container, session) {
		this.container = container
		this.session = session
		this.message_views = []
	}

	async draw() {
		await Promise.all(this.session.messages.map(message => {
			const container = document.createElement('div')
			container.className = 'message'
			this.container.appendChild(container)
			const view = new MessageView(container, this, message)
			this.message_views.push(view)
			return view.draw()
		}))
		this.collapseAllButLast()
	}

	async update() {
		if (this.session.messages.length > this.message_views.length) {
			const message = this.session.messages[this.session.messages.length - 1]
			const container = document.createElement('div')
			container.className = 'message'
			this.container.appendChild(container)
			const view = new MessageView(container, this, message)
			this.message_views.push(view)
			await view.draw()
		} else if (this.message_views.length > 0) {
			this.message_views[this.message_views.length - 1].update()
		}
		this.collapseAllButLast()
	}

	// At any point, only the single last message stays fully expanded -
	// everything earlier is collapsed to one line, so scrolling back through
	// history never means scrolling through full text. Tool blocks are not
	// touched here - they always start and stay collapsed by default,
	// regardless of which message they're in, and only expand on click.
	// Reasoning is different: it's only useful while the answer is still
	// being produced, so once the session goes idle it collapses too, even
	// in the last message - only the prompt, error, and response stay open.
	collapseAllButLast() {
		const messages = Array.from(this.container.querySelectorAll('.message'))
		const lastMessage = messages[messages.length - 1]
		const running = this.session.status === 'busy'
		messages.forEach(message => {
			const blocks = message.querySelectorAll('.prompt, .reasoning, .error, .response')
			blocks.forEach(el => {
				const keepOpen = message === lastMessage && (running || !el.classList.contains('reasoning'))
				el.classList.toggle('collapsed', !keepOpen)
			})
		})
	}
}

class MessageView {
	constructor(container, session_view, message) {
		this.container = container
		this.session_view = session_view
		this.message = message
		this.turn_views = []
	}

	directChild(selector) {
		return Array.from(this.container.children).find(c => c.matches(selector))
	}

	insertDirectChild(el) {
		const action = this.directChild('.action')
		if (action) this.container.insertBefore(el, action)
		else this.container.appendChild(el)
	}

	async draw() {
		await Promise.all(this.message.turns.map(turn => this.drawTurn(turn)))
		this.update()
	}

	drawTurn(turn) {
		const container = document.createElement('div')
		this.insertDirectChild(container)
		const view = turn instanceof ToolTurn
			? new ToolTurnView(container, this, turn)
			: new TurnView(container, this, turn)
		this.turn_views.push(view)
		return view.draw()
	}

	update() {
		if (this.message.turns.length > this.turn_views.length) {
			this.drawTurn(this.message.turns[this.message.turns.length - 1])
		} else if (this.turn_views.length > 0) {
			this.turn_views[this.turn_views.length - 1].update()
		}
		this.updateInfo()
		this.drawHideAction()
	}

	updateInfo() {
		const turn = this.message.turns[this.message.turns.length - 1]
		if (!(turn instanceof MessageTurn) || !turn.model) return

		let info = this.directChild('.info')
		if (!info) {
			info = document.createElement('div')
			info.className = 'info'
			this.insertDirectChild(info)
		}

		let model = info.children[0]
		if (!model) {
			model = document.createElement('div')
			info.appendChild(model)
		}
		model.innerHTML = ''
		const modelInfo = chatpad.model(turn.model)
		if (modelInfo && modelInfo.icon) {
			const img = document.createElement('img')
			img.src = `/@@/models/${modelInfo.icon}`
			img.alt = ''
			model.appendChild(img)
		}
		const small = document.createElement('small')
		small.textContent = (modelInfo && modelInfo.name) || turn.model
		model.appendChild(small)

		const inputTokens = this.message.input_tokens
		const outputTokens = this.message.output_tokens
		const contextLength = this.message.context_length || (modelInfo && modelInfo.context_length)
		if (!inputTokens && !outputTokens && !contextLength) return
		let usage = info.children[1]
		if (!usage) {
			usage = document.createElement('div')
			info.appendChild(usage)
		}
		const parts = []
		if (inputTokens) parts.push('Input: ' + inputTokens)
		if (outputTokens) parts.push('Output: ' + outputTokens)
		if (contextLength) {
			let max = 'Max: ' + formatNumber(contextLength)
			if (inputTokens || outputTokens) max += ' (' + (((inputTokens || 0) + (outputTokens || 0)) / contextLength * 100).toFixed(1) + '%)'
			parts.push(max)
		}
		usage.innerHTML = ''
		const small2 = document.createElement('small')
		small2.textContent = parts.join(' | ')
		usage.appendChild(small2)

		let bar = info.querySelector('[data-usage]')
		if (contextLength && (inputTokens || outputTokens)) {
			if (!bar) {
				bar = document.createElement('div')
				bar.dataset.usage = ''
				bar.appendChild(document.createElement('div'))
				bar.appendChild(document.createElement('div'))
				info.appendChild(bar)
			}
			const [inputBar, outputBar] = bar.children
			inputBar.style.width = Math.min(100, inputTokens / contextLength * 100) + '%'
			outputBar.style.width = Math.min(100, outputTokens / contextLength * 100) + '%'
		} else if (bar) {
			bar.remove()
		}
	}

	drawHideAction() {
		let action = this.directChild('.action')
		if (!action) {
			action = document.createElement('span')
			action.className = 'action'
			action.onclick = () => this.container.classList.contains('hidden') ? this.unhide() : this.hide()
			this.container.appendChild(action)
			if (this.message.hidden) this.container.classList.add('hidden')
		}
		this.updateHideAction(action)
	}

	updateHideAction(action) {
		const hidden = this.container.classList.contains('hidden')
		action.title = hidden ? 'Show in history' : 'Hide from history'
		action.innerHTML = `<i data-lucide="${hidden ? 'eye-off' : 'eye'}"></i>`
		iconify(this.container)
	}

	// hidden is backend-owned: it decides whether this message is sent as
	// history to the model. The DOM class is only a reflection of that state,
	// exactly like a Chatbox setting - the write is the save.
	hide() {
		this.container.classList.add('hidden')
		this.updateHideAction(this.directChild('.action'))
		fetch(`/api/sessions/${this.message.session.uuid}/messages/${this.message.uuid}/hide`, { method: 'POST' })
	}

	unhide() {
		this.container.classList.remove('hidden')
		this.updateHideAction(this.directChild('.action'))
		fetch(`/api/sessions/${this.message.session.uuid}/messages/${this.message.uuid}/unhide`, { method: 'POST' })
	}
}

class TurnView {
	constructor(container, message_view, turn) {
		this.container = container
		this.message_view = message_view
		this.turn = turn
	}

	async draw() {
		if (this.turn.reasoning) this.turn.reasoning = await renderMarkdown(this.turn.reasoning)
		if (this.turn.response) this.turn.response = await renderMarkdown(this.turn.response)
		this.update()
	}

	update() {
		if (this.turn.prompt) this.updatePrompt(this.turn.prompt)
		if (this.turn.reasoning) this.updateBlock('reasoning', this.turn.reasoning)
		if (this.turn.response) this.updateBlock('response', this.turn.response)
		if (this.turn.error) this.updateError(this.turn.error)
	}

	updatePrompt(text) {
		let div = this.container.querySelector('.prompt')
		if (!div) div = this.createBlock('prompt')
		div.querySelector('.content').textContent = text
	}

	updateBlock(className, html) {
		let div = this.container.querySelector('.' + className)
		if (!div) div = this.createBlock(className)
		div.querySelector('.content').innerHTML = html
	}

	createBlock(className) {
		const div = document.createElement('div')
		div.className = className
		div.onclick = () => toggleCollapsed(div)

		const content = document.createElement('div')
		content.className = 'content'
		div.appendChild(content)

		const action = document.createElement('span')
		action.className = 'action'
		action.title = 'Copy'
		action.innerHTML = '<i data-lucide="copy"></i>'
		action.onclick = (e) => { e.stopPropagation(); this.copy(action) }
		div.appendChild(action)

		this.container.appendChild(div)
		iconify(div)
		return div
	}

	copy(button) {
		const content = button.parentElement.querySelector('.content')
		const clone = content.cloneNode(true)
		clone.querySelectorAll('.turn-time').forEach(el => el.remove())
		navigator.clipboard.writeText(clone.textContent.trim())
	}

	updateError(error) {
		let div = this.container.querySelector('.error')
		if (!div) div = this.createBlock('error')
		const content = div.querySelector('.content')
		content.innerHTML = ''
		const p = document.createElement('p')
		p.textContent = error.message
		content.appendChild(p)
		if (error.code) {
			const small = document.createElement('small')
			small.textContent = 'Code: ' + error.code
			content.appendChild(small)
		}
	}
}

class ToolTurnView {
	constructor(container, message_view, turn) {
		this.container = container
		this.message_view = message_view
		this.turn = turn
	}

	draw() {
		this.container.className = 'tool'

		const content = document.createElement('div')
		content.className = 'content'
		this.container.appendChild(content)

		this.update()
	}

	copy(button) {
		const content = button.parentElement.querySelector('.content')
		const clone = content.cloneNode(true)
		clone.querySelectorAll('.turn-time').forEach(el => el.remove())
		navigator.clipboard.writeText(clone.textContent.trim())
	}

	// The label every tool shows on its first line - verb plus the argument
	// that matters, written out by name. Each one of our tools is named here;
	// there is no table standing in for this knowledge.
	label() {
		const args = JSON.parse(this.turn.arguments || '{}')
		switch (this.turn.tool) {
			case 'read_file': return `read ${args.path}`
			case 'write_file': return `write ${args.path}`
			case 'edit_file': return `edit ${args.path}`
			case 'append_file': return `append ${args.path}`
			case 'rename_file': return `rename ${args.path} to ${args.new_path}`
			case 'copy_file': return `copy ${args.path} to ${args.new_path}`
			case 'get_url': return `get ${args.url}`
			case 'download_url': return `download ${args.url} to ${args.path}`
			case 'delete_file': return `delete ${args.path}`
			case 'current_time': return 'current time'
			case 'calculator': return `calculate ${args.expression}`
			case 'todo': return 'todo'
			case 'run_python': return `run python ${truncate(args.code, LABEL_TRUNCATE_LENGTH)}`
			case 'run_command': return `run ${truncate(args.command, LABEL_TRUNCATE_LENGTH)}`
			case 'make_folder': return `make folder ${args.path}`
			case 'remove_folder': return `remove folder ${args.path}`
			case 'light_status': return 'light status'
			case 'light_on': return 'turn light on'
			case 'light_off': return 'turn light off'
			default: return this.turn.tool
		}
	}

	// The second line, shown only when there's something to show. Each tool
	// is named here explicitly: read/get/download/calculator/todo/run_python/
	// run_command/light_status show their result; write/edit/append show the
	// argument being written. rename, copy, delete, make_folder, remove_folder,
	// light_on, light_off have nothing beyond "it happened" and are left out.
	contentText() {
		if (this.turn.result && this.turn.result.error) return this.turn.result.error.message
		const args = JSON.parse(this.turn.arguments || '{}')
		switch (this.turn.tool) {
			case 'write_file': return args.content
			case 'edit_file': return args.new
			case 'append_file': return args.content
			case 'read_file':
			case 'get_url':
			case 'download_url':
			case 'current_time':
			case 'calculator':
			case 'todo':
			case 'run_python':
			case 'run_command':
			case 'light_status':
				return this.turn.result ? this.turn.result.content : null
			default:
				return null
		}
	}

	update() {
		this.container.classList.toggle('error', !!(this.turn.result && this.turn.result.error))
		const content = this.container.querySelector('.content')
		content.innerHTML = ''
		const summary = document.createElement('div')
		summary.textContent = this.label()
		content.appendChild(summary)
		const text = this.contentText()
		this.container.classList.toggle('static', !text)
		if (text) {
			const result = document.createElement('div')
			result.textContent = text
			content.appendChild(result)
			if (!this.container.onclick) {
				this.container.classList.add('collapsed')
				this.container.onclick = () => toggleCollapsed(this.container)
			}
			if (!this.container.querySelector('.action')) {
				const action = document.createElement('span')
				action.className = 'action'
				action.title = 'Copy'
				action.innerHTML = '<i data-lucide="copy"></i>'
				action.onclick = (e) => { e.stopPropagation(); this.copy(action) }
				this.container.appendChild(action)
				iconify(this.container)
			}
		} else {
			this.container.classList.remove('collapsed')
			this.container.onclick = null
			const action = this.container.querySelector('.action')
			if (action) action.remove()
		}
	}
}

function toggleCollapsed(container) {
	if (window.getSelection().toString()) return
	container.classList.toggle('collapsed')
}

// fetch_url was split into get_url and download_url
const LEGACY_TOOLS = { 'fetch_url': ['get_url', 'download_url'] }

const LABEL_TRUNCATE_LENGTH = 30

function truncate(text, length) {
	return text.length > length ? text.slice(0, length) + '…' : text
}

// Session is a single global (see `let session` below); Chatbox never stores
class Chatbox {
	constructor(container) {
		this.container = container
	}

	// The one slot next to the textarea: Send alone when idle or paused (where
	// it means Resume), Stop+Pause while busy, Send-but-disabled while awaiting
	// a pending tool-call approval. Always reflects session.status right away -
	// never waits on a network round trip to decide what it shows.
	set status(value) {
		const slot = this.container.querySelector('.chatbox-input > div')
		slot.innerHTML = ''
		if (value === 'busy') {
			const stop = document.createElement('div')
			stop.className = 'action'
			stop.title = 'Stop'
			stop.innerHTML = '<i data-lucide="square"></i>'
			stop.onclick = () => { session.stop(); this.status = session.status }
			slot.appendChild(stop)

			const pause = document.createElement('div')
			pause.className = 'action'
			pause.title = 'Pause'
			pause.innerHTML = '<i data-lucide="pause"></i>'
			pause.onclick = () => { session.pause(); this.status = session.status }
			slot.appendChild(pause)
		} else {
			const send = document.createElement('div')
			send.className = 'action'
			send.title = value === 'paused' ? 'Resume' : 'Send'
			send.innerHTML = '<i data-lucide="send"></i>'
			if (value === 'awaiting') send.setAttribute('disabled', '')
			else send.onclick = () => document.getElementById('chat-form').requestSubmit()
			slot.appendChild(send)
		}
		iconify(slot)
	}

	// Sync the UI from the session's saved settings without writing them back -
	// a page view must never have the side effect of touching session state.
	init() {
		if (!session) return
		this.status = session.status || 'idle'
		this.applyTools(session.settings.tools || [])
		this.applyThink(session.settings.think == null ? true : session.settings.think)
		this.applyMode(session.settings.mode || 'auto')
		if (session.settings.model) this.applyModel(session.settings.model)
	}

	toggleModelList() {
		const list = this.container.querySelector('#model-menu')
		const hidden = list.classList.toggle('hidden')
		if (!hidden) this.container.querySelector('#tools-menu').classList.add('hidden')
	}

	toggleToolsList() {
		const list = this.container.querySelector('#tools-menu')
		const hidden = list.classList.toggle('hidden')
		if (!hidden) this.container.querySelector('#model-menu').classList.add('hidden')
	}

	// Mirror the picked option's icon and name onto the collapsed menu header, so
	// the closed menu shows which model is selected. Options carry their icon as a
	// nested <img class="model-icon-small"> (absent for the "none" option); the
	// header is an <img> + <span> pair so the icon can be swapped in and out.
	setHeaderModel(header, source) {
		const sourceIcon = source.querySelector('img.model-icon-small')
		const headerIcon = header.querySelector('img.model-icon-small')
		const nameEl = header.querySelector('span')
		if (headerIcon) {
			if (sourceIcon) {
				headerIcon.src = sourceIcon.src
				headerIcon.hidden = false
			} else {
				headerIcon.removeAttribute('src')
				headerIcon.hidden = true
			}
		}
		if (nameEl) nameEl.textContent = source.dataset.name
	}

	toggleTool(el) {
		const input = el.nextElementSibling
		const enabled = el.classList.toggle('enabled')
		input.disabled = !enabled
		this.tools = Array.from(this.container.querySelectorAll('.tool-toggle.enabled')).map(o => o.dataset.name)
	}

	get tools() {
		return Array.from(this.container.querySelectorAll('.tool-toggle.enabled')).map(el => el.dataset.name)
	}

	set tools(names) {
		this.applyTools(names)
		if (session) {
			session.settings.tools = this.tools
			session.settings.save()
		}
	}

	applyTools(names) {
		const current = Array.from(this.container.querySelectorAll('.tool-toggle')).map(el => el.dataset.name)
		const wanted = new Set()
		names.forEach(name => {
			if (current.includes(name)) wanted.add(name)
			;(LEGACY_TOOLS[name] || []).forEach(n => wanted.add(n))
		})
		this.container.querySelectorAll('.tool-toggle').forEach(el => {
			const input = el.nextElementSibling
			const on = wanted.has(el.dataset.name)
			el.classList.toggle('enabled', on)
			input.disabled = !on
		})
	}

	toggleReasoning(el) {
		this.think = el.classList.toggle('enabled')
	}

	get think() {
		const el = this.container.querySelector('#reasoning-toggle')
		return el ? el.classList.contains('enabled') : null
	}

	set think(value) {
		this.applyThink(value)
		if (session) {
			session.settings.think = value
			session.settings.save()
		}
	}

	applyThink(value) {
		const el = this.container.querySelector('#reasoning-toggle')
		if (el) {
			el.classList.toggle('enabled', value)
			el.nextElementSibling.disabled = !value
		}
	}

	selectModel(el) {
		this.model = el.dataset.id
	}

	get model() {
		const el = this.container.querySelector('[name="model"]')
		return el ? el.value : null
	}

	set model(value) {
		this.applyModel(value)
		if (session) {
			session.settings.model = value
			session.settings.save()
		}
	}

	applyModel(value) {
		this.container.querySelectorAll('#model-menu .model-toggle.enabled').forEach(o => o.classList.remove('enabled'))
		const option = this.container.querySelector(`#model-menu .model-toggle[data-id="${CSS.escape(value)}"]`)
		if (option) {
			option.classList.add('enabled')
			this.setHeaderModel(this.container.querySelector('#model-link'), option)
		}
		this.container.querySelector('[name="model"]').value = value
		this.container.querySelector('#model-menu').classList.add('hidden')
	}

	toggleMode(el) {
		this.mode = el.dataset.mode === 'manual' ? 'auto' : 'manual'
	}

	get mode() {
		const el = this.container.querySelector('#mode-toggle')
		return el ? el.dataset.mode : null
	}

	set mode(value) {
		this.applyMode(value)
		if (session) {
			session.settings.mode = value
			session.settings.save()
		}
	}

	applyMode(value) {
		const el = this.container.querySelector('#mode-toggle')
		if (el) {
			el.dataset.mode = value
			el.title = value === 'manual' ? 'Manual: approve each tool call' : 'Auto: tools run without approval'
		}
		this.container.querySelector('[name="mode"]').value = value
	}

	refreshModels() {
		fetch('/api/models').then(() => location.reload())
	}
}

function iconify(root) {
	lucide.createIcons({ root })
}

// Follow the stream only while the reader is at the bottom, so scrolling up to
// read is never yanked back down by an incoming patch. The flag is driven by
// real scroll events rather than measured at paint time, so content growing
// cannot by itself push the reader out of the follow zone.
let followStream = true

function initFollowStream() {
	const update = () => {
		followStream = document.body.scrollHeight - window.innerHeight - window.scrollY < 120
	}
	window.addEventListener('scroll', update, { passive: true })
	update()
}

function scrollIfAtBottom() {
	if (followStream) window.scrollTo(0, document.body.scrollHeight)
}

function ensureMessagesContainer() {
	let el = document.querySelector('.session')
	if (el) return el
	el = document.createElement('div')
	el.className = 'session'
	document.querySelector('main').appendChild(el)
	return el
}

function showPermissionBox() {
	if (permissionBox) return
	const template = document.getElementById('permission-template')
	permissionBox = template.content.firstElementChild.cloneNode(true)
	permissionBox.querySelectorAll('button').forEach(button => {
		button.onclick = () => {
			session.resume(button.dataset.action, new FormData(document.getElementById('chat-form')))
			chatbox.status = session.status
		}
	})
	ensureMessagesContainer().appendChild(permissionBox)
}

let session = null
let liveSessionView = null
let chatbox = null
let permissionBox = null
let chatpad = null

function startLiveTurn(record) {
	session.messages.push(new Message({ uuid: record.uuid, hidden: false, turns: [] }, session))
	liveSessionView.update()
}

function currentMessage() {
	return session.messages[session.messages.length - 1]
}

function currentTurn() {
	return currentMessage().turns[currentMessage().turns.length - 1]
}

function applyPatch(turn, target, patchText) {
	const dmp = new diff_match_patch()
	const patches = dmp.patch_fromText(patchText)
	const [newHtml] = dmp.patch_apply(patches, turn[target] || '')
	turn[target] = newHtml
}

async function renderMarkdown(text) {
	const response = await fetch('/api/markdown', { method: 'POST', body: new URLSearchParams({ text }) })
	return response.text()
}

async function handleStreamRecord(record) {
	if (record.type === 'status') {
		session.status = record.status
		chatbox.status = record.status
		if (permissionBox && record.status !== 'awaiting') { permissionBox.remove(); permissionBox = null }
	} else if (record.type === 'prompt') {
		startLiveTurn(record)
		currentMessage().turns.push(new MessageTurn({ prompt: record.content }, currentMessage()))
	} else if (!session || !session.messages.length) {
		return
	} else if (record.type === 'html_patch' || record.type === 'reasoning' || record.type === 'response') {
		if (!(currentTurn() instanceof MessageTurn)) currentMessage().turns.push(new MessageTurn({}, currentMessage()))
		if (record.type === 'html_patch') applyPatch(currentTurn(), record.target, record.patch)
		else if (record.type === 'reasoning') currentTurn().reasoning = await renderMarkdown(record.content)
		else {
			if (record.content) currentTurn().response = await renderMarkdown(record.content)
			if (record.model) currentTurn().model = record.model
			if (record.usage) {
				currentMessage().input_tokens = record.usage.prompt_tokens
				currentMessage().output_tokens = record.usage.completion_tokens
			}
			if (record.context_length) currentMessage().context_length = record.context_length
		}
	} else if (record.type === 'error') {
		currentTurn().error = record.error
	} else if (record.type === 'tool_call') {
		record.tool_calls.forEach(call => {
			currentMessage().turns.push(new ToolTurn({
				tool: call.function.name,
				arguments: call.function.arguments,
				result: null,
				call_id: call.id,
			}, currentMessage()))
		})
	} else if (record.type === 'tool_result') {
		const turn = currentMessage().turns.find(t => t instanceof ToolTurn && t.call_id === record.tool_call_id)
		if (turn) turn.result = { content: record.content, error: record.error }
	} else if (record.type === 'await_approval') {
		session.status = 'awaiting'
		chatbox.status = 'awaiting'
		showPermissionBox()
	}
	liveSessionView.update()
	scrollIfAtBottom()
}

async function openLiveStream(afterUuid) {
	const url = afterUuid
		? `/api/sessions/${session.uuid}/stream?uuid=${afterUuid}`
		: `/api/sessions/${session.uuid}/stream`
	const response = await fetch(url)
	const reader = response.body.getReader()
	const decoder = new TextDecoder()
	let buffer = ''
	while (true) {
		const { done, value } = await reader.read()
		if (done) break
		buffer += decoder.decode(value, { stream: true })
		const parts = buffer.split('\n\n')
		buffer = parts.pop()
		for (const part of parts) {
			if (!part.startsWith('data: ')) continue
			try {
				await handleStreamRecord(JSON.parse(part.slice(6)))
			} catch (e) {
				console.error('live record failed, continuing stream:', e)
			}
		}
	}
}

async function submitLiveChat(event) {
	event.preventDefault()
	if (session && session.status === 'awaiting') return
	const form = event.currentTarget
	const formData = new FormData(form)

	if (session && session.status === 'paused') {
		session.status = 'busy'
		chatbox.status = 'busy'
		fetch(`/api/sessions/${session.uuid}/resume`, { method: 'POST', body: formData })
		return
	}

	const promptEl = form.querySelector('textarea[name="prompt"]')
	promptEl.value = ''

	const url = session ? `/api/sessions/${session.uuid}/message` : '/api/message'
	const response = await fetch(url, { method: 'POST', body: formData })
	const ack = await response.json()
	if (!session) {
		session = new Session({ uuid: ack.session, archived: false, status: 'busy', settings: {}, messages: [] })
		liveSessionView = new SessionView(ensureMessagesContainer(), session)
		session.settings.model = chatbox.model
		session.settings.tools = chatbox.tools
		session.settings.mode = chatbox.mode
		session.settings.think = chatbox.think
		session.settings.save()
		chatbox.status = 'busy'
		history.pushState(null, '', `/chat/${ack.session}`)
		openLiveStream()
	} else {
		session.status = 'busy'
		chatbox.status = 'busy'
	}
}

async function init() {
	const form = document.getElementById('chat-form')
	if (!form) return
	const sessionId = form.dataset.sessionId || null
	form.addEventListener('submit', submitLiveChat)
	chatbox = new Chatbox(document.querySelector('.chatbox'))
	chatpad = new ChatPad(await (await fetch('/api/models')).json())
	renderModelMenu()
	if (!sessionId) return
	const json = await (await fetch(`/api/sessions/${sessionId}`)).json()
	session = new Session(json)
	liveSessionView = new SessionView(ensureMessagesContainer(), session)
	await liveSessionView.draw()
	chatbox.init()
	if (session.archived) return
	if (session.status === 'awaiting') showPermissionBox()
	const lastMessage = session.messages[session.messages.length - 1]
	const lastTurn = lastMessage && lastMessage.turns[lastMessage.turns.length - 1]
	openLiveStream(lastTurn ? lastTurn.uuid : null)
}

document.addEventListener('DOMContentLoaded', async function() {
	await init()
	initFollowStream()
	lucide.createIcons()
})

function formatNumber(n) {
	if (n === null || n === undefined) return ""
	if (n === 0) return "0"
	if (n % 1024 === 0) {
		if (n < 1024) return `${Math.trunc(n)} B`
		const units = ["KiB", "MiB", "GiB", "TiB"]
		let i = -1
		while (n >= 1024 && i < units.length - 1) {
			n /= 1024
			i += 1
		}
		const s = n.toFixed(1).replace(/0+$/, "").replace(/\.$/, "")
		return `${s} ${units[i]}`
	} else {
		if (n < 1000) return String(Math.trunc(n))
		const units = ["K", "M", "B", "T"]
		let i = -1
		while (n >= 1000 && i < units.length - 1) {
			n /= 1000
			i += 1
		}
		const s = n.toFixed(1).replace(/0+$/, "").replace(/\.$/, "")
		return `${s}${units[i]}`
	}
}

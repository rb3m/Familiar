const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const token = new URLSearchParams(location.search).get('token') || '';
const storageKey = 'familiar.launcher.choices';
let languageNames = {};
let englishLanguageNames = {};
const samples = {
  en: 'Hello! I’m ready for our next adventure.',
  es: '¡Hola! Estoy lista para nuestra próxima aventura.',
  ja: 'こんにちは！次の冒険に出かけましょう。',
};
const criticalEvents = new Set(['fuel_low', 'air_pressure', 'engine_warning', 'fined', 'job_cancelled']);
let state;
let choices = {};
let voices = null;
let voicesError = '';
let editor = null;
let dirty = false;
let sessionBusy = false;
let editorBusy = false;
let closed = false;
let currentView = 'play';
let source;
let reconnectTimer;
let reconnectDelay = 1000;
let followTranscript = true;
let lastBubble = null;
let replayCounts = new Map();
const lineCounts = new Map();

function readStorage(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

function writeStorage(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch { /* The launcher also works when browser storage is unavailable. */ }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  headers.set('X-Familiar-Token', token);
  if (options.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers });
  const body = await response.text();
  let data;
  try { data = body ? JSON.parse(body) : null; }
  catch { throw new Error(response.ok ? 'The server returned invalid JSON.' : `Request failed (${response.status}).`); }
  if (!response.ok) throw new Error(data?.error || `Request failed (${response.status}).`);
  return data;
}

const post = (path, body) => api(path, {
  method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const personaPath = (id) => `/api/personas/${encodeURIComponent(id)}`;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, action, className = '') {
  const node = element('button', className, text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function showMessage(node, message) {
  node.textContent = message;
  node.hidden = !message;
}

function toast(message, error = false) {
  const node = element('div', `toast${error ? ' error' : ''}`, message);
  $('#toasts').append(node);
  setTimeout(() => node.remove(), error ? 10000 : 5500);
}

async function busy(control, action) {
  control.disabled = true;
  try { await action(); }
  catch (error) { toast(error.message, true); }
  finally { if (control.isConnected) control.disabled = false; }
}

function canLeaveEditor() {
  if (editorBusy) {
    toast('Please wait for the current persona operation to finish.');
    return false;
  }
  return !dirty || window.confirm('Discard the unsaved changes to this persona?');
}

function setDirty(value) {
  dirty = value;
  $('#unsaved').hidden = !value;
}

function switchView(view) {
  if (view === currentView) return;
  if (!canLeaveEditor()) return;
  if (dirty) closeEditor();
  currentView = view;
  $('#play-view').hidden = view !== 'play';
  $('#personas-view').hidden = view !== 'personas';
  for (const tab of $$('[data-view]')) {
    const active = tab.dataset.view === view;
    tab.classList.toggle('active', active);
    if (active) tab.setAttribute('aria-current', 'page');
    if (!active) tab.removeAttribute('aria-current');
  }
  $(`#${view}-title`).setAttribute('tabindex', '-1');
  $(`#${view}-title`).focus();
}

function selectedPersona() {
  return state?.personas.find((persona) => persona.id === choices.persona);
}

function normalizeChoices(preferred = choices) {
  const defaults = state.defaults;
  const validGame = (id) => id === null || state.games.some((game) => game.id === id);
  const validPersona = (id) => state.personas.some((persona) => persona.id === id);
  const game = validGame(preferred.game) ? preferred.game : (validGame(defaults.game) ? defaults.game : null);
  const persona = validPersona(preferred.persona) ? preferred.persona
    : (validPersona(defaults.persona) ? defaults.persona : state.personas[0]?.id);
  const allowed = state.languages.filter((language) => state.personas.find((item) => item.id === persona)?.languages.includes(language.id));
  const validLanguage = (id) => allowed.some((language) => language.id === id);
  const language = validLanguage(preferred.language) ? preferred.language
    : (validLanguage(defaults.language) ? defaults.language : allowed[0]?.id);
  choices = { game, persona, language };
  writeStorage(storageKey, choices);
}

function renderGames() {
  const root = $('#games');
  root.replaceChildren();
  for (const game of [{ id: null, name: 'No game', description: 'Desk chat' }, ...state.games]) {
    const node = button('', () => {
      choices.game = game.id;
      writeStorage(storageKey, choices);
      renderGames();
      $('[aria-pressed=true]', root)?.focus();
    }, 'choice');
    node.disabled = state.session.running || sessionBusy;
    node.setAttribute('aria-pressed', String(choices.game === game.id));
    const icon = element('span', 'game-icon', game.id === null ? '☾' : (game.icon || '🎮'));
    icon.setAttribute('aria-hidden', 'true');
    node.append(icon, element('strong', '', game.name));
    if (game.description) node.append(element('small', '', game.description));
    root.append(node);
  }
}

function renderPersonas() {
  const picker = $('#persona-picker');
  const library = $('#persona-list');
  picker.replaceChildren();
  library.replaceChildren();
  $('#no-personas').hidden = state.personas.length > 0;
  if (!state.personas.length) library.append(element('p', 'muted', 'No personas yet. Create your first companion.'));
  for (const persona of state.personas) {
    const node = button('', () => {
      choices.persona = persona.id;
      normalizeChoices();
      renderPersonas();
      renderLanguage();
      renderSession();
      $('[aria-pressed=true]', picker)?.focus();
    }, 'choice');
    node.disabled = state.session.running || sessionBusy;
    node.setAttribute('aria-pressed', String(choices.persona === persona.id));
    node.append(element('strong', '', persona.display_name), element('small', '', persona.source || 'Original character'));
    const meta = element('div', 'persona-meta');
    meta.append(element('span', 'badge', persona.truthfulness), element('span', 'origin', persona.builtin ? 'Built-in' : 'Custom'));
    const meter = element('div', 'meter');
    meter.setAttribute('role', 'meter');
    meter.setAttribute('aria-label', 'Chattiness');
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', '1');
    meter.setAttribute('aria-valuenow', String(persona.chattiness));
    const fill = element('span');
    fill.style.width = `${Math.max(0, Math.min(1, persona.chattiness)) * 100}%`;
    meter.append(fill);
    const chips = element('div', 'chips');
    for (const language of persona.languages) chips.append(element('span', 'chip', language.toUpperCase()));
    node.append(meta, meter, chips);
    picker.append(node);

    const item = element('article', 'library-item');
    const title = element('h3', '', persona.display_name);
    title.append(element('span', 'badge', persona.builtin ? 'Built-in' : 'Custom'));
    item.append(title, element('p', 'muted', persona.source || 'Original character'));
    const actions = element('div', 'actions');
    const editButton = button('Edit', () => busy(editButton, () => openPersona(persona.id)));
    const duplicateButton = button('Duplicate', () => busy(duplicateButton, () => openPersona(persona.id, true)));
    editButton.setAttribute('aria-label', `Edit ${persona.display_name}`);
    duplicateButton.setAttribute('aria-label', `Duplicate ${persona.display_name}`);
    actions.append(editButton, duplicateButton);
    if (!persona.builtin) {
      const label = persona.overrides_builtin ? 'Reset to built-in' : 'Delete';
      const remove = button(label, () => busy(remove, () => deletePersona(persona)), 'danger');
      remove.setAttribute('aria-label', `${label}: ${persona.display_name}`);
      actions.append(remove);
    }
    item.append(actions);
    if (persona.overrides_builtin) {
      item.append(element('small', '', 'Deleting an override restores its built-in persona.'));
    }
    library.append(item);
  }
}

function renderLanguage() {
  const select = $('#language');
  select.replaceChildren();
  for (const language of state.languages.filter((item) => selectedPersona()?.languages.includes(item.id))) {
    select.add(new Option(language.native || language.name, language.id));
  }
  select.value = choices.language || '';
  select.disabled = state.session.running || sessionBusy || !select.options.length;
}

function renderQuota() {
  const root = $('#quota');
  root.replaceChildren();
  root.hidden = !state.quota;
  if (!state.quota) return;
  const { used, limit } = state.quota;
  root.append(element('strong', '', 'ElevenLabs'), element('div', '', `${used.toLocaleString()} / ${limit.toLocaleString()} characters`));
  const bar = element('div', 'meter');
  const fill = element('span');
  fill.style.width = `${limit > 0 ? Math.min(100, Math.max(0, used / limit * 100)) : 0}%`;
  bar.append(fill);
  root.append(bar);
}

function renderSession() {
  const session = state.session;
  const toggle = $('#session-toggle');
  toggle.textContent = sessionBusy ? 'Please wait…' : (session.running ? 'Stop companion' : 'Start companion');
  toggle.classList.toggle('running', session.running);
  toggle.disabled = sessionBusy || (!session.running && (!choices.persona || !choices.language));
  let message = 'Ready when you are.';
  if (session.running) {
    const persona = state.personas.find((item) => item.id === session.persona)?.display_name || session.persona || 'Companion';
    const date = session.started_at ? new Date(session.started_at) : null;
    const since = date && !Number.isNaN(date.getTime()) ? ` · running since ${date.toLocaleString()}` : ' · running';
    const game = state.games.find((item) => item.id === session.game)?.name || session.game || 'desk chat';
    message = `${persona} · ${game} · ${session.language || ''}${since}`;
  }
  if (!session.running && session.exit_code !== null) message = `Session stopped · exit code ${session.exit_code}`;
  $('#session-status').textContent = message;
  if (!session.running) $('#listening').hidden = true;
  for (const control of $$('#games button, #persona-picker button')) control.disabled = session.running || sessionBusy;
  $('#language').disabled = session.running || sessionBusy || !$('#language').options.length;
}

function applySession(session) {
  state.session = session;
  if (session.running) {
    normalizeChoices({ game: session.game, persona: session.persona, language: session.language });
    renderGames();
    renderPersonas();
    renderLanguage();
  }
  renderSession();
}

function applyState(nextState) {
  state = nextState;
  languageNames = Object.fromEntries(state.languages.map((item) => [item.id, item.native || item.name]));
  englishLanguageNames = Object.fromEntries(state.languages.map((item) => [item.id, item.name]));
  normalizeChoices(state.session.running ? state.session : choices);
  renderGames();
  renderPersonas();
  renderLanguage();
  renderSession();
  renderQuota();
}

async function refreshState() {
  const [nextState, languages] = await Promise.all([api('/api/state'), api('/api/languages')]);
  applyState({ ...nextState, languages });
}

async function toggleSession() {
  if (sessionBusy || !state) return;
  sessionBusy = true;
  renderSession();
  try {
    const session = state.session.running
      ? await post('/api/session/stop')
      : await post('/api/session/start', choices);
    applySession(session);
  } catch (error) {
    toast(error.message, true);
    try { await refreshState(); } catch { /* Keep the existing state until reconnect. */ }
  } finally {
    sessionBusy = false;
    renderSession();
  }
}

function setEditorBusy(value) {
  editorBusy = value;
  $('#editor-form').inert = value;
}

function closeEditor() {
  editor = null;
  setDirty(false);
  $('#editor-form').hidden = true;
  $('#editor-empty').hidden = false;
}

function newPersona() {
  if (!canLeaveEditor()) return;
  fillEditor({ id: '', display_name: '', source: '', truthfulness: 'strict', chattiness: 0.5,
    prompt: '', style_rules: [], languages: { en: { voices: {} } }, game_hooks: {} }, true);
}

async function openPersona(id, duplicate = false) {
  if (!canLeaveEditor()) return;
  setEditorBusy(true);
  try {
    const data = await api(personaPath(id));
    if (duplicate) {
      data.id = uniqueCopyId(id);
      data.display_name = `${data.display_name} (copy)`;
      data.builtin = false;
    }
    fillEditor(data, duplicate);
  } finally {
    setEditorBusy(false);
    $('#edit-name').focus();
  }
}

function uniqueCopyId(id) {
  const base = id.slice(0, 30);
  let candidate = `${base}-copy`;
  let index = 2;
  while (state.personas.some((persona) => persona.id === candidate)) candidate = `${base}-copy-${index++}`;
  return candidate;
}

function fillEditor(data, isNew, draft = false) {
  editor = { data, isNew };
  $('#editor-form').reset();
  $('#editor-empty').hidden = true;
  $('#editor-form').hidden = false;
  $('#editor-title').textContent = isNew ? 'New persona' : `Edit ${data.display_name}`;
  $('#edit-id').value = data.id || '';
  $('#edit-id').readOnly = !isNew;
  $('#id-field').hidden = !isNew;
  $('#edit-name').value = data.display_name || '';
  $('#edit-source').value = data.source || '';
  const truthfulness = data.truthfulness === 'playful' ? 'playful' : 'strict';
  $(`input[name="truthfulness"][value="${truthfulness}"]`).checked = true;
  $('#edit-chattiness').value = data.chattiness ?? 0.5;
  $('#chattiness-value').textContent = $('#edit-chattiness').value;
  $('#edit-prompt').value = data.prompt || '';
  $('#edit-rules').value = (data.style_rules || []).join('\n');
  showMessage($('#editor-notice'), draft ? 'AI draft · Unsaved. Review the facts, personality, voices, and game hooks before saving.'
    : (data.builtin && !isNew ? 'This is a built-in persona. Saving creates a custom copy with the same ID; the original stays available to restore.' : ''));
  showMessage($('#editor-error'), '');
  renderVoiceRows(data.languages || {});
  renderHooks(data.game_hooks || {});
  setDirty(isNew || draft);
  $('#edit-name').focus();
}

function voiceLabel(voice) {
  const language = voice.labels?.language;
  if (!language) return voice.name;
  const raw = voice.labels?.accent;
  const accent = raw && raw !== 'standard' ? ` (${raw.replace(/\b\w/g, (c) => c.toUpperCase())})` : '';
  return `${voice.name} · ${englishLanguageNames[language] || language}${accent}`;
}

// ElevenLabs voices native to the row's language come first, in their own group.
function addElevenLabsOptions(select, options, language) {
  const native = options.filter((voice) => voice.labels?.language === language);
  const others = options.filter((voice) => voice.labels?.language !== language);
  const groups = [[`${englishLanguageNames[language] || language} voices`, native], ['Other voices', others]];
  for (const [label, list] of groups) {
    if (!list.length) continue;
    const group = element('optgroup');
    group.label = label;
    for (const voice of [...list].sort((a, b) => a.name.localeCompare(b.name))) group.append(new Option(voiceLabel(voice), voice.id));
    select.append(group);
  }
}

function voiceInput(backend, value, language) {
  if (!voices) {
    const input = element('input');
    input.type = 'text';
    input.value = value || '';
    input.placeholder = backend === 'gemini' ? 'Voice name' : 'Voice ID';
    return input;
  }
  const select = element('select');
  select.add(new Option('Choose a voice…', ''));
  const options = backend === 'gemini' ? voices.gemini.map((name) => ({ id: name, name })) : voices.elevenlabs;
  if (backend === 'gemini') {
    // Gemini voices are all multilingual: the same list serves every language.
    for (const voice of options) select.add(new Option(voice.name, voice.id));
  }
  if (backend !== 'gemini') addElevenLabsOptions(select, options, language);
  if (value && !options.some((voice) => voice.id === value)) select.add(new Option(`${value} (current)`, value));
  select.value = value || '';
  // An unavailable ElevenLabs catalog still needs an editable voice ID.
  if (backend === 'elevenlabs' && voices.elevenlabs_error) {
    const input = element('input');
    input.type = 'text';
    input.value = value || '';
    input.placeholder = 'ElevenLabs voice ID';
    input.setAttribute('list', 'elevenlabs-voices');
    return input;
  }
  return select;
}

function renderVoiceRows(languages) {
  const root = $('#language-rows');
  root.replaceChildren();
  const notice = voicesError || voices?.elevenlabs_error || '';
  showMessage($('#voices-notice'), notice ? `${notice} You can enter voice names or IDs manually where the catalog is unavailable.` : '');
  $('#elevenlabs-voices')?.remove();
  if (voices?.elevenlabs_error) {
    const list = element('datalist');
    list.id = 'elevenlabs-voices';
    for (const voice of voices.elevenlabs) list.append(new Option(voiceLabel(voice), voice.id));
    root.append(list);
  }
  const languageIds = [...new Set([...state.languages.map((item) => item.id), ...Object.keys(languages)])];
  for (const id of languageIds) {
    const row = element('div', 'language-row');
    row.dataset.language = id;
    const label = element('label', 'enable-language');
    const enabled = element('input');
    enabled.type = 'checkbox';
    enabled.checked = Object.hasOwn(languages, id);
    label.append(enabled, document.createTextNode(languageNames[id] || state.languages.find((item) => item.id === id)?.name || id));
    const fields = element('div', 'voice-fields');
    for (const backend of ['gemini', 'elevenlabs']) {
      const group = element('div', 'voice-control');
      const voiceLabel = element('label', '', backend === 'gemini' ? 'Gemini' : 'ElevenLabs');
      const input = voiceInput(backend, languages[id]?.voices?.[backend], id);
      input.dataset.backend = backend;
      voiceLabel.append(input);
      const test = button('▶', () => busy(test, async () => {
        if (!input.value.trim()) throw new Error('Choose or enter a voice first.');
        await post('/api/voices/test', { backend, voice: input.value.trim(), language: id, text: samples[id] || samples.en });
        toast('Voice sample played on this machine.');
      }));
      test.setAttribute('aria-label', `Test ${backend === 'gemini' ? 'Gemini' : 'ElevenLabs'} voice in ${languageNames[id] || id}`);
      group.append(voiceLabel, test);
      fields.append(group);
    }
    function updateEnabled() {
      for (const input of $$('input, select, button', fields)) input.disabled = !enabled.checked;
    }
    enabled.addEventListener('change', updateEnabled);
    row.append(label, fields);
    updateEnabled();
    root.append(row);
  }
}

function renderHooks(hooks) {
  $('#game-hooks').replaceChildren();
  for (const game of state.games) addHook(game.id, hooks[game.id] || '', true, game.name);
  for (const [id, text] of Object.entries(hooks)) {
    if (!state.games.some((game) => game.id === id)) addHook(id, text);
  }
}

function addHook(id = '', text = '', fixed = false, name = id) {
  const row = element('div', fixed ? 'hook-fixed' : 'hook-extra');
  row.dataset.hook = '';
  const idInput = element('input');
  idInput.type = fixed ? 'hidden' : 'text';
  idInput.value = id;
  idInput.dataset.hookId = '';
  if (!fixed) {
    idInput.required = true;
    idInput.placeholder = 'e.g. ksp';
    const idLabel = element('label', '', 'Game ID');
    idLabel.append(idInput);
    const remove = button('Remove', () => { row.remove(); setDirty(true); }, 'subtle');
    remove.setAttribute('aria-label', `Remove extra game hook${id ? ` ${id}` : ''}`);
    row.append(idLabel, remove);
  }
  if (fixed) row.append(idInput);
  const label = element('label', '', fixed ? `${name} (${id})` : 'Game context');
  const textarea = element('textarea');
  textarea.rows = 2;
  textarea.value = text;
  textarea.placeholder = 'You ride along…';
  label.append(textarea);
  row.append(label);
  $('#game-hooks').append(row);
  return row;
}

function collectLanguages() {
  if (!editor) return {};
  const languages = {};
  for (const row of $$('.language-row')) {
    if (!$('input[type=checkbox]', row).checked) continue;
    const id = row.dataset.language;
    const backendVoices = { ...editor.data.languages?.[id]?.voices };
    for (const input of $$('[data-backend]', row)) {
      const value = input.value.trim();
      delete backendVoices[input.dataset.backend];
      if (value) backendVoices[input.dataset.backend] = value;
    }
    languages[id] = { ...editor.data.languages?.[id], voices: backendVoices };
  }
  return languages;
}

function collectPersona() {
  const form = new FormData($('#editor-form'));
  const languages = collectLanguages();
  if (!Object.keys(languages).length) throw new Error('Enable at least one language.');
  const gameHooks = {};
  for (const row of $$('[data-hook]')) {
    const id = $('[data-hook-id]', row).value.trim();
    const text = $('textarea', row).value.trim();
    if (!id) throw new Error('Enter an ID for each extra game hook, or remove the empty row.');
    if (Object.hasOwn(gameHooks, id)) throw new Error(`Game hook “${id}” appears more than once.`);
    gameHooks[id] = text;
  }
  const { builtin, overrides_builtin: overridesBuiltin, ...original } = editor.data;
  return { ...original, id: form.get('id').trim(), display_name: form.get('display_name').trim(),
    source: form.get('source').trim(), truthfulness: form.get('truthfulness'),
    chattiness: Number(form.get('chattiness')), prompt: form.get('prompt').trim(),
    style_rules: form.get('style_rules').split('\n').map((line) => line.trim()).filter(Boolean),
    languages, game_hooks: gameHooks };
}

async function savePersona(event) {
  event.preventDefault();
  showMessage($('#editor-error'), '');
  const save = $('#save-persona');
  if (editorBusy) return;
  save.disabled = true;
  setEditorBusy(true);
  try {
    const data = collectPersona();
    if (editor.isNew && state.personas.some((persona) => persona.id === data.id)) throw new Error('That persona ID already exists. Choose a new ID, or edit the existing persona.');
    await api(personaPath(data.id), { method: 'PUT', body: JSON.stringify(data) });
    editor = { data: { ...data, builtin: false }, isNew: false };
    $('#edit-id').readOnly = true;
    $('#id-field').hidden = true;
    $('#editor-title').textContent = `Edit ${data.display_name}`;
    setDirty(false);
    showMessage($('#editor-notice'), '');
    toast('Persona saved.');
    await refreshState();
  } catch (error) {
    showMessage($('#editor-error'), error.message);
    $('#editor-error').scrollIntoView({ block: 'nearest' });
  } finally {
    save.disabled = false;
    setEditorBusy(false);
  }
}

async function deletePersona(persona) {
  if (!canLeaveEditor()) return;
  const question = persona.overrides_builtin
    ? `Reset ${persona.display_name} to the built-in version? Your custom copy will be removed.`
    : `Delete ${persona.display_name}? This cannot be undone.`;
  if (!window.confirm(question)) return;
  setEditorBusy(true);
  try {
    await api(personaPath(persona.id), { method: 'DELETE' });
    closeEditor();
    await refreshState();
    toast(reset ? 'Built-in persona restored.' : 'Custom persona removed.');
  } finally { setEditorBusy(false); }
}

async function draftPersona(event) {
  event.preventDefault();
  if (!canLeaveEditor()) return;
  const initialEditor = editor;
  const submit = $('button[type=submit]', event.currentTarget);
  const data = new FormData(event.currentTarget);
  showMessage($('#draft-error'), '');
  submit.disabled = true;
  submit.textContent = 'Drafting…';
  try {
    const body = { character: data.get('character').trim() };
    if (!body.character) throw new Error('Enter a character name.');
    for (const field of ['source', 'notes']) {
      if (data.get(field).trim()) body[field] = data.get(field).trim();
    }
    const draft = await post('/api/personas/draft', body);
    // The request can take a while. Protect any edits made while it was running.
    if (editorBusy) {
      showMessage($('#draft-error'), 'Another persona operation is in progress. Generate a new draft when it finishes.');
      return;
    }
    if (dirty && !canLeaveEditor()) return;
    if (!dirty && editor !== initialEditor && !window.confirm('Replace the current editor with the AI draft?')) return;
    fillEditor(draft, true, true);
    $('#draft-panel').hidden = true;
    toast('AI draft ready. Review it before saving.');
  } catch (error) { showMessage($('#draft-error'), error.message); }
  finally { submit.disabled = false; submit.textContent = 'Generate draft'; }
}

function scrollTranscript() {
  if (!followTranscript) return;
  const transcript = $('#transcript');
  transcript.scrollTop = transcript.scrollHeight;
}

function renderLine(line) {
  const transcript = $('#transcript');
  $('#transcript-empty')?.remove();
  const text = String(line.text ?? '');
  if (line.stream !== 'stderr' && /^listening(?:…|\.{3})?\s*$/iu.test(text.trim())) {
    $('#listening').hidden = false;
    return;
  }
  $('#listening').hidden = true;
  const match = line.stream === 'stdout' ? text.match(/^([^:]+?)(?: \(event: ([^)]+)\))?:\s*(.*)$/u) : null;
  const name = match?.[1];
  // A diagnostic with a colon is not a speaker. Accept the session persona and known names.
  const personaNames = new Set(state.personas.map((persona) => persona.display_name));
  if (state.session.persona) personaNames.add(state.session.persona);
  const isSpeech = match && (name === 'You' || personaNames.has(name) || Boolean(match[2]));
  if (!isSpeech) {
    transcript.append(element('div', `system-line${line.stream === 'stderr' ? ' stderr' : ''}`, text));
    lastBubble = null;
    scrollTranscript();
    return;
  }
  const [, speaker, kind = '', message] = match;
  const key = `${speaker}\u0000${kind}`;
  if (lastBubble?.key === key) {
    lastBubble.body.append(document.createTextNode(` ${message}`));
    scrollTranscript();
    return;
  }
  const bubble = element('article', `bubble${speaker === 'You' ? ' user' : ''}${criticalEvents.has(kind) ? ' alert' : ''}`);
  const heading = element('span', 'speaker', speaker);
  if (kind) heading.append(element('span', 'event-tag', kind.replaceAll('_', ' ')));
  const body = element('span', '', message);
  bubble.append(heading, body);
  transcript.append(bubble);
  lastBubble = { key, body };
  scrollTranscript();
}

function handleLine(event) {
  try {
    const line = JSON.parse(event.data);
    const key = JSON.stringify([line.ts, line.stream, line.text]);
    const replay = replayCounts.get(key) || 0;
    if (replay > 0) {
      replayCounts.set(key, replay - 1);
      return;
    }
    lineCounts.set(key, (lineCounts.get(key) || 0) + 1);
    // The server replays only 200 lines. Bound bookkeeping during long drives.
    if (lineCounts.size > 1000) lineCounts.delete(lineCounts.keys().next().value);
    renderLine(line);
  } catch { toast('An unreadable transcript event was received.', true); }
}

function connectEvents() {
  if (closed) return;
  source?.close();
  replayCounts = new Map(lineCounts);
  source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
  source.addEventListener('open', () => {
    reconnectDelay = 1000;
    $('#connection').textContent = 'Connected';
    $('#connection').classList.add('live');
  });
  source.addEventListener('line', handleLine);
  source.addEventListener('session', (event) => {
    try { applySession(JSON.parse(event.data)); }
    catch { toast('An unreadable session event was received.', true); }
  });
  source.addEventListener('error', () => {
    source.close();
    $('#connection').textContent = 'Reconnecting…';
    $('#connection').classList.remove('live');
    $('#listening').hidden = true;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectEvents, reconnectDelay);
    reconnectDelay = Math.min(30000, reconnectDelay * 2);
  });
}

async function quit() {
  if (!canLeaveEditor()) return;
  const control = $('#quit');
  await busy(control, async () => {
    await post('/api/shutdown');
    closed = true;
    setDirty(false);
    source?.close();
    clearTimeout(reconnectTimer);
    const message = element('section', 'closed');
    const icon = element('img');
    icon.src = '/familiar.svg';
    icon.alt = '';
    message.append(icon, element('h1', '', 'Familiar closed.'), element('p', 'muted', 'You can close this window.'));
    message.setAttribute('role', 'status');
    $('.shell').replaceChildren(message);
    message.tabIndex = -1;
    message.focus();
  });
}

function addLanguageControl() {
  const details = element('details');
  details.append(element('summary', '', 'Add language'));
  const form = element('form', 'actions');
  const fields = {};
  for (const [name, labelText, placeholder] of [['code', 'ISO code', 'pt-BR'], ['name', 'Name', 'Portuguese']]) {
    const label = element('label', '', labelText);
    const input = element('input');
    input.name = name;
    input.placeholder = placeholder;
    input.required = true;
    if (name === 'code') input.pattern = '[a-zA-Z]{2,3}(-[A-Z]{2}|-[0-9]{3})?';
    label.append(input);
    form.append(label);
    fields[name] = input;
  }
  const save = element('button', '', 'Add language');
  save.type = 'submit';
  form.append(save);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    busy(save, async () => {
      if (editorBusy) throw new Error('Please wait for the current persona operation to finish.');
      setEditorBusy(true);
      try {
        const currentLanguages = collectLanguages();
        const added = await post('/api/languages', { code: fields.code.value.trim(), name: fields.name.value.trim() });
        await refreshState();
        if (editor) renderVoiceRows(currentLanguages);
        toast(`${added.name} added. Enable it in the persona editor to use it.`);
      } finally { setEditorBusy(false); }
      form.reset();
      details.open = false;
    });
  });
  details.append(form);
  $('#language').parentElement.after(details);
}

function bindControls() {
  addLanguageControl();
  for (const tab of $$('[data-view]')) tab.addEventListener('click', () => switchView(tab.dataset.view));
  $('.brand').addEventListener('click', (event) => { event.preventDefault(); switchView('play'); });
  $('#language').addEventListener('change', (event) => {
    choices.language = event.target.value;
    writeStorage(storageKey, choices);
  });
  $('#session-toggle').addEventListener('click', toggleSession);
  $('#quit').addEventListener('click', quit);
  $('#new-persona').addEventListener('click', newPersona);
  $('#open-draft').addEventListener('click', () => {
    $('#draft-panel').hidden = false;
    $('input[name=character]').focus();
  });
  $('#close-draft').addEventListener('click', () => { $('#draft-panel').hidden = true; });
  $('#draft-form').addEventListener('submit', draftPersona);
  $('#editor-form').addEventListener('input', () => setDirty(true));
  $('#editor-form').addEventListener('change', () => setDirty(true));
  $('#edit-chattiness').addEventListener('input', (event) => { $('#chattiness-value').textContent = event.target.value; });
  $('#editor-form').addEventListener('submit', savePersona);
  $('#cancel-edit').addEventListener('click', () => { if (canLeaveEditor()) closeEditor(); });
  $('#add-hook').addEventListener('click', () => {
    const row = addHook();
    setDirty(true);
    $('[data-hook-id]', row).focus();
  });
  $('#transcript').addEventListener('scroll', () => {
    const node = $('#transcript');
    followTranscript = node.scrollHeight - node.scrollTop - node.clientHeight < 60;
    $('#jump-latest').hidden = followTranscript;
  });
  $('#jump-latest').addEventListener('click', () => {
    followTranscript = true;
    scrollTranscript();
    $('#jump-latest').hidden = true;
  });
  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  window.addEventListener('pagehide', () => { source?.close(); clearTimeout(reconnectTimer); });
  window.addEventListener('pageshow', (event) => { if (event.persisted && state && !closed) connectEvents(); });
}

async function init() {
  if (!token) {
    showMessage($('#load-error'), 'Missing launch token. Open the address provided by familiar gui, including its ?token=… query.');
    $('#main').setAttribute('aria-busy', 'false');
    $('#session-status').textContent = 'Waiting for an authenticated launcher URL.';
    return;
  }
  choices = readStorage(storageKey, {});
  if (!choices || typeof choices !== 'object') choices = {};
  const results = await Promise.allSettled([api('/api/state'), api('/api/voices')]);
  if (results[1].status === 'fulfilled') voices = results[1].value;
  if (results[1].status === 'rejected') voicesError = `Voice catalog unavailable: ${results[1].reason.message}`;
  if (results[0].status === 'rejected') {
    showMessage($('#load-error'), `Could not load Familiar: ${results[0].reason.message}`);
    const retry = button('Retry', () => { $('#load-error').hidden = true; init(); });
    $('#load-error').append(document.createTextNode(' '), retry);
    $('#main').setAttribute('aria-busy', 'false');
    $('#session-status').textContent = 'Could not connect to the launcher.';
    return;
  }
  const languages = await api('/api/languages');
  applyState({ ...results[0].value, languages });
  $('#main').setAttribute('aria-busy', 'false');
  $('#main').inert = false;
  connectEvents();
}

bindControls();
init().catch((error) => {
  showMessage($('#load-error'), error.message);
  $('#main').setAttribute('aria-busy', 'false');
});

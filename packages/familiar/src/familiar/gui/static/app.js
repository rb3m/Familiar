const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const token = new URLSearchParams(location.search).get('token') || '';
let languageNames = {};
let englishLanguageNames = {};
const samples = {
  en: 'Hello! I’m ready for our next adventure.',
  es: '¡Hola! Estoy lista para nuestra próxima aventura.',
  ja: 'こんにちは！次の冒険に出かけましょう。',
};
const criticalEvents = new Set(['fuel_low', 'air_pressure', 'engine_warning', 'fined', 'job_cancelled']);
let state;
let ptt;
let pttProposal;
let pttBusy = false;
let choices = { game: null, crew: [], carrier: '', dj: '', language: '', banter: 2, stereo: false, seats: {} };
let saveTimer;
let saveFailed = false;
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
let lastPersonaSentence = null;
let localVoiceTimer;
let replayCounts = new Map();
const lineCounts = new Map();

async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  headers.set('X-Familiar-Token', token);
  if (options.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers });
  const body = await response.text();
  let data;
  try { data = body ? JSON.parse(body) : null; }
  catch { throw new Error(response.ok ? 'The server returned invalid JSON.' : `Request failed (${response.status}).`); }
  if (!response.ok) {
    const error = new Error(data?.error || `Request failed (${response.status}).`);
    error.problems = Array.isArray(data?.problems) ? data.problems : [];
    throw error;
  }
  return data;
}

const post = (path, body) => api(path, {
  method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body) });
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

function personaById(id) {
  return state?.personas.find((persona) => persona.id === id);
}

function personaName(id) {
  return personaById(id)?.display_name || id;
}

const SEATS = [
  ['Left (-0.7)', -0.7],
  ['Centre-left (-0.35)', -0.35],
  ['Centre (0)', 0],
  ['Centre-right (0.35)', 0.35],
  ['Right (0.7)', 0.7],
];

function defaultSeats(crew, carrier) {
  const seats = {};
  let others = 0;
  for (const id of crew) {
    if (id === carrier) { seats[id] = 0; continue; }
    seats[id] = others % 2 === 0 ? 0.6 : -0.6;
    others += 1;
  }
  return seats;
}

function seatOf(id) {
  const pan = choices.seats[id];
  if (Number.isFinite(pan) && pan >= -1 && pan <= 1) return pan;
  return defaultSeats(choices.crew, choices.carrier)[id] ?? 0;
}

function normalizeChoices(preferred = choices) {
  const defaults = state.defaults;
  const validGame = (id) => id === null || state.games.some((game) => game.id === id);
  const validPersona = (id) => state.personas.some((persona) => persona.id === id);
  const validLanguage = (id) => state.languages.some((language) => language.id === id);
  const game = validGame(preferred.game) ? preferred.game : (validGame(defaults.game) ? defaults.game : null);
  const crew = [];
  for (const id of Array.isArray(preferred.crew) ? preferred.crew : []) {
    if (validPersona(id) && !crew.includes(id) && crew.length < 3) crew.push(id);
  }
  if (!crew.length && state.personas.length) {
    crew.push(validPersona(defaults.persona) ? defaults.persona : state.personas[0].id);
  }
  const carrier = crew.includes(preferred.carrier) ? preferred.carrier : (crew[0] || '');
  const dj = crew.includes(preferred.dj) ? preferred.dj : carrier;
  const language = validLanguage(preferred.language) ? preferred.language
    : (validLanguage(defaults.language) ? defaults.language : state.languages[0]?.id || '');
  const banter = Number.isInteger(preferred.banter) && preferred.banter >= 0 && preferred.banter <= 10 ? preferred.banter : 2;
  const stereo = typeof preferred.stereo === 'boolean' ? preferred.stereo : choices.stereo;
  const source = preferred.seats && typeof preferred.seats === 'object' ? preferred.seats : choices.seats;
  const seats = defaultSeats(crew, carrier);
  for (const id of crew) {
    const pan = Number(source[id]);
    if (Number.isFinite(pan) && pan >= -1 && pan <= 1) seats[id] = pan;
  }
  choices = { game, crew, carrier, dj, language, banter, stereo, seats };
}

function settingsToChoices(settings) {
  return {
    game: settings.game ?? null,
    crew: Array.isArray(settings.crew) ? [...settings.crew] : [],
    carrier: typeof settings.carrier === 'string' ? settings.carrier : '',
    dj: typeof settings.dj === 'string' ? settings.dj : '',
    language: typeof settings.language === 'string' ? settings.language : '',
    banter: settings.banter,
    stereo: settings.stereo === true,
    seats: settings.seats && typeof settings.seats === 'object' ? { ...settings.seats } : {},
  };
}

function banterLabel(value) {
  if (value === 0) return 'Off';
  if (value <= 3) return 'Rare';
  if (value <= 6) return 'Sometimes';
  if (value <= 9) return 'Often';
  return 'Chatty';
}

function showCrewProblems(problems) {
  const root = $('#crew-problems');
  root.replaceChildren();
  for (const problem of problems) root.append(element('li', '', problem));
  root.hidden = problems.length === 0;
}

function showSaveStatus(message) {
  $('#save-status').textContent = message;
}

async function saveSettings() {
  const seats = Object.fromEntries(choices.crew.map((id) => [id, seatOf(id)]));
  const payload = { version: 3, ...choices, seats };
  try {
    await put('/api/session/settings', payload);
    saveFailed = false;
    showSaveStatus('Saved');
    showCrewProblems([]);
  } catch (error) {
    saveFailed = true;
    showSaveStatus('Could not save');
    showCrewProblems(error.problems.length ? error.problems : [error.message]);
  }
  updateStartAvailability();
}

function scheduleSave() {
  if (state.session.running) return;
  clearTimeout(saveTimer);
  showSaveStatus('Saving…');
  saveTimer = setTimeout(saveSettings, 400);
}

async function flushSave() {
  clearTimeout(saveTimer);
  await saveSettings();
}

function renderGames() {
  const root = $('#games');
  root.replaceChildren();
  for (const game of [{ id: null, name: 'No game', description: 'Desk chat' }, ...state.games]) {
    const node = button('', () => {
      choices.game = game.id;
      renderGames();
      renderCrew();
      scheduleSave();
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

function seatSelect(id) {
  const select = element('select', 'crew-seat');
  select.setAttribute('aria-label', `Stereo seat for ${personaName(id)}`);
  const pan = seatOf(id);
  for (const [label, value] of SEATS) select.add(new Option(label, String(value)));
  if (!SEATS.some(([, value]) => value === pan)) select.add(new Option('Custom', String(pan)));
  select.value = String(pan);
  select.disabled = state.session.running || sessionBusy;
  select.addEventListener('change', (event) => {
    choices.seats[id] = Number(event.target.value);
    renderCrew();
    scheduleSave();
  });
  return select;
}

function crewSlot(id, index) {
  const row = element('div', 'crew-slot');
  const label = element('label', 'crew-member', `Crew member ${index + 1}`);
  const select = element('select');
  for (const persona of state.personas) {
    const option = new Option(persona.display_name, persona.id);
    option.disabled = choices.crew.includes(persona.id) && persona.id !== id;
    select.add(option);
  }
  select.value = id;
  select.disabled = state.session.running || sessionBusy;
  select.addEventListener('change', (event) => {
    choices.crew[index] = event.target.value;
    renderCrew();
    scheduleSave();
  });
  label.append(select);
  if (choices.stereo) label.append(seatSelect(id));
  const actions = element('div', 'crew-actions');
  const locked = state.session.running || sessionBusy;
  const up = button('↑', () => moveCrew(index, -1), 'subtle');
  up.disabled = locked || index === 0;
  up.setAttribute('aria-label', `Move ${personaName(id)} earlier in the roster`);
  const down = button('↓', () => moveCrew(index, 1), 'subtle');
  down.disabled = locked || index === choices.crew.length - 1;
  down.setAttribute('aria-label', `Move ${personaName(id)} later in the roster`);
  const remove = button('Remove', () => removeCrew(index), 'subtle danger');
  remove.disabled = locked || choices.crew.length <= 1;
  remove.setAttribute('aria-label', `Remove ${personaName(id)} from the crew`);
  actions.append(up, down, remove);
  row.append(label, actions);
  const persona = personaById(id);
  const role = choices.game ? persona?.roles?.[choices.game] : null;
  const missingVoice = persona && choices.language && !persona.languages.includes(choices.language);
  if (role || missingVoice) {
    const meta = element('div', 'crew-meta');
    if (role) meta.append(element('span', 'crew-role muted', role));
    if (missingVoice) meta.append(element('span', 'crew-warning', `No voice for ${languageNames[choices.language] || choices.language}`));
    row.append(meta);
  }
  return row;
}

function moveCrew(index, delta) {
  const [member] = choices.crew.splice(index, 1);
  choices.crew.splice(index + delta, 0, member);
  renderCrew();
  scheduleSave();
  $$('#crew-slots .crew-slot')[index + delta]?.querySelector('select')?.focus();
}

function removeCrew(index) {
  choices.crew.splice(index, 1);
  renderCrew();
  scheduleSave();
}

function addCrewMember() {
  const free = state.personas.find((persona) => !choices.crew.includes(persona.id));
  if (!free || choices.crew.length >= 3) return;
  choices.crew.push(free.id);
  renderCrew();
  scheduleSave();
  $$('#crew-slots .crew-slot').at(-1)?.querySelector('select')?.focus();
}

// Roles stay on crew members: controls fall back to the first member, music to the controls.
function keepRoles() {
  if (!choices.crew.includes(choices.carrier)) choices.carrier = choices.crew[0] || '';
  if (!choices.crew.includes(choices.dj)) choices.dj = choices.carrier;
}

function renderRoleSelect(selector, value) {
  const select = $(selector);
  select.replaceChildren();
  for (const id of choices.crew) select.add(new Option(personaName(id), id));
  select.value = value;
  select.disabled = state.session.running || sessionBusy || !choices.crew.length;
}

function renderRoles() {
  keepRoles();
  renderRoleSelect('#carrier', choices.carrier);
  renderRoleSelect('#dj', choices.dj);
  // A crew of one does both jobs; the music picker only matters with company aboard.
  $('#dj-field').hidden = choices.crew.length < 2;
}

function renderBanter() {
  const slider = $('#banter');
  slider.value = String(choices.banter);
  slider.disabled = state.session.running || sessionBusy;
  $('#banter-label').textContent = banterLabel(choices.banter);
}

function renderStereo() {
  const toggle = $('#stereo');
  toggle.checked = choices.stereo;
  toggle.disabled = state.session.running || sessionBusy;
}

function renderCrew() {
  const root = $('#crew-slots');
  root.replaceChildren();
  $('#no-personas').hidden = state.personas.length > 0;
  choices.crew.forEach((id, index) => root.append(crewSlot(id, index)));
  $('#crew-add').disabled = state.session.running || sessionBusy
    || choices.crew.length >= 3 || choices.crew.length >= state.personas.length;
  renderRoles();
  renderBanter();
  renderStereo();
}

function renderLibrary() {
  const library = $('#persona-list');
  library.replaceChildren();
  if (!state.personas.length) library.append(element('p', 'muted', 'No personas yet. Create your first companion.'));
  for (const persona of state.personas) {
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
  for (const language of state.languages) {
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

function renderLocalVoice(status) {
  const root = $('#local-voice');
  root.replaceChildren();
  root.hidden = !status;
  if (!status) return;
  const labels = {
    ready: 'Ready', loading: 'Loading…', idle: 'Idle · loads when a session starts',
    stopping: 'Stopping', failed: 'Worker failed', unreachable: 'Unreachable',
    unauthorized: 'Unauthorized', unconfigured: 'Unconfigured',
  };
  root.dataset.state = status.state;
  const heading = element('strong');
  const dot = element('span', 'voice-status-dot');
  dot.setAttribute('aria-hidden', 'true');
  heading.append(dot, document.createTextNode(`Local voice · ${labels[status.state] || status.state}`));
  root.append(heading);
  const error = status.error || (status.state === 'unauthorized' ? 'Check the local voice bearer token.' : '');
  if (error) root.append(element('div', '', error));
  if (error.includes('No route to host')) {
    root.append(element('div', '', 'macOS blocks Familiar.app from the LAN. Install the voice tunnel: packaging/macos/install-voice-tunnel.sh.'));
  }
}

async function pollLocalVoice() {
  clearTimeout(localVoiceTimer);
  if (document.hidden || closed || !state) return;
  try { renderLocalVoice(await api('/api/local-voice')); }
  catch (error) { renderLocalVoice({ state: 'unreachable', error: error.message }); }
  finally {
    if (!document.hidden && !closed) localVoiceTimer = setTimeout(pollLocalVoice, 10000);
  }
}

function tagVoice(name) {
  if (!lastPersonaSentence) return;
  const { sentence, heading } = lastPersonaSentence;
  sentence.dataset.voice = name;
  if (Array.from(heading.querySelectorAll('.voice-label')).some((label) => label.dataset.voice === name)) return;
  const label = element('span', 'voice-label', new Map([['local', 'Local'], ['elevenlabs', 'ElevenLabs'], ['gemini', 'Gemini']]).get(name) || name);
  label.dataset.voice = name;
  heading.append(label);
}

function updateStartAvailability() {
  const toggle = $('#session-toggle');
  toggle.disabled = sessionBusy || pttBusy || (!state.session.running && (saveFailed || !choices.crew.length || !choices.language));
}

function lockSetupControls(locked) {
  if (!locked) {
    renderGames();
    renderCrew();
    renderLanguage();
    return;
  }
  for (const control of $$('#games button, #crew-slots button, #crew-slots select')) control.disabled = true;
  $('#crew-add').disabled = true;
  $('#carrier').disabled = true;
  $('#dj').disabled = true;
  $('#banter').disabled = true;
  $('#stereo').disabled = true;
  $('#language').disabled = true;
}

function renderSession() {
  const session = state.session;
  const toggle = $('#session-toggle');
  toggle.textContent = sessionBusy ? 'Please wait…' : (session.running ? 'Stop companion' : 'Start companion');
  toggle.classList.toggle('running', session.running);
  updateStartAvailability();
  let message = 'Ready when you are.';
  if (session.running) {
    const crew = (Array.isArray(session.crew) && session.crew.length ? session.crew : [session.persona]).filter(Boolean);
    const names = crew.map((id) => personaName(id)).join(', ');
    const carrier = session.carrier ? `${personaName(session.carrier)} takes the controls · ` : '';
    const dj = session.dj && session.dj !== session.carrier ? `${personaName(session.dj)} on the music · ` : '';
    const date = session.started_at ? new Date(session.started_at) : null;
    const since = date && !Number.isNaN(date.getTime()) ? ` · running since ${date.toLocaleString()}` : ' · running';
    const game = state.games.find((item) => item.id === session.game)?.name || session.game || 'desk chat';
    message = `${names || 'Companion'} · ${carrier}${dj}${game} · ${session.language || ''}${since}`;
  }
  if (!session.running && session.exit_code !== null) message = `Session stopped · exit code ${session.exit_code}`;
  $('#session-status').textContent = message;
  if (!session.running) $('#listening').hidden = true;
  lockSetupControls(session.running || sessionBusy);
  renderPTT();
}

function applySession(session) {
  const wasRunning = state.session.running;
  state.session = session;
  if (session.running) {
    normalizeChoices({
      game: session.game,
      crew: session.crew,
      carrier: session.carrier,
      dj: session.dj,
      language: session.language,
      banter: session.banter,
    });
  }
  if (wasRunning !== session.running) {
    renderGames();
    renderCrew();
    renderLibrary();
    renderLanguage();
  }
  renderSession();
}

function applyState(nextState) {
  state = nextState;
  languageNames = Object.fromEntries(state.languages.map((item) => [item.id, item.native || item.name]));
  englishLanguageNames = Object.fromEntries(state.languages.map((item) => [item.id, item.name]));
  normalizeChoices(state.session.running ? settingsToChoices(state.session) : choices);
  renderGames();
  renderCrew();
  renderLibrary();
  renderLanguage();
  renderSession();
  renderQuota();
  renderLocalVoice(state.local_voice);
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
    if (!state.session.running) {
      await flushSave();
      if (saveFailed) throw new Error('Fix the crew settings above before starting.');
    }
    const session = state.session.running
      ? await post('/api/session/stop')
      : await post('/api/session/start');
    applySession(session);
  } catch (error) {
    if (error.problems?.length) showCrewProblems(error.problems);
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
  $('#edit-music').value = (data.tastes?.music || []).join('\n');
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
    style_rules: lines(form.get('style_rules')),
    tastes: withTaste(original.tastes, 'music', lines(form.get('music_taste'))),
    languages, game_hooks: gameHooks };
}

function lines(text) {
  return text.split('\n').map((line) => line.trim()).filter(Boolean);
}

// Set one taste topic, keeping the others; an empty list removes the topic.
function withTaste(tastes, topic, likes) {
  const { [topic]: _previous, ...others } = tastes || {};
  return likes.length ? { ...others, [topic]: likes } : others;
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
  const text = String(line.text ?? '');
  const voice = line.stream === 'stdout' ? text.match(/^\[voice\] (\S+)\s*$/u) : null;
  if (voice) { tagVoice(voice[1]); return; }
  const transcript = $('#transcript');
  $('#transcript-empty')?.remove();
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
  const sentence = element('span', 'sentence', message);
  if (lastBubble?.key === key) {
    lastBubble.body.append(document.createTextNode(' '), sentence);
    if (speaker !== 'You') lastPersonaSentence = { sentence, heading: lastBubble.heading };
    scrollTranscript();
    return;
  }
  const bubble = element('article', `bubble${speaker === 'You' ? ' user' : ''}${criticalEvents.has(kind) ? ' alert' : ''}`);
  const heading = element('span', 'speaker', speaker);
  if (kind) heading.append(element('span', 'event-tag', kind.replaceAll('_', ' ')));
  const body = element('span');
  body.append(sentence);
  if (speaker !== 'You') lastPersonaSentence = { sentence, heading };
  bubble.append(heading, body);
  transcript.append(bubble);
  lastBubble = { key, body, heading };
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

function renderPTT() {
  const locked = Boolean(state?.session.running || sessionBusy || pttBusy);
  $('#ptt-label').textContent = pttProposal ? `Add: ${pttProposal.label}` : '';
  const list = $('#ptt-bindings');
  list.replaceChildren();
  for (const [index, binding] of (ptt?.bindings || []).entries()) {
    const row = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = binding.label;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.disabled = locked || ptt.bindings.length === 1;
    remove.addEventListener('click', () => persistPTT(ptt.bindings.filter((_, i) => i !== index)));
    row.append(label, remove);
    list.append(row);
  }
  const kinds = ptt?.kinds || (ptt?.kind ? [ptt.kind] : []);
  const assignments = $('#ptt-assignments');
  assignments.replaceChildren();
  for (const kind of kinds.length ? kinds : [null]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = kinds.length > 1
      ? ({ mouse: 'Assign mouse button…', key: 'Assign key…', joystick: 'Assign joystick button…' }[kind] || 'Assign…')
      : 'Assign…';
    button.disabled = locked || !ptt?.detectable;
    button.addEventListener('click', () => assignPTT(kind));
    assignments.append(button);
  }
  $('#ptt-save').hidden = $('#ptt-cancel').hidden = !pttProposal;
  $('#ptt-save').disabled = $('#ptt-cancel').disabled = locked;
}

async function assignPTT(kind) {
  const control = kind === 'key' ? 'key' : 'button';
  pttBusy = true;
  pttProposal = null;
  $('#ptt-status').textContent = `Press the ${control} now…`;
  renderPTT();
  updateStartAvailability();
  try {
    pttProposal = await post('/api/ptt/detect', { timeout: 30, kind });
    $('#ptt-status').textContent = `Save this ${control} for push-to-talk?`;
  } catch (error) {
    $('#ptt-status').textContent = error.message;
  } finally {
    pttBusy = false;
    renderPTT();
    updateStartAvailability();
  }
}

async function savePTT() {
  const bindings = (ptt?.bindings || []).filter(binding => binding.backend !== 'terminal');
  await persistPTT([...bindings, pttProposal]);
}

async function persistPTT(bindings) {
  pttBusy = true;
  renderPTT();
  updateStartAvailability();
  try {
    ptt = await put('/api/ptt', { bindings: bindings.map(({ backend, settings }) => ({ backend, settings })) });
    pttProposal = null;
    $('#ptt-status').textContent = 'Push-to-talk saved.';
  } catch (error) {
    $('#ptt-status').textContent = error.message;
  } finally {
    pttBusy = false;
    renderPTT();
    updateStartAvailability();
  }
}

function bindControls() {
  $('#ptt-save').addEventListener('click', savePTT);
  $('#ptt-cancel').addEventListener('click', () => {
    pttProposal = null;
    $('#ptt-status').textContent = '';
    renderPTT();
  });
  addLanguageControl();
  for (const tab of $$('[data-view]')) tab.addEventListener('click', () => switchView(tab.dataset.view));
  $('.brand').addEventListener('click', (event) => { event.preventDefault(); switchView('play'); });
  $('#language').addEventListener('change', (event) => {
    choices.language = event.target.value;
    renderCrew();
    scheduleSave();
  });
  $('#crew-add').addEventListener('click', addCrewMember);
  $('#carrier').addEventListener('change', (event) => {
    choices.carrier = event.target.value;
    scheduleSave();
  });
  $('#dj').addEventListener('change', (event) => {
    choices.dj = event.target.value;
    scheduleSave();
  });
  $('#banter').addEventListener('input', (event) => {
    choices.banter = Number(event.target.value);
    $('#banter-label').textContent = banterLabel(choices.banter);
    scheduleSave();
  });
  $('#stereo').addEventListener('change', (event) => {
    choices.stereo = event.target.checked;
    renderCrew();
    scheduleSave();
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
  document.addEventListener('visibilitychange', pollLocalVoice);
  window.addEventListener('pagehide', () => { source?.close(); clearTimeout(reconnectTimer); clearTimeout(localVoiceTimer); });
  window.addEventListener('pageshow', (event) => { if (event.persisted && state && !closed) connectEvents(); });
}

async function init() {
  if (!token) {
    showMessage($('#load-error'), 'Missing launch token. Open the address provided by familiar gui, including its ?token=… query.');
    $('#main').setAttribute('aria-busy', 'false');
    $('#session-status').textContent = 'Waiting for an authenticated launcher URL.';
    return;
  }
  const results = await Promise.allSettled([api('/api/state'), api('/api/voices'), api('/api/session/settings'), api('/api/ptt')]);
  if (results[3].status === 'fulfilled') ptt = results[3].value;
  if (results[3].status === 'rejected') $('#ptt-status').textContent = results[3].reason.message;
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
  if (results[2].status === 'fulfilled') {
    choices = settingsToChoices(results[2].value);
    if (results[2].value.problems?.length) {
      saveFailed = true;
      showSaveStatus('Saved session has problems');
      showCrewProblems(results[2].value.problems);
    }
  }
  const languages = await api('/api/languages');
  applyState({ ...results[0].value, languages });
  // A repaired selection (invalid entries dropped by normalizeChoices) is persisted once.
  if (saveFailed) scheduleSave();
  $('#main').setAttribute('aria-busy', 'false');
  $('#main').inert = false;
  connectEvents();
  localVoiceTimer = setTimeout(pollLocalVoice, 10000);
}

bindControls();
init().catch((error) => {
  showMessage($('#load-error'), error.message);
  $('#main').setAttribute('aria-busy', 'false');
});

/* The Icons page: the home of the pictures a button can wear.
 *
 * Three rules shape this page.
 *
 * Built-in sets are read-only.  They ship with the app, so the page never offers to
 * change one; the honest way to change a shipped icon is to copy it into a set of
 * a custom set and edit the copy.  The server refuses a write to a built-in set as well,
 * so the rule holds whether or not the browser behaves.
 *
 * A set exists because it has a picture in it.  The loader reads a set from its
 * manifest, and a manifest with no icons is not a set: so "New set" is not a form
 * that makes an empty shell, it is the name you give the set your first pictures land
 * in.
 *
 * Your picture is what you uploaded.  The browser makes the two tile sizes the deck
 * paints (60px for the key face, 180px for this page) and sends the original file
 * too, so nothing about your art is thrown away - and nothing is recoloured, cropped
 * or redrawn on the way in.
 */

let iconLibrary = null;     // the whole library, as the server describes it
let iconSet = null;         // the set being looked at
let iconNewSetOpen = false; // the "New set" form is showing
let iconRenaming = null;    // `set/name` of the icon whose label is being edited
let iconBusy = false;       // one write at a time, and the page says so
// Messages this page shows live in the page bar's own status line, not in the page: the bar
// sits outside the icons root, so a re-render cannot wipe the words, and every other page
// reports in the same place.  This page used to keep its own line at the top of the body,
// which meant a second message area in the row its two buttons now live in.
let iconCopy = null;         // a built-in icon waiting for a set to be copied into
let iconSetRenaming = false;  // the set's own name is being edited
let iconSetDeleting = false;  // the whole set is being deleted, and has been asked about

const TILE_SIZES = [60, 180];

function iconRoot() {
  return document.getElementById('iconsRoot');
}

function iconSetById(id) {
  if (!iconLibrary) return null;
  return (iconLibrary.packs || []).find(pack => pack.id === id) || null;
}

function iconIsBuiltIn(id) {
  return (iconLibrary.builtin || []).includes(id);
}

function iconEsc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// One status line for the whole app: the page bar's, which every other page already uses.
// The bar fades news that worked and keeps a refusal on screen, which is what this page's own
// line used to do.  `ok` is that news, so it goes over as `saved`.
function iconStatus(text, tone) {
  if (typeof setStatus !== 'function') return;
  setStatus(tone === 'ok' ? 'saved' : (tone || ''), String(text || ''));
}

// ── talking to the server ────────────────────────────────────────────────────

async function loadIconLibrary() {
  try {
    const response = await fetch('/api/icons');
    const payload = await response.json();
    iconLibrary = payload;
    if (!iconSet || !iconSetById(iconSet)) {
      // Open on a custom set when you have one: the built-in sets are there to
      // browse, but the work happens in a custom set.
      const mine = (payload.packs || []).filter(pack => !iconIsBuiltIn(pack.id));
      iconSet = mine.length ? mine[0].id : (payload.packs || [])[0]?.id || null;
    }
  } catch (error) {
    iconLibrary = null;
    iconStatus('The icon library could not be read.', 'error');
  }
}

async function sendIconChange(body) {
  if (iconBusy) return null;
  iconBusy = true;
  iconStatus('Working…');
  try {
    const response = await fetch('/api/icons', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!payload.ok) {
      iconStatus(payload.reason || 'That change was refused.', 'error');
      return null;
    }
    if (payload.library) iconLibrary = payload.library;
    iconStatus(payload.detail || 'Done.', 'ok');
    return payload;
  } catch (error) {
    iconStatus('The change did not reach the deck.', 'error');
    return null;
  } finally {
    iconBusy = false;
    renderIcons();
  }
}

// ── your picture in, exactly as you drew it ──────────────────────────────────

function iconReadFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function iconLoadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('that file is not a picture this browser can read'));
    image.src = dataUrl;
  });
}

function iconTileDataUrl(image, size) {
  // A transparent canvas, drawn to fit without stretching the art: the deck's key is
  // 60px square, and a logo squashed to fill it would not be the picture you chose.
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const pen = canvas.getContext('2d');
  const scale = Math.min(size / image.width, size / image.height);
  const width = Math.max(1, Math.round(image.width * scale));
  const height = Math.max(1, Math.round(image.height * scale));
  pen.imageSmoothingQuality = 'high';
  pen.drawImage(image, Math.round((size - width) / 2), Math.round((size - height) / 2), width, height);
  return canvas.toDataURL('image/png');
}

function iconBase64(dataUrl) {
  return String(dataUrl).split(',')[1] || '';
}

function iconNameFromFilename(filename) {
  const stem = String(filename).replace(/\.[^.]+$/, '') || 'icon';
  const slug = stem.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return slug || 'icon';
}

async function iconPayloadFromFile(file) {
  const dataUrl = await iconReadFile(file);
  const image = await iconLoadImage(dataUrl);
  const tiles = {};
  for (const size of TILE_SIZES) tiles[size] = iconBase64(iconTileDataUrl(image, size));
  return {
    name: iconNameFromFilename(file.name),
    label: String(file.name).replace(/\.[^.]+$/, '').slice(0, 32),
    tiles,
    source: {name: file.name, data: iconBase64(dataUrl)},
  };
}

async function addIconFiles(files) {
  const list = [...files];
  if (!list.length) return;
  const target = iconSet;
  if (!target || iconIsBuiltIn(target)) {
    iconStatus('Pick a custom set first, or make one — the built-in sets never change.', 'error');
    return;
  }
  if (iconBusy) return;
  iconBusy = true;
  iconStatus(`Reading ${list.length} picture${list.length === 1 ? '' : 's'}…`);
  let icons;
  try {
    icons = [];
    for (const file of list) icons.push(await iconPayloadFromFile(file));
  } catch (error) {
    iconBusy = false;
    iconStatus(String(error.message || error), 'error');
    renderIcons();
    return;
  }
  iconBusy = false;
  await sendIconChange({op: 'add', set: target, set_name: iconSetById(target)?.name || target, icons});
  if (iconRenaming === null) renderIcons();
}

async function createSet(setName) {
  const label = String(setName || '').trim().slice(0, 40);
  const set = iconNameFromFilename(label);
  if (!label || !set) {
    iconStatus('Give the set a name.', 'error');
    return;
  }
  if ((iconLibrary.builtin || []).includes(set)) {
    iconStatus(`“${set}” is a built-in set. Pick another name.`, 'error');
    return;
  }
  // A set is a container: naming it is this whole step, and the pictures come
  // afterwards, into the set the person is left standing in.  Asking for a picture
  // here made the name conditional on something that belongs in the next step.
  const made = await sendIconChange({op: 'set-create', set, set_name: label});
  if (!made) return;
  iconNewSetOpen = false;
  iconSet = set;
  renderIcons();
}

// ── what the page looks like ─────────────────────────────────────────────────

// Which icon is picked, `<set>/<name>`.  The tiles are selections: what you can do to the
// picked one is stated once, in the bar above the grid, instead of on every tile.
let iconPicked = null;

function iconTileHTML(pack, icon) {
  const mine = !iconIsBuiltIn(pack.id);
  const key = `${pack.id}/${icon.name}`;
  const preview = icon.preview || `/api/icons/${encodeURIComponent(icon.reference || '')}.png`;
  const renaming = iconRenaming === key;
  const picked = iconPicked === key;
  return `<figure class="ic-tile${picked ? ' is-picked' : ''}" data-ic-key="${iconEsc(key)}"
      data-ic-pick="${iconEsc(key)}" role="button" tabindex="0" aria-pressed="${picked ? 'true' : 'false'}"
      title="${iconEsc(icon.reference || key)}">
    <span class="ic-face"><img src="${iconEsc(preview)}" alt="" width="60" height="60"></span>
    <figcaption>
      ${renaming
        ? `<form class="ic-rename" data-ic-rename="${iconEsc(key)}">
             <input type="text" maxlength="32" value="${iconEsc(icon.label)}" aria-label="Icon name">
             <button type="submit" class="primary">Save</button>
             <button type="button" class="quiet" data-ic-cancel>Cancel</button>
           </form>`
        : `<b class="ic-label">${iconEsc(icon.label)}</b>`}
    </figcaption>
  </figure>`;
}

// What can be done to the picked icon, said once.  A picture from a built-in set can
// only be copied, because the built-in sets never change.  A picture of your own can be
// renamed, copied on into another of your sets, or deleted.
function iconPickedToolsHTML(pack, icon) {
  const mine = !iconIsBuiltIn(pack.id);
  const key = `${pack.id}/${icon.name}`;
  return `<span class="ic-picked-label">Selected: <b>${iconEsc(icon.label || icon.name)}</b></span>
    ${mine
      ? `<button type="button" class="quiet" data-ic-relabel="${iconEsc(key)}">Rename…</button>
         <button type="button" class="quiet" data-ic-copy="${iconEsc(key)}"
           title="Copy this icon into another of your sets">Copy…</button>
         <button type="button" class="quiet is-danger" data-ic-delete="${iconEsc(key)}">Delete</button>`
      : `<button type="button" class="quiet" data-ic-copy="${iconEsc(key)}"
           title="Copy this icon into a custom set, where you can change it">Copy…</button>`}`;
}

function iconSetsHTML() {
  const packs = iconLibrary.packs || [];
  const builtIn = packs.filter(pack => iconIsBuiltIn(pack.id));
  const mine = packs.filter(pack => !iconIsBuiltIn(pack.id));
  const group = (title, list, note) => `
    <div class="ic-group">
      <p class="ic-group-label">${title}</p>
      ${note ? `<p class="ic-note">${note}</p>` : ''}
      ${list.map(pack => `
        <button type="button" class="ic-set${pack.id === iconSet ? ' on' : ''}" data-ic-set="${iconEsc(pack.id)}">
          <b>${iconEsc(pack.name)}</b>
          <small>${pack.icons.length} icon${pack.icons.length === 1 ? '' : 's'}</small>
        </button>`).join('')}
    </div>`;
  return `
    ${group('Built-in sets', builtIn, 'Read-only: copy one into a custom set to change it.')}
    ${group('Custom sets', mine, mine.length ? '' : 'No custom set yet — use “＋ New set…” above.')}`;
}

function iconChooserHTML() {
  const {set, name} = iconKeyParts(iconCopy);
  const icon = (iconSetById(set)?.icons || []).find(entry => entry.name === name) || {};
  const label = icon.label || name;
  // The set the picture came from is left out: a copy of it inside itself is not a copy.
  const mine = (iconLibrary.packs || [])
    .filter(pack => !iconIsBuiltIn(pack.id) && pack.id !== set);
  return `<div class="ic-chooser">
    <p class="ic-chooser-q">Copy “<b>${iconEsc(label)}</b>” to which custom set?</p>
    ${mine.length
      ? `<div class="ic-chooser-rows">${mine.map(pack => `
          <button type="button" class="ic-chooser-row" data-ic-copyset="${iconEsc(pack.id)}">
            <b>${iconEsc(pack.name)}</b>
            <small>${pack.icons.length} icon${pack.icons.length === 1 ? '' : 's'}</small>
          </button>`).join('')}</div>`
      : `<p class="ic-note">You have no custom sets yet — name one here and
         “${iconEsc(label)}” goes straight into it.</p>`}
    <form class="ic-chooser-new" data-ic-copynew>
      <input type="text" id="icCopyNewName" maxlength="40" placeholder="Name a new set"
             autocomplete="off" aria-label="New set name">
      <button type="submit" class="ic-add">Make it and copy</button>
      <button type="button" class="ic-tiny" data-ic-copycancel>Cancel</button>
    </form>
  </div>`;
}

function iconWorkHTML() {
  const pack = iconSetById(iconSet);
  if (!pack) {
    return `<div class="ic-empty">Choose a set on the left, or make a custom set.</div>`;
  }
  const mine = !iconIsBuiltIn(pack.id);
  const addRow = mine
    ? `<div class="ic-actions">
         <label class="ic-add" for="icFiles">＋ Add icons…</label>
         <input id="icFiles" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden>
         <span class="ic-hint-inline">or drop files here — they go into ${iconEsc(pack.name)}</span>
       </div>
       <p class="ic-hint">PNG, JPEG or WebP — your picture is used exactly as it is, nothing redrawn or recoloured.</p>`
    : `<p class="ic-hint">These are built-in sets. Copy one into a custom set to change it.</p>`;
  // A custom set is named by you, so the name is editable here.  The name is a
  // label: keys and menu buttons point at pack:<set>/<icon>, which does not change.
  const head = iconSetRenaming
    ? `<form class="ic-setname" data-ic-setlabel="${iconEsc(pack.id)}">
         <label for="icSetName">Set name</label>
         <input id="icSetName" type="text" maxlength="40" value="${iconEsc(pack.name)}" autocomplete="off">
         <button type="submit" class="primary">Save</button>
         <button type="button" class="quiet" data-ic-cancelsetname>Cancel</button>
       </form>`
    : `<div>
        <h2>${iconEsc(pack.name)}</h2>
        <p class="muted">${pack.icons.length} icon${pack.icons.length === 1 ? '' : 's'} · ${mine ? 'custom set' : 'built-in set'}</p>
      </div>`;
  // Export sits on every set, including the ones that ship with the app: taking
  // General away with you is a reasonable thing to want.
  const tools = `<div class="ic-bar-set">
      ${mine ? `<button type="button" class="quiet" data-ic-setrename>Rename set…</button>` : ''}
      <a class="quiet" href="/api/icons/export?set=${encodeURIComponent(pack.id)}"
         download="${iconEsc(pack.id)}.zip">Export…</a>
      ${mine ? `<button type="button" class="quiet is-danger" data-ic-setdeleteopen>Delete set…</button>` : ''}
    </div>`;
  // Emptying a set leaves it standing, so losing the whole set is asked for plainly.
  const confirming = iconSetDeleting
    ? `<div class="ic-confirm">
         <p>Delete the set <b>${iconEsc(pack.name)}</b>${pack.icons.length
           ? ` and the ${pack.icons.length} icon${pack.icons.length === 1 ? '' : 's'} in it` : ''}?</p>
         <div class="ic-confirm-buttons">
           <button type="button" class="ic-danger" data-ic-setdelete="${iconEsc(pack.id)}">Delete the set</button>
           <button type="button" class="ic-tiny" data-ic-cancelsetdelete>Keep it</button>
         </div>
       </div>`
    : '';
  const pickedIcon = (pack.icons || []).find(entry => `${pack.id}/${entry.name}` === iconPicked);
  // The bar is above the grid and always there: the set's own actions live in it, so it
  // cannot come and go with the contents of the set.
  const bar = `<div class="ic-bar">
      <div class="ic-bar-picked">${pickedIcon ? iconPickedToolsHTML(pack, pickedIcon) : ''}</div>
      ${tools}
    </div>`;
  return `
    <div class="ic-head">${head}</div>
    ${addRow}
    ${confirming}
    ${iconCopy ? iconChooserHTML() : ''}
    ${bar}
    ${pack.icons.length
      ? `<div class="ic-grid">${pack.icons.map(icon => iconTileHTML(pack, icon)).join('')}</div>`
      : `<p class="ic-empty-set">Nothing in this set yet — use <b>＋ Add icons…</b> above, or copy one in
         from a built-in set. The set stays here until you delete it.</p>`}`;
}

function renderIcons() {
  const root = iconRoot();
  if (!root) return;
  if (!iconLibrary) {
    root.innerHTML = `<div class="ic-shell"><p class="ic-empty">Reading the icon library…</p></div>`;
    return;
  }
  root.innerHTML = `
    <div class="ic-shell">
      <!-- The two ways a set arrives.  They sit in their own row at the top of the page rather
           than at the foot of the list: this is the page's main job, and the lists below grow
           without limit, so anywhere lower would mean scrolling to reach them.  With the
           buttons out of it the sidebar is only ever sets; the form the first one opens
           follows the buttons, where the person who pressed it is looking. -->
      <div class="ic-top">
        <div class="ic-top-actions">
          <button type="button" class="quiet ic-addset" data-ic-newsetopen>＋ New set…</button>
          <label class="quiet ic-addset ic-import" for="icImportZip">⤒ Import a set…</label>
          <input id="icImportZip" type="file" accept=".zip,application/zip" hidden>
        </div>
        ${iconNewSetOpen
          ? `<form class="ic-newset" data-ic-newset>
               <label for="icNewSetName">New set</label>
               <input id="icNewSetName" type="text" maxlength="40" placeholder="e.g. Workshop" autocomplete="off">
               <button type="submit" class="primary">Make the set</button>
               <button type="button" class="quiet" data-ic-cancelnew>Cancel</button>
             </form>`
          : ''}
      </div>
      <div class="ic-split">
        <aside class="ic-side">${iconSetsHTML()}</aside>
        <div class="ic-work">${iconWorkHTML()}</div>
      </div>
    </div>`;
}

// ── clicks ───────────────────────────────────────────────────────────────────

function iconKeyParts(key) {
  const cut = String(key).indexOf('/');
  return {set: String(key).slice(0, cut), name: String(key).slice(cut + 1)};
}

async function onIconsClick(event) {
  const hit = selector => event.target.closest(selector);
  const setButton = hit('[data-ic-set]');
  if (setButton) {
    iconSet = setButton.dataset.icSet;
    // Another set means another set of pictures, so nothing stays picked.
    iconPicked = null;
    // Choosing a set answers nothing and leaves no question hanging.
    iconCopy = null;
    iconSetRenaming = false;
    iconSetDeleting = false;
    iconRenaming = null;
    iconNewSetOpen = false;
    iconStatus('');
    renderIcons();
    return;
  }
  if (hit('[data-ic-newsetopen]')) {
    iconNewSetOpen = true;
    renderIcons();
    document.getElementById('icNewSetName')?.focus();
    return;
  }
  if (hit('[data-ic-cancelnew]')) {
    iconNewSetOpen = false;
    renderIcons();
    return;
  }
  const relabel = hit('[data-ic-relabel]');
  if (relabel) {
    iconRenaming = relabel.dataset.icRelabel;
    renderIcons();
    iconRoot().querySelector('[data-ic-rename] input')?.select();
    return;
  }
  if (hit('[data-ic-cancel]')) {
    iconRenaming = null;
    renderIcons();
    return;
  }
  const remove = hit('[data-ic-delete]');
  if (remove) {
    const {set, name} = iconKeyParts(remove.dataset.icDelete);
    if (remove.dataset.icDelete === iconPicked) iconPicked = null;
    await sendIconChange({op: 'delete', set, name});
    // Deleting the last icon of a set takes the set with it, so the page lands on
    // another one rather than on a message telling you to choose.
    if (!iconSetById(iconSet)) {
      const packs = iconLibrary.packs || [];
      iconSet = (packs.find(pack => !iconIsBuiltIn(pack.id)) || packs[0] || {}).id || null;
      renderIcons();
    }
    return;
  }
  // Copying asks where.  Nothing is guessed, and nothing is created until you say so.
  const copy = hit('[data-ic-copy]');
  if (copy) {
    iconCopy = copy.dataset.icCopy;
    iconStatus('');
    renderIcons();
    return;
  }
  if (hit('[data-ic-copycancel]')) {
    iconCopy = null;
    renderIcons();
    return;
  }
  const copyTo = hit('[data-ic-copyset]');
  if (copyTo) {
    const destination = copyTo.dataset.icCopyset;
    const {set, name} = iconKeyParts(iconCopy);
    iconCopy = null;
    const made = await sendIconChange({op: 'copy', from: set, name, set: destination});
    if (made) iconSet = destination;
    renderIcons();
    return;
  }
  if (hit('[data-ic-setdeleteopen]')) {
    iconSetDeleting = true;
    renderIcons();
    return;
  }
  if (hit('[data-ic-cancelsetdelete]')) {
    iconSetDeleting = false;
    renderIcons();
    return;
  }
  const wipe = hit('[data-ic-setdelete]');
  if (wipe) {
    const going = wipe.dataset.icSetdelete;
    iconSetDeleting = false;
    iconPicked = null;
    const made = await sendIconChange({op: 'set-delete', set: going});
    if (made) {
      const packs = iconLibrary.packs || [];
      iconSet = (packs.find(pack => !iconIsBuiltIn(pack.id)) || packs[0] || {}).id || null;
    }
    renderIcons();
    return;
  }
  if (hit('[data-ic-setrename]')) {
    iconSetRenaming = true;
    renderIcons();
    return;
  }
  if (hit('[data-ic-cancelsetname]')) {
    iconSetRenaming = false;
    renderIcons();
    return;
  }
  // Last, so every action above has already had its turn: a click on a tile that is not on
  // one of its own controls picks that tile, and picks it again to let it go.
  if (hit('.ic-rename')) return;
  const tile = hit('[data-ic-pick]');
  if (tile) {
    const key = tile.dataset.icPick;
    iconPicked = iconPicked === key ? null : key;
    renderIcons();
  }
}

async function onIconsSubmit(event) {
  const rename = event.target.closest('[data-ic-rename]');
  if (rename) {
    event.preventDefault();
    const {set, name} = iconKeyParts(rename.dataset.icRename);
    const label = rename.querySelector('input').value;
    iconRenaming = null;
    iconPicked = null;   // the key it was held as is the old name
    await sendIconChange({op: 'label', set, name, label});
    return;
  }
  const setName = event.target.closest('[data-ic-setlabel]');
  if (setName) {
    event.preventDefault();
    const typed = setName.querySelector('input').value;
    const where = setName.dataset.icSetlabel;
    iconSetRenaming = false;
    await sendIconChange({op: 'set-label', set: where, name: typed});
    renderIcons();
    return;
  }
  const copyNew = event.target.closest('[data-ic-copynew]');
  if (copyNew) {
    event.preventDefault();
    const typed = (copyNew.querySelector('input').value || '').trim();
    if (!typed) {
      iconStatus('Give the set a name and it goes straight in.', 'error');
      return;
    }
    const id = typed.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'my-set';
    const {set, name} = iconKeyParts(iconCopy);
    iconCopy = null;
    const made = await sendIconChange({op: 'copy', from: set, name, set: id, set_name: typed});
    if (made) iconSet = id;
    renderIcons();
    return;
  }
  const newSet = event.target.closest('[data-ic-newset]');
  if (newSet) {
    event.preventDefault();
    await createSet(document.getElementById('icNewSetName').value);
  }
}

function onIconsChange(event) {
  if (event.target.id === 'icFiles') addIconFiles(event.target.files);
}

function wireIcons() {
  const root = iconRoot();
  if (!root || root.dataset.icWired) return;
  root.dataset.icWired = '1';
  root.addEventListener('click', onIconsClick);
  // A tile answers the keyboard like the button it claims to be.  The render replaces the
  // tiles, so focus is put back on the one that was picked.
  root.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const tile = event.target.closest && event.target.closest('[data-ic-pick]');
    if (!tile || (event.target.closest && event.target.closest('.ic-rename'))) return;
    event.preventDefault();
    const key = tile.dataset.icPick;
    iconPicked = iconPicked === key ? null : key;
    renderIcons();
    const again = iconRoot();
    const back = again && [...again.querySelectorAll('[data-ic-pick]')].find(t => t.dataset.icPick === iconPicked);
    if (back) back.focus();
  });
  root.addEventListener('submit', onIconsSubmit);
  root.addEventListener('change', onIconsChange);
  root.addEventListener('change', async event => {
    if (event.target.id !== 'icImportZip') return;
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    // The daemon reads the archive out of the request body and will not take one past its
    // own 25MB limit, so say that here instead of letting the send die as a network error.
    if (file.size > 25 * 1024 * 1024) {
      iconStatus(`That file is ${(file.size / 1048576).toFixed(1)}MB. A set has to be under 25MB.`, 'error');
      return;
    }
    iconStatus(`Reading ${file.name}…`);
    const encoded = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('that file could not be read'));
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.readAsDataURL(file);
    });
    const made = await sendIconChange({op: 'import', data: encoded});
    if (made && made.set) iconSet = made.set;
    renderIcons();
  });
  // Dropping pictures onto the page is the same as choosing them.  The drag is always
  // swallowed: without that the browser opens the dropped file and leaves the editor.
  // The highlight appears only where the drop will actually be taken.
  root.addEventListener('dragover', event => {
    event.preventDefault();
    if (iconSet && !iconIsBuiltIn(iconSet)) root.classList.add('ic-dragging');
  });
  root.addEventListener('dragleave', () => root.classList.remove('ic-dragging'));
  root.addEventListener('drop', event => {
    root.classList.remove('ic-dragging');
    event.preventDefault();
    if (!iconSet || iconIsBuiltIn(iconSet)) {
      iconStatus('This is a built-in set — copy an icon into a custom set to add pictures to it.', 'warn');
      renderIcons();
      return;
    }
    addIconFiles(event.dataTransfer.files);
  });
  document.querySelectorAll('.nav[data-view="icons"]').forEach(button => {
    button.addEventListener('click', () => { wireIcons(); renderIcons(); });
  });
}

async function openIcons() {
  wireIcons();
  renderIcons();
  if (!iconLibrary) await loadIconLibrary();
  renderIcons();
}

openIcons();

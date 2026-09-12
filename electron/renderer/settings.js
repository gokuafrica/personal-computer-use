'use strict';

const providerSel = document.getElementById('provider');
const fields = {
  openai: {
    model: document.getElementById('openai-model')
  },
  anthropic: {
    model: document.getElementById('anthropic-model')
  },
  openai_compat: {
    url: document.getElementById('compat-url'),
    model: document.getElementById('compat-model')
  }
};
const keyStatus = document.getElementById('key-status');
const newKeyInput = document.getElementById('new-api-key');
const clearKeyBtn = document.getElementById('clear-key');
const saveBtn = document.getElementById('save');
const hotkeyInput = document.getElementById('hotkey');
const delayInput = document.getElementById('action-delay');
const glideInput = document.getElementById('pointer-glide');
const overlayToggle = document.getElementById('cursor-overlay');
const toast = document.getElementById('toast');
const errorBox = document.getElementById('error');

const SOURCE_LABELS = {
  bundled: 'the built-in key',
  user: 'your own key',
  none: 'no key'
};

function refreshFieldsets() {
  document.querySelectorAll('fieldset[data-provider]').forEach((fs) => {
    fs.classList.toggle('active', fs.dataset.provider === providerSel.value);
  });
}

function describeKey(summary) {
  const parts = [];
  if (summary.keyConfigured) {
    parts.push(`A key is saved: ${SOURCE_LABELS[summary.keySource] || 'a key'}`);
    if (summary.keySource === 'bundled' && summary.keyVersion) {
      parts.push(`(version ${summary.keyVersion})`);
    }
  } else {
    parts.push('No API key is saved yet.');
  }
  return parts.join(' ');
}

// Key store unreachable (backend not connected): disable key entry entirely
// (keys are stored by the backend; existing stored keys keep working).
function applyEncryptionState(summary) {
  const encryptionDown = summary.keyEncryptionAvailable === false;
  newKeyInput.disabled = encryptionDown;
  if (summary.keySource !== 'user') clearKeyBtn.disabled = encryptionDown;
  if (encryptionDown) {
    newKeyInput.placeholder = 'Key storage unavailable';
    keyStatus.textContent = describeKey(summary) +
      ' The key store is not connected right now, so new keys cannot be saved. Your existing key (if any) still works.';
  } else {
    newKeyInput.placeholder = 'Paste a new key here';
  }
}

function fill(summary) {
  providerSel.value = summary.provider || 'openai';
  fields.openai.model.value = summary.openai?.model || '';
  fields.anthropic.model.value = summary.anthropic?.model || '';
  fields.openai_compat.url.value = summary.openai_compat?.base_url || '';
  fields.openai_compat.model.value = summary.openai_compat?.model || '';
  hotkeyInput.value = summary.hotkey || 'Control+Alt+K';
  delayInput.value = (typeof summary.action_delay_s === 'number' && isFinite(summary.action_delay_s))
    ? summary.action_delay_s
    : 0.4;
  glideInput.value = (typeof summary.pointer_glide_s === 'number' && isFinite(summary.pointer_glide_s))
    ? summary.pointer_glide_s
    : 0.45;
  overlayToggle.checked = summary.cursor_overlay !== false;
  keyStatus.textContent = describeKey(summary);
  clearKeyBtn.classList.toggle('hidden', !(summary.keyConfigured && summary.keySource === 'user'));
  newKeyInput.value = '';
  applyEncryptionState(summary);
  refreshFieldsets();
}

function collect() {
  return {
    provider: providerSel.value,
    openai: {
      model: fields.openai.model.value.trim()
    },
    anthropic: {
      model: fields.anthropic.model.value.trim()
    },
    openai_compat: {
      base_url: fields.openai_compat.url.value.trim(),
      model: fields.openai_compat.model.value.trim()
    },
    hotkey: hotkeyInput.value.trim() || 'Control+Alt+K',
    action_delay_s: (() => {
      const n = Number(delayInput.value);
      if (!isFinite(n)) return 0.4;
      return Math.min(3, Math.max(0, n));
    })(),
    pointer_glide_s: (() => {
      const n = Number(glideInput.value);
      if (!isFinite(n)) return 0.45;
      return Math.min(1, Math.max(0, n));
    })(),
    cursor_overlay: overlayToggle.checked
  };
}

providerSel.addEventListener('change', refreshFieldsets);

clearKeyBtn.addEventListener('click', async () => {
  if (!window.confirm('Remove your own key and go back to the built-in key?')) return;
  try {
    const summary = await window.pcu.clearApiKey();
    fill(summary);
    toast.textContent = 'Saved';
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 2500);
  } catch (err) {
    errorBox.textContent = 'Could not remove the key: ' + (err?.message || err);
    errorBox.classList.remove('hidden');
  }
});

document.getElementById('save').addEventListener('click', async () => {
  errorBox.classList.add('hidden');
  const cfg = collect();
  const newKey = newKeyInput.value.trim();
  const summary = await window.pcu.getConfigSummary();
  if (newKey && summary.keyEncryptionAvailable === false) {
    errorBox.textContent = 'The key store is not connected right now, so the key was not saved. ' +
      'Your existing key (if any) still works. Try again in a moment.';
    errorBox.classList.remove('hidden');
    return;
  }
  try {
    const saved = await window.pcu.saveConfig(cfg);
    if (newKey) {
      const after = await window.pcu.setApiKey(newKey);
      newKeyInput.value = '';
      keyStatus.textContent = describeKey({ ...after, keyConfigured: true, keySource: 'user' });
      applyEncryptionState(after);
    } else {
      keyStatus.textContent = describeKey(saved);
      applyEncryptionState(saved);
    }
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 2500);
  } catch (err) {
    errorBox.textContent = 'Save failed: ' + (err?.message || err);
    errorBox.classList.remove('hidden');
  }
});

window.pcu.getConfigSummary().then(fill);

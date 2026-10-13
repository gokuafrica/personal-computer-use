'use strict';

const providerSel = document.getElementById('provider');
const fields = {
  codex: { model: document.getElementById('codex-model'), effort: document.getElementById('codex-effort') },
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
const confirmToggle = document.getElementById('confirm-actions');
const toast = document.getElementById('toast');
const errorBox = document.getElementById('error');

const SOURCE_LABELS = {
  bundled: 'the built-in key',
  user: 'your own key',
  none: 'no key'
};

function refreshFieldsets() {
  document.getElementById('api-key-fieldset').classList.toggle('hidden', providerSel.value === 'codex');
  document.querySelectorAll('fieldset[data-provider]').forEach((fs) => {
    fs.classList.toggle('active', fs.dataset.provider === providerSel.value);
    fs.classList.toggle('hidden', fs.dataset.provider !== providerSel.value);
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
  providerSel.value = summary.provider || 'codex';
  fields.codex.model.value = summary.codex?.model || 'gpt-6-luna';
  fields.codex.effort.value = summary.codex?.reasoning_effort || 'high';
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
  confirmToggle.checked = summary.confirm_actions === true;
  keyStatus.textContent = describeKey(summary);
  clearKeyBtn.classList.toggle('hidden', !(summary.keyConfigured && summary.keySource === 'user'));
  newKeyInput.value = '';
  applyEncryptionState(summary);
  refreshFieldsets();
}

function collect() {
  return {
    provider: providerSel.value,
    codex: { model: fields.codex.model.value.trim() || 'gpt-6-luna', reasoning_effort: fields.codex.effort.value },
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
    cursor_overlay: overlayToggle.checked,
    confirm_actions: confirmToggle.checked
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
  const newKey = providerSel.value === 'codex' ? '' : newKeyInput.value.trim();
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

async function checkCodexSignIn(login = false) {
  const status = document.getElementById('codex-status');
  const button = document.getElementById('codex-login');
  button.disabled = true;
  status.textContent = login ? 'Complete sign-in in your browser…' : 'Checking sign-in…';
  try {
    const result = await (login ? window.pcu.codexLogin() : window.pcu.codexStatus());
    renderCodexState(result);
  } catch (error) { status.textContent = error.message; }
  finally { if (!codexAuthPending) button.disabled = false; }
}
let codexAuthPending = false;
function renderCodexState(result) {
  codexAuthPending = result.pending === true;
  document.getElementById('codex-status').textContent = result.signedIn
    ? `Signed in. You're ready to go! Press ${hotkeyInput.value || 'Control+Alt+K'} to start a task.`
    : result.message;
  document.getElementById('codex-login').disabled = codexAuthPending;
  document.getElementById('codex-login').textContent = codexAuthPending ? 'Waiting for sign-in…'
    : result.signedIn ? 'Use another ChatGPT account' : 'Try again';
  document.getElementById('codex-refresh').disabled = codexAuthPending;
}
window.pcu.onCodexAuthState(renderCodexState);
document.getElementById('codex-login').addEventListener('click', () => checkCodexSignIn(true));
document.getElementById('codex-refresh').addEventListener('click', () => checkCodexSignIn());
checkCodexSignIn();

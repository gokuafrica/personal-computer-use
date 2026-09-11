'use strict';

const providerSel = document.getElementById('provider');
const fields = {
  openai: {
    key: document.getElementById('openai-key'),
    model: document.getElementById('openai-model')
  },
  anthropic: {
    key: document.getElementById('anthropic-key'),
    model: document.getElementById('anthropic-model')
  },
  openai_compat: {
    url: document.getElementById('compat-url'),
    key: document.getElementById('compat-key'),
    model: document.getElementById('compat-model')
  }
};
const hotkeyInput = document.getElementById('hotkey');
const delayInput = document.getElementById('action-delay');
const toast = document.getElementById('toast');
const errorBox = document.getElementById('error');

function refreshFieldsets() {
  document.querySelectorAll('fieldset[data-provider]').forEach((fs) => {
    fs.classList.toggle('active', fs.dataset.provider === providerSel.value);
  });
}

function fill(cfg) {
  providerSel.value = cfg.provider || 'openai';
  fields.openai.key.value = cfg.openai?.api_key || '';
  fields.openai.model.value = cfg.openai?.model || '';
  fields.anthropic.key.value = cfg.anthropic?.api_key || '';
  fields.anthropic.model.value = cfg.anthropic?.model || '';
  fields.openai_compat.url.value = cfg.openai_compat?.base_url || '';
  fields.openai_compat.key.value = cfg.openai_compat?.api_key || '';
  fields.openai_compat.model.value = cfg.openai_compat?.model || '';
  hotkeyInput.value = cfg.hotkey || 'Control+Alt+K';
  delayInput.value = (typeof cfg.action_delay_s === 'number' && isFinite(cfg.action_delay_s))
    ? cfg.action_delay_s
    : 0.4;
  refreshFieldsets();
}

function collect() {
  return {
    provider: providerSel.value,
    openai: {
      api_key: fields.openai.key.value,
      model: fields.openai.model.value.trim()
    },
    anthropic: {
      api_key: fields.anthropic.key.value,
      model: fields.anthropic.model.value.trim()
    },
    openai_compat: {
      base_url: fields.openai_compat.url.value.trim(),
      api_key: fields.openai_compat.key.value,
      model: fields.openai_compat.model.value.trim()
    },
    hotkey: hotkeyInput.value.trim() || 'Control+Alt+K',
    action_delay_s: (() => {
      const n = Number(delayInput.value);
      if (!isFinite(n)) return 0.4;
      return Math.min(3, Math.max(0, n));
    })()
  };
}

document.querySelectorAll('.show-toggle').forEach((btn) => {
  btn.addEventListener('click', () => {
    const target = document.getElementById(btn.dataset.target);
    const show = target.type === 'password';
    target.type = show ? 'text' : 'password';
    btn.textContent = show ? 'Hide' : 'Show';
  });
});

providerSel.addEventListener('change', refreshFieldsets);

document.getElementById('save').addEventListener('click', async () => {
  errorBox.classList.add('hidden');
  const cfg = collect();
  try {
    JSON.stringify(cfg);
  } catch {
    errorBox.textContent = 'Settings could not be serialized as JSON.';
    errorBox.classList.remove('hidden');
    return;
  }
  try {
    await window.pcu.saveConfig(cfg);
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 2500);
  } catch (err) {
    errorBox.textContent = 'Save failed: ' + (err?.message || err);
    errorBox.classList.remove('hidden');
  }
});

window.pcu.getConfig().then(fill);

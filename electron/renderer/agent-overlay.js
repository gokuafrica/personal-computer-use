'use strict';

const body = document.body;
const cursorEl = document.getElementById('cursor');
const bannerText = document.getElementById('banner-text');
const fxLayer = document.getElementById('fx');

const BANNER_RUNNING = 'PCU is in control';

let cursorX = -10000;
let cursorY = -10000;

cursorEl.style.transform = 'translate(-10000px, -10000px)';

function setBanner(text) {
  bannerText.textContent = text;
}

function setActive(on) {
  body.classList.toggle('active', on);
  if (!on) {
    body.classList.remove('awaiting');
    setBanner(BANNER_RUNNING);
    clearFx();
  }
}

function clearFx() {
  while (fxLayer.firstChild) fxLayer.firstChild.remove();
}

function expire(el, ttlMs) {
  window.setTimeout(() => {
    el.classList.add('fade-out');
    window.setTimeout(() => el.remove(), 350);
  }, ttlMs);
}

function placeAt(el, x, y) {
  el.style.left = Math.round(x) + 'px';
  el.style.top = Math.round(y) + 'px';
}

function spawnRipple(x, y) {
  const el = document.createElement('div');
  el.className = 'fx-el ripple';
  placeAt(el, x, y);
  fxLayer.appendChild(el);
  el.addEventListener('animationend', () => el.remove());
  window.setTimeout(() => el.remove(), 1000);
}

function spawnTypePill() {
  const el = document.createElement('div');
  el.className = 'fx-el type-pill';
  el.appendChild(document.createTextNode('typing'));
  const dots = document.createElement('span');
  dots.className = 'dots';
  for (let i = 0; i < 3; i += 1) dots.appendChild(document.createElement('i'));
  el.appendChild(dots);
  placeAt(el, cursorX + 24, cursorY + 34);
  fxLayer.appendChild(el);
  expire(el, 1400);
}

function fmtCombo(detail) {
  const raw = detail.replace(/^press\s+/i, '').trim();
  return raw
    .split('+')
    .filter((part) => part.trim())
    .map((part) => {
      const t = part.trim();
      return t.length === 1 ? t.toUpperCase() : t.charAt(0).toUpperCase() + t.slice(1);
    })
    .join(' + ');
}

function spawnKeyChip(detail) {
  const el = document.createElement('div');
  el.className = 'fx-el key-chip';
  el.textContent = fmtCombo(detail);
  placeAt(el, cursorX + 24, cursorY + 34);
  fxLayer.appendChild(el);
  expire(el, 1400);
}

function spawnScrollFx(detail) {
  const el = document.createElement('div');
  el.className = 'fx-el scroll-fx';
  const up = document.createElement('span');
  up.textContent = '\u25B2';
  const down = document.createElement('span');
  down.textContent = '\u25BC';
  el.appendChild(up);
  el.appendChild(down);
  placeAt(el, cursorX + 24, cursorY + 34);
  fxLayer.appendChild(el);
  expire(el, 1100);
}

function handleAction(msg) {
  const kind = String(msg.kind || '');
  if (kind === 'click' || kind === 'double_click' || kind === 'right_click' || kind === 'click_element') {
    const x = Number.isFinite(msg.x) ? msg.x : cursorX;
    const y = Number.isFinite(msg.y) ? msg.y : cursorY;
    spawnRipple(x, y);
    body.classList.add('pressed');
    window.setTimeout(() => body.classList.remove('pressed'), 180);
  } else if (kind === 'type') {
    spawnTypePill();
  } else if (kind === 'key') {
    spawnKeyChip(String(msg.detail || ''));
  } else if (kind === 'scroll') {
    spawnScrollFx(String(msg.detail || ''));
  }
}

window.pcu.onOverlayCursor((pt) => {
  if (!pt || !Number.isFinite(pt.x) || !Number.isFinite(pt.y)) return;
  cursorX = pt.x;
  cursorY = pt.y;
  cursorEl.style.transform = 'translate(' + pt.x + 'px, ' + pt.y + 'px)';
});

window.pcu.onBackendMessage((msg) => {
  if (!msg || typeof msg.type !== 'string') return;
  switch (msg.type) {
    case 'status':
      if (msg.state === 'running') {
        body.classList.remove('awaiting');
        setBanner(BANNER_RUNNING);
        setActive(true);
      } else if (msg.state === 'awaiting_confirmation') {
        body.classList.add('awaiting');
        setBanner('Needs your approval');
        setActive(true);
      } else {
        setActive(false);
      }
      break;
    case 'action':
      handleAction(msg);
      break;
    case 'task_done':
      setActive(false);
      break;
  }
});

window.pcu.onBackendConnection((info) => {
  if (!info || !info.connected) setActive(false);
});

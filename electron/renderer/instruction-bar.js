'use strict';

const input = document.getElementById('instruction');
const hint = document.getElementById('hint');
const running = document.getElementById('running');

const HINT_TEXT = 'Enter to run \u00b7 Esc to close';
let isRunning = false;
let connected = true;

function setRunning(state) {
  isRunning = state;
  input.disabled = state;
  running.classList.toggle('hidden', !state);
  hint.classList.toggle('hidden', state);
  if (!state) input.focus();
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    window.pcu.hideBar();
  } else if (e.key === 'Enter' && !isRunning) {
    const text = input.value.trim();
    if (!text) return;
    if (!connected) {
      hint.textContent = 'Backend not ready \u2014 try again in a moment';
      return;
    }
    setRunning(true);
    window.pcu.submitInstruction(text).then((result) => {
      if (!result?.ok) throw new Error('Backend not ready');
      input.value = '';
      window.pcu.hideBar();
    }).catch(() => {
      setRunning(false);
      hint.textContent = 'Could not start task — your instruction is kept. Try again.';
    });
  }
});

// While a task runs, clicking the bar jumps to the status card.
document.getElementById('bar').addEventListener('click', () => {
  if (isRunning) window.pcu.hideBar();
});

window.pcu.onBackendMessage((msg) => {
  if (msg.type === 'status') {
    setRunning(msg.state === 'running' || msg.state === 'awaiting_confirmation');
  } else if (msg.type === 'task_done') {
    setRunning(false);
  }
});

window.pcu.onBackendConnection((info) => {
  connected = !!info.connected;
  if (!connected) setRunning(false);
  if (connected) hint.textContent = HINT_TEXT;
});

window.pcu.onBarShown(() => {
  if (!isRunning) {
    input.focus();
    input.select();
  }
});

input.focus();

'use strict';

const pill = document.getElementById('state-pill');
const action = document.getElementById('action');
const log = document.getElementById('log');
const confirmBox = document.getElementById('confirm-box');
const confirmReason = document.getElementById('confirm-reason');
const confirmDetail = document.getElementById('confirm-detail');

const STATE_LABELS = {
  idle: 'Idle',
  running: 'Running',
  awaiting_confirmation: 'Needs approval',
  error: 'Error'
};

let pendingConfirmId = null;

function setState(state) {
  pill.className = 'pill ' + state;
  pill.textContent = STATE_LABELS[state] || state;
}

function addLogLine(text) {
  const div = document.createElement('div');
  div.textContent = text;
  log.appendChild(div);
  while (log.childElementCount > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function showConfirm(id, reason, detail) {
  pendingConfirmId = id;
  confirmReason.textContent = reason || 'This action needs your approval.';
  confirmDetail.textContent = detail || '';
  confirmBox.classList.remove('hidden');
}

function hideConfirm() {
  pendingConfirmId = null;
  confirmBox.classList.add('hidden');
}

function describeAction(kind, detail) {
  const k = (kind || 'action').toLowerCase();
  if (k === 'click' || k === 'double_click' || k === 'right_click') return `Clicking ${detail}`;
  if (k === 'type' || k === 'write') return `Typing ${detail}`;
  if (k === 'key' || k === 'press') return `Pressing ${detail}`;
  if (k === 'scroll') return `Scrolling ${detail}`;
  if (k === 'move') return `Moving mouse to ${detail}`;
  if (k === 'wait') return `Waiting ${detail}`;
  if (k === 'screenshot') return 'Taking a screenshot';
  return `${kind}: ${detail}`;
}

document.getElementById('stop').addEventListener('click', () => window.pcu.stopTask());
document.getElementById('approve').addEventListener('click', () => {
  if (pendingConfirmId) window.pcu.confirm(pendingConfirmId, true);
  hideConfirm();
});
document.getElementById('reject').addEventListener('click', () => {
  if (pendingConfirmId) window.pcu.confirm(pendingConfirmId, false);
  hideConfirm();
});

window.pcu.onBackendConnection((info) => {
  if (!info.connected) {
    setState('error');
    pill.className = 'pill disconnected';
    pill.textContent = 'Backend not running — trying to reconnect…';
  } else {
    setState('idle');
  }
});

window.pcu.onBackendMessage((msg) => {
  switch (msg.type) {
    case 'status':
      setState(msg.state);
      if (msg.state !== 'awaiting_confirmation') hideConfirm();
      if (msg.message) action.textContent = msg.message;
      break;
    case 'log':
      if (msg.line) addLogLine(msg.line);
      break;
    case 'action':
      action.textContent = describeAction(msg.kind, msg.detail);
      addLogLine(describeAction(msg.kind, msg.detail));
      break;
    case 'need_confirmation':
      setState('awaiting_confirmation');
      showConfirm(msg.id, msg.reason, msg.detail);
      break;
    case 'task_done': {
      hideConfirm();
      setState('idle');
      const verdict = msg.success ? 'Done' : 'Failed';
      action.textContent = `${verdict}: ${msg.summary || ''}`;
      addLogLine(`${verdict}: ${msg.summary || ''}`);
      break;
    }
  }
});

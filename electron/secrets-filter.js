'use strict';

// Redacts configured secrets and common credential patterns from text that is
// forwarded to consoles, diagnostics, or renderers. Never accepts the secret
// value back from callers for display.
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const GENERIC_PATTERNS = [
  [/Bearer\s+[A-Za-z0-9._~+/=-]{8,}/gi, 'Bearer [redacted]'],
  [/sk-[A-Za-z0-9_-]{8,}/g, '[redacted-key]'],
  [/api[-_]?key["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{8,}/gi, 'api_key=[redacted]'],
  [/authorization["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{8,}/gi, 'authorization=[redacted]'],
  [/token["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{16,}/gi, 'token=[redacted]']
];

// Derived forms shorter than this are not registered (absurd false positives).
const DERIVED_MIN_LEN = 16;

// Encoded forms of a secret that must also be redacted verbatim: base64 of
// the secret, base64 of its UTF-16LE bytes, and the JSON-escape form
// (JSON.stringify's escaped inner string). Short derived forms are skipped.
function derivedForms(secret) {
  const forms = [
    Buffer.from(secret, 'utf8').toString('base64'),
    Buffer.from(secret, 'utf16le').toString('base64')
  ];
  // JSON-escape form: JSON.stringify's escaped inner string, with non-ASCII
  // additionally escaped to \uXXXX to mirror json.dumps(ensure_ascii=True).
  const jsonEscaped = JSON.stringify(secret)
    .replace(/[\u0080-\uffff]/g, (c) =>
      `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
    .slice(1, -1);
  if (jsonEscaped !== secret) forms.push(jsonEscaped);
  return [...new Set(forms)].filter((f) => f.length >= DERIVED_MIN_LEN);
}

function createSecretsFilter(secrets) {
  const known = new Set(
    (Array.isArray(secrets) ? secrets : [])
      .filter((s) => typeof s === 'string' && s.length >= 4)
  );
  for (const secret of [...known]) {
    for (const form of derivedForms(secret)) known.add(form);
  }
  return function redact(text) {
    let out = String(text == null ? '' : text);
    for (const secret of known) {
      out = out.split(secret).join('[redacted]');
    }
    for (const [pattern, replacement] of GENERIC_PATTERNS) {
      out = out.replace(pattern, replacement);
    }
    return out;
  };
}

module.exports = { createSecretsFilter, escapeRegExp };

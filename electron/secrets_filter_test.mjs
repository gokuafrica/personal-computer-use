import assert from 'node:assert';
import { createSecretsFilter } from './secrets-filter.js';

const FAKE = 'sk-test-fake-000000000000';

const redact = createSecretsFilter([FAKE]);

// configured key is removed verbatim, everywhere
assert.strictEqual(redact(`prefix ${FAKE} suffix`), 'prefix [redacted] suffix');
assert.strictEqual(redact(`${FAKE}${FAKE}`), '[redacted][redacted]');

// bearer tokens
assert.ok(!redact('Authorization: Bearer abcdefgh123456').includes('abcdefgh123456'));
assert.ok(redact('Authorization: Bearer abcdefgh123456').includes('Bearer [redacted]'));

// openai-style keys
assert.ok(!redact('request used sk-abcdef0123456789 signature').includes('sk-abcdef0123456789'));

// generic key/value leaks
assert.ok(!redact('api_key = "sk-abcdef0123456789"').includes('sk-abcdef0123456789'));
assert.ok(!redact('token: 1234567890abcdefghij').includes('1234567890abcdefghij'));

// short generic strings are left alone
assert.strictEqual(redact('token: abc'), 'token: abc');

// benign text is unchanged
assert.strictEqual(redact('open notepad and type hello'), 'open notepad and type hello');

// multi-line payload with the known key
const lines = redact(['line1', FAKE, 'Bearer abcdefgh123456'].join('\n'));
assert.ok(!lines.includes(FAKE) && !lines.includes('abcdefgh123456'));

// base64 of the configured key (utf8 and utf16le) is redacted
const b64 = Buffer.from(FAKE, 'utf8').toString('base64');
const b64le = Buffer.from(FAKE, 'utf16le').toString('base64');
assert.ok(!redact(`encoded: ${b64}`).includes(b64), 'utf8 base64 form');
assert.ok(!redact(`encoded-le: ${b64le}`).includes(b64le), 'utf16le base64 form');

// JSON-escaped form of a key that needs escaping is redacted
const NONASCII = 'sk-test-f\u00e4ke-000000000000';
const redact2 = createSecretsFilter([NONASCII]);
// ensure_ascii-style escape of NONASCII, written out literally
const jsonEscaped = 'sk-test-f\\u00e4ke-000000000000';
assert.notStrictEqual(jsonEscaped, NONASCII);
assert.ok(!redact2(`"apiKey": "${jsonEscaped}"`).includes(jsonEscaped),
  'json-escaped form');
assert.ok(!redact2(`"apiKey": "${jsonEscaped}"`).includes(NONASCII));

// json-escaped form of a plain ASCII key differs only by quoting, so the
// exact key itself (inside quotes) must still be redacted
assert.ok(!redact(`{"apiKey": "${FAKE}"}`).includes(FAKE), 'exact inside quotes');

console.log('SECRETS-FILTER PASS');

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createSubscriptionSetup } from './subscription-setup.js';

const signedOut = { available: true, signedIn: false, message: 'Sign in' };
const signedIn = { available: true, signedIn: true, message: 'Signed in' };

test('first launch opens setup and begins browser login without a button click', async () => {
  const calls = [];
  const flow = createSubscriptionSetup({ check: async () => signedOut,
    login: async () => { calls.push('browser'); return signedIn; },
    onState: state => { if (state.pending) calls.push('waiting'); } });
  const result = await flow.ensure(() => calls.push('setup'));
  assert.deepEqual(calls, ['setup', 'waiting', 'browser']);
  assert.equal(result.signedIn, true);
  assert.equal(result.pending, false);
});

test('existing ChatGPT sign-in skips both browser and setup', async () => {
  const flow = createSubscriptionSetup({ check: async () => signedIn,
    login: async () => { assert.fail('Login should not open'); } });
  assert.equal((await flow.ensure(() => assert.fail('Setup should not open'))).signedIn, true);
});

test('repeated login requests and status refresh share one active login', async () => {
  let finish, calls = 0;
  const flow = createSubscriptionSetup({ check: async () => signedOut,
    login: () => { calls++; return new Promise(resolve => { finish = resolve; }); } });
  const first = flow.signIn();
  assert.equal(first, flow.signIn());
  assert.equal((await flow.status()).pending, true);
  assert.equal(calls, 1);
  finish(signedIn);
  assert.equal((await first).signedIn, true);
});

test('cancelled login stops waiting and supports manual retry', async () => {
  let attempts = 0;
  const flow = createSubscriptionSetup({ check: async () => signedOut,
    login: async () => ++attempts === 1 ? signedOut : signedIn });
  const cancelled = await flow.ensure(() => {});
  assert.equal(cancelled.pending, false);
  assert.match(cancelled.message, /Try again/);
  assert.equal((await flow.signIn()).signedIn, true);
  assert.equal(attempts, 2);
});

test('unavailable runtime shows setup error without looping into login', async () => {
  let opened = 0;
  const flow = createSubscriptionSetup({ check: async () => { throw Error('private path'); },
    login: () => assert.fail('No runtime'), });
  const result = await flow.ensure(() => opened++);
  assert.equal(opened, 1);
  assert.equal(result.available, false);
  assert.equal(result.message.includes('private path'), false);
});

test('an in-flight status check cannot overwrite a pending browser login', async () => {
  let finishCheck, finishLogin;
  const flow = createSubscriptionSetup({
    check: () => new Promise(resolve => { finishCheck = resolve; }),
    login: () => new Promise(resolve => { finishLogin = resolve; }) });
  const checking = flow.status();
  await Promise.resolve();
  const signing = flow.signIn();
  finishCheck(signedOut);
  await checking;
  await Promise.resolve();
  assert.equal((await flow.status()).pending, true);
  finishLogin(signedIn);
  await signing;
});

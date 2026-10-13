'use strict';

// One browser sign-in at a time, shared by startup and Settings. Only public
// status reaches the renderer; Codex owns the credentials and browser callback.
function createSubscriptionSetup({ check, login, onState = () => {} }) {
  let state = { available: true, signedIn: false, pending: false, message: 'Checking ChatGPT sign-in…' };
  let checkJob = null;
  let loginJob = null;
  const publish = (value) => {
    state = { ...value };
    onState({ ...state });
    return { ...state };
  };
  const failed = () => publish({ available: false, signedIn: false, pending: false,
    message: 'Could not start ChatGPT sign-in. Try again, or reinstall the app if this continues.' });

  function status() {
    if (loginJob) return Promise.resolve({ ...state });
    if (!checkJob) {
      checkJob = Promise.resolve().then(check)
        .then(result => publish({ ...result, pending: false }))
        .catch(failed).finally(() => { checkJob = null; });
    }
    return checkJob;
  }

  function signIn() {
    if (!loginJob) {
      loginJob = (async () => {
        if (checkJob) await checkJob;
        publish({ available: true, signedIn: false, pending: true,
          message: 'Complete ChatGPT sign-in in your browser. This window updates automatically.' });
        try {
          const result = await login();
          return publish({ ...result, pending: false,
            message: result.signedIn ? result.message :
              'Sign-in was not completed. Click Try again to reopen ChatGPT sign-in.' });
        } catch { return failed(); }
      })().finally(() => { loginJob = null; });
    }
    return loginJob;
  }

  async function ensure(onRequired) {
    const result = await status();
    if (result.signedIn) return result;
    onRequired();
    if (!result.available) return result;
    return signIn();
  }

  return { status, signIn, ensure };
}

module.exports = { createSubscriptionSetup };

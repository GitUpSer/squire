'use strict';
const $ = id => document.getElementById(id);
let csrf = '', emailChallenge = '', phoneChallenge = sessionStorage.getItem('squire-phone-challenge') || '', account = null;
const params = new URLSearchParams(location.hash.slice(1));
let invitation = params.get('invite') || sessionStorage.getItem('squire-invitation') || '';
if (invitation) sessionStorage.setItem('squire-invitation', invitation);
if (location.hash.includes('invite=')) history.replaceState(null, '', location.pathname);
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const zones = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [zone, 'UTC'];
for (const item of new Set([zone, ...zones])) { const option = new Option(item.replaceAll('_', ' '), item); $('timezone').add(option); }
$('timezone').value = zone;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch('/api/v1' + path, { method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, ...(data === undefined ? {} : {body: JSON.stringify(data)}) });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(typeof value.detail === 'string' ? value.detail : 'Please check your information and try again.'); error.status = response.status; throw error; }
  return value;
}
function action(id, handler) {
  const element = $(id); element.addEventListener(element.tagName === 'FORM' ? 'submit' : 'click', async event => {
    event.preventDefault(); const buttons = element.tagName === 'FORM' ? [...element.querySelectorAll('button')] : [element];
    buttons.forEach(button => button.disabled = true); notice('');
    try { await handler(); } catch (error) { notice(error.message, true); } finally { buttons.forEach(button => button.disabled = false); }
  });
}
function render() {
  $('login').hidden = !!account; $('setup').hidden = !account || !!account.trial_state;
  $('dashboard').hidden = !account?.trial_state; $('logout-button').hidden = !account;
  if (!account) return;
  csrf = account.csrf_token; $('name').value ||= account.display_name;
  const pending = account.messaging?.phone_challenge;
  if (pending && !pending.verified && pending.expires_at * 1000 > Date.now()) { phoneChallenge = pending.challenge_id; sessionStorage.setItem('squire-phone-challenge',phoneChallenge); }
  $('fallback').hidden = !pending?.fallback_nonce;
  if (pending?.fallback_nonce) { $('fallback-command').textContent = 'SQUIRE-CONNECT ' + pending.fallback_nonce; const number = (account.service_whatsapp_number || '').replace(/[^0-9]/g,''); $('fallback-link').hidden = !number; if(number) $('fallback-link').href='https://wa.me/' + number + '?text=' + encodeURIComponent($('fallback-command').textContent); }
  if (account.admission_state === 'reserved') { $('profile-form').hidden = true; $('phone-form').hidden = !!phoneChallenge; $('phone-code-form').hidden = !phoneChallenge; }
  if (!account.trial_state) return;
  const state = account.trial_state;
  $('greeting').textContent = account.display_name ? `${account.display_name}’s Squire` : 'Your Squire';
  $('state').textContent = state.replaceAll('_', ' ');
  const descriptions = {setup_paused: 'Setup was paused after 24 hours without a first message. Resume within seven days of verification to keep your place.', setup_resuming: 'Your Squire is resuming setup.', extending: 'Your approved extension is being applied.', ready_unstarted: account.runtime_ready ? 'Your Squire is ready. Look for its introduction on WhatsApp, then send your first message to start your trial.' : 'Your number is verified. We’re preparing your private Squire; its introduction will arrive on WhatsApp when it’s ready.', activating: 'Your first message is safely queued while we activate your trial.', active: 'Your Squire is available on WhatsApp.', imported_active: 'Your Squire is available on WhatsApp.', paused: 'Your Squire is paused. It will not process new conversations.', expired: 'Your trial has ended. Request more time if you’d like to keep exploring.', deleted: 'Your account is disabled. Removal of its agent storage is queued.'};
  $('status-text').textContent = descriptions[state] || 'Checking your Squire’s status.';
  $('trial-details').textContent = state === 'imported_active' ? 'Your existing Squire continues without a trial expiry. Approved groups use their own capped model allowance.' : account.expires_at ? `${account.turns_used} of ${account.daily_turn_limit} turns used in the current trial day. Trial ends ${new Date(account.expires_at).toLocaleString()}.` : 'Your 72-hour trial has not started yet.';
  $('group-button').disabled = !['active', 'ready_unstarted', 'imported_active'].includes(state);
  $('resume-button').hidden = state !== 'setup_paused';
  $('pause-button').hidden = ['paused', 'expired', 'deleted', 'setup_paused', 'setup_resuming', 'extending'].includes(state);
  $('groups').replaceChildren();
  for (const group of account.messaging?.groups || []) { const li = document.createElement('li'); li.textContent = `Approved group · ${group.active ? (group.ready ? 'ready' : 'preparing') : 'paused'} · ${group.mode || 'addressed'}`; if (group.active) { const remove = document.createElement('button'); remove.className = 'quiet'; remove.textContent = 'Remove approval'; remove.addEventListener('click', async () => { try { await api('/groups/' + encodeURIComponent(group.conversation_id), {active:false}); await refresh(); } catch(error) { notice(error.message, true); } }); li.append(' ', remove); } $('groups').append(li); }
}
async function loadApprovals() {
  if (!account?.tenant_id || document.activeElement?.tagName === 'TEXTAREA') return;
  const response = await api('/approvals'); $('approvals').replaceChildren();
  $('approval-panel').hidden = !response.approvals?.length;
  for (const approval of response.approvals || []) {
    const card = document.createElement('div'), summary = document.createElement('p');
    summary.textContent = approval.summary; card.append(summary);
    const send = async (decision, result) => { try { await api('/approvals/' + encodeURIComponent(approval.approval_id), {decision, ...(result === undefined ? {} : {result})}); await loadApprovals(); } catch(error) { notice(error.message, true); } };
    if (['pending', 'requested'].includes(approval.state)) {
      for (const decision of ['approve', 'deny']) { const button = document.createElement('button'); button.className = 'secondary'; button.textContent = decision === 'approve' ? 'Approve private action' : 'Deny'; button.onclick = () => send(decision); card.append(button); }
    } else if (approval.result != null && !['released', 'denied'].includes(approval.state)) {
      const selection = document.createElement('textarea'); selection.value = approval.result; selection.maxLength = 10000; selection.setAttribute('aria-label', 'Selected result to share with the group'); card.append(selection);
      const button = document.createElement('button'); button.textContent = 'Share selected result with group'; button.onclick = () => send('release', selection.value); card.append(button);
    } else { const state = document.createElement('p'); state.textContent = approval.state; card.append(state); }
    $('approvals').append(card);
  }
}
async function refresh() { try { account = await api('/me'); render(); if(account.trial_state) await loadApprovals(); } catch(error) { if(error.status === 401) { account = null; render(); } else notice(error.message, true); } }
action('login-form', async () => { const result = await api('/auth/start', {email: $('email').value, invitation}); emailChallenge = result.challenge_id; $('code-form').hidden = false; notice(result.status); $('email-code').focus(); });
action('code-form', async () => { const result = await api('/auth/verify', {challenge_id: emailChallenge, code: $('email-code').value}); csrf = result.csrf_token; await refresh(); });
action('profile-form', async () => { await api('/trial', {invitation, display_name: $('name').value, timezone: $('timezone').value, consent_version: account.consent_version}); await refresh(); });
action('phone-form', async () => { const result = await api('/phone/challenge', {operation_id: crypto.randomUUID(), phone: $('phone').value}); phoneChallenge = result.challenge_id; sessionStorage.setItem('squire-phone-challenge', phoneChallenge); $('phone-code-form').hidden = false; notice(`A verification code is being sent to ${result.masked_phone || 'your WhatsApp number'}.`); $('phone-code').focus(); });
action('fallback-check', async () => { const result=await api('/phone/status',{}); if(result.verified) { sessionStorage.removeItem('squire-phone-challenge'); await refresh(); } else notice('Verification has not arrived yet. Send the command from the WhatsApp number you registered, then check again.'); });
action('phone-code-form', async () => { await api('/phone/verify', {challenge_id: phoneChallenge, code: $('phone-code').value}); sessionStorage.removeItem('squire-invitation'); sessionStorage.removeItem('squire-phone-challenge'); await refresh(); });
action('group-button', async () => { const result = await api('/groups/claims', {}); $('group-command').textContent = result.command || `!squire claim ${result.nonce}`; $('group-instructions').hidden = false; });
action('resume-button', async () => { await api('/trial/resume', {}); await refresh(); });
action('extension-button', async () => { await api('/trial/extension', {}); notice('Your extension request has been recorded.'); });
action('pause-button', async () => { await api('/account/state', {state: 'paused'}); await refresh(); });
action('delete-button', async () => { if (!confirm('Delete your Squire account and its agent storage? This cannot be undone.')) return; await api('/account/state', {state: 'deleted'}); account = null; render(); notice('Your account is disabled and deletion is queued.'); });
action('logout-button', async () => { await api('/auth/logout', {}); account = null; csrf = ''; render(); });
refresh(); setInterval(() => { if (account && !document.hidden) refresh(); }, 10000);

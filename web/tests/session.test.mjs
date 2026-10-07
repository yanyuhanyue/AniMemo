import test from 'node:test';
import assert from 'node:assert/strict';
import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { changeAccount } from '../src/accounts/session.ts';

test('sign in and sign out notify the mounted session observer and clear account data', async () => {
  const cache = new QueryClient();
  cache.setQueryData(['session'], null);
  cache.setQueryData(['journal', 'previous-user'], { private: 'previous notes' });
  const observer = new QueryObserver(cache, { queryKey: ['session'], enabled: false });
  const seen = [];
  const unsubscribe = observer.subscribe(result => seen.push(result.data));
  const user = { id: 'new-user', email: 'new@example.test', display_name: '小春' };
  await changeAccount(cache, user);
  assert.deepEqual(observer.getCurrentResult().data, user);
  assert.deepEqual(seen.at(-1), user);
  assert.equal(cache.getQueryData(['journal', 'previous-user']), undefined);
  await changeAccount(cache, null);
  assert.equal(observer.getCurrentResult().data, null);
  assert.equal(seen.at(-1), null);
  unsubscribe(); cache.clear();
});

test('an old account response is cancelled before the new session is exposed', async () => {
  const cache = new QueryClient();
  cache.setQueryData(['session'], { id: 'old-user' });
  let aborted = false;
  const request = cache.fetchQuery({ queryKey: ['journal', 'old-user'], queryFn: ({ signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); }, { once: true });
  }) }).catch(() => {});
  const user = { id: 'new-user', email: 'new@example.test', display_name: '小春' };
  await changeAccount(cache, user);
  await request;
  assert.equal(aborted, true);
  assert.deepEqual(cache.getQueryData(['session']), user);
  assert.equal(cache.getQueryData(['journal', 'old-user']), undefined);
  cache.clear();
});

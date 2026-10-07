import type { QueryClient } from '@tanstack/react-query';
import type { User } from '../api/client';

export async function changeAccount(cache: QueryClient, user: User | null) {
  await cache.cancelQueries();
  // Keep the observed session query alive so its subscribers see the transition.
  cache.removeQueries({ predicate: query => query.queryKey[0] !== 'session' });
  cache.setQueryData(['session'], user);
}

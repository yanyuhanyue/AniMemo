import type { QueryClient } from '@tanstack/react-query';
import type { User } from '../api/client';

export async function changeAccount(cache: QueryClient, user: User | null) {
  await cache.cancelQueries({ predicate: query => query.queryKey[0] !== 'site' });
  // Keep observers attached to the session and account-independent site identity.
  cache.removeQueries({ predicate: query => !['session', 'site'].includes(String(query.queryKey[0])) });
  cache.setQueryData(['session'], user);
}

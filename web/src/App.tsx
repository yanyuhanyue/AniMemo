import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Auth } from './accounts/Auth';
import { changeAccount } from './accounts/session';
import { client, errorMessage, result } from './api/client';
import { Journal } from './journal/Journal';

export function App() {
  const cache = useQueryClient();
  const session = useQuery({
    queryKey: ['session'], retry: false,
    queryFn: async ({ signal }) => {
      const response = await client.GET('/api/v1/auth/me', { signal });
      if (response.response.status === 401) return null;
      return result(Promise.resolve(response));
    },
  });
  const logout = useMutation({ mutationFn: () => result(client.POST('/api/v1/auth/logout')), onSuccess: () => changeAccount(cache, null) });
  if (session.isPending) return <main className="opening-screen" role="status"><span className="wordmark">AniMemo.</span><span className="spinner" />正在打开你的记忆库…</main>;
  if (session.isError) return <main className="opening-screen"><span className="wordmark">AniMemo.</span><h1>暂时无法连接</h1><p role="alert">{errorMessage(session.error)}</p><button className="button primary" onClick={() => session.refetch()}>重新连接</button></main>;
  if (!session.data) return <Auth onAuthenticated={user => changeAccount(cache, user)} />;
  return <>{logout.isError && <div className="global-error" role="alert">退出失败：{errorMessage(logout.error)}</div>}<Journal key={session.data.id} user={session.data} onLogout={() => logout.mutate()} loggingOut={logout.isPending} /></>;
}

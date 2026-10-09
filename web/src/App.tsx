import { MemoryWorkspace, Universe, SharedMemoryPage, SiteHomepage } from './memory/Memory';
import { Button } from './components/ui/Button';
import { MyColumns, FeaturedColumns, ReadColumn } from './columns/Columns';
import { Explore, PublicJournal, SharedEntry, PageShell } from './public/Public';
import { Admin, Setup } from './admin/Admin';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Auth } from './accounts/Auth';
import { EmailConfirmation } from './accounts/EmailAccess';
import { Connections } from './accounts/Connections';
import { changeAccount } from './accounts/session';
import { client, errorMessage, result } from './api/client';
import { ThemeProvider } from './themes/Themes';
import { Journal } from './journal/Journal';

export function App() {
  const path = window.location.pathname;
  if (path === '/verify-email' || path === '/reset-password') return <EmailConfirmation purpose={path === '/verify-email' ? 'verify' : 'reset'} />;
  if (path === '/home') return <SiteHomepage />;
  const memoryShare = path.match(/^\/memory-share\/([a-f0-9]{64})$/);
  if (memoryShare?.[1]) return <SharedMemoryPage token={memoryShare[1]} />;
  if (path === '/explore') return <Explore />;
  if (path === '/catalog') return <PublicJournal />;
  if (path === '/featured') return <FeaturedColumns />;
  const columnRoute = path.match(/^\/columns\/([a-f0-9-]{36})$/i);
  if (columnRoute?.[1]) return <ReadColumn id={columnRoute[1]} />;
  if (path === '/setup') return <Setup />;
  const publicRoute = path.match(/^\/(u|s)\/([a-f0-9-]{36})$/i);
  if (publicRoute?.[2]) return publicRoute[1] === 'u' ? <PublicJournal slug={publicRoute[2]} /> : <SharedEntry slug={publicRoute[2]} />;
  if (path !== '/' && path !== '/admin' && path !== '/my-columns' && path !== '/connections' && path !== '/memory' && path !== '/universe') return <PageShell title="这一页不存在"><a href="/">返回我的手账</a></PageShell>;
  return <PrivateApp admin={path === '/admin'} columns={path === '/my-columns'} connections={path === '/connections'} />;
}
function PrivateApp({ admin, columns, connections }: { admin: boolean; columns: boolean; connections: boolean }) {
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
  if (session.isError) return <main className="opening-screen"><span className="wordmark">AniMemo.</span><h1>暂时无法连接</h1><p role="alert">{errorMessage(session.error)}</p><Button className="button primary" onClick={() => session.refetch()}>重新连接</Button></main>;
  if (!session.data) return <Auth onAuthenticated={user => changeAccount(cache, user)} />;
  if (location.pathname === '/memory') return <ThemeProvider key={session.data.id} userID={session.data.id}><MemoryWorkspace user={session.data} /></ThemeProvider>;
  if (location.pathname === '/universe') return <Universe key={session.data.id} user={session.data} />;
  if (columns) return <MyColumns userID={session.data.id} />;
  if (connections) return <Connections userID={session.data.id} />;
  if (admin) return session.data.is_admin ? <Admin displayName={session.data.display_name} /> : <PageShell title="需要管理员权限"><a href="/">返回我的手账</a></PageShell>;
  return <ThemeProvider key={session.data.id} userID={session.data.id}>{logout.isError && <div className="global-error" role="alert">退出失败：{errorMessage(logout.error)}</div>}<Journal key={session.data.id} user={session.data} onLogout={() => logout.mutate()} loggingOut={logout.isPending} /></ThemeProvider>;
}

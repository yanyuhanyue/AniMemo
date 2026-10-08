import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App } from './App';
import { ApiError } from './api/client';
import { SiteIdentity } from './components/SiteIdentity';
import './styles.css';

const queryClient = new QueryClient({ defaultOptions: {
  queries: { staleTime: 30_000, retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1 },
  mutations: { retry: false },
} });

createRoot(document.getElementById('root')!).render(<StrictMode><QueryClientProvider client={queryClient}><SiteIdentity /><App /></QueryClientProvider></StrictMode>);

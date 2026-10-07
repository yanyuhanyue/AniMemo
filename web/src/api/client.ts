import createClient from 'openapi-fetch';
import type { components, paths } from './schema';

export type User = components['schemas']['User'];
export type Entry = components['schemas']['Entry'];
export type WatchRecord = components['schemas']['WatchRecord'];
export type Status = Entry['status'];
export type Accent = Entry['accent'];

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export const client = createClient<paths>({ baseUrl: '', credentials: 'same-origin' });

export async function result<T>(request: Promise<{ data?: T; error?: components['schemas']['Error']; response: Response }>): Promise<T> {
  const response = await request;
  if (!response.response.ok) {
    throw new ApiError(response.response.status, response.error?.error.code || 'request_failed', response.error?.error.message || '暂时无法连接，请稍后重试。');
  }
  return response.data as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return '连接暂时中断，请检查网络后重试。';
}

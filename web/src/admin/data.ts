import { useQuery } from '@tanstack/react-query';
import { client, result } from '../api/client';

export function useAdminStatus() {
  return useQuery({ queryKey: ['admin', 'status'], queryFn: ({ signal }) => result(client.GET('/api/v1/admin/status', { signal })), refetchInterval: 30000 });
}
export function useRuntime() {
  return useQuery({ queryKey: ['admin', 'runtime'], queryFn: ({ signal }) => result(client.GET('/api/v1/admin/runtime', { signal })), refetchInterval: 10000 });
}
export function useAudit(page: number) {
  return useQuery({ queryKey: ['admin', 'audit', page], queryFn: ({ signal }) => result(client.GET('/api/v1/admin/audit', { signal, params: { query: { page } } })) });
}
export const formatSize = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
export const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
export const auditLabel = (action: string) => ({
  'grant-admin': '授予管理员', 'remove-admin': '移除管理员', disable: '停用账号', enable: '恢复账号',
  'revoke-sessions': '撤销登录', 'approve-public': '通过公开申请', 'reject-public': '拒绝公开申请', 'hide-public': '下架公开手账',
  'plugin.install': '安装扩展', 'plugin.activate': '启用扩展', 'plugin.disable': '停用扩展', 'plugin.uninstall': '卸载扩展',
  'update-settings': '更新站点设置', setup: '初始化实例', 'clear-expired': '清理过期数据',
  trash: '移入回收站', restore: '恢复内容', hide: '下架内容', unhide: '解除隐藏', approve: '通过审核', reject: '拒绝审核', feature: '设为精选', unfeature: '取消精选',
  'save-preset': '保存标签预设', 'delete-preset': '删除标签预设',
} as Record<string, string>)[action] || action;

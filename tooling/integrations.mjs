import { parseEnv } from 'node:util';
export const integrationNames=['RESEND_API_KEY','RESEND_FROM_EMAIL','BANGUMI_OAUTH_CLIENT_ID','BANGUMI_OAUTH_CLIENT_SECRET','BANGUMI_OAUTH_REDIRECT_URI','R2_ENDPOINT','R2_BUCKET','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY'];
export function integrationValues(values={}) {
 if(!values||typeof values!=='object'||Array.isArray(values)||Object.keys(values).some(k=>!integrationNames.includes(k))||Object.values(values).some(v=>typeof v!=='string'||v.length>4096||/[\r\n\0]/.test(v)))throw new Error('Invalid external service configuration.');
 return Object.fromEntries(integrationNames.map(k=>[k,values[k]||'']));
}
export const parseIntegrations=text=>integrationValues(parseEnv(text.replace(/^\uFEFF/,'')));

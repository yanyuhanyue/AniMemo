import assert from 'node:assert/strict';
import { test } from 'node:test';
import { integrationValues,parseIntegrations } from './integrations.mjs';
test('external service env preserves quoted credentials and rejects unknown variables',()=>{
 const parsed=parseIntegrations('RESEND_API_KEY="test-key#with-symbols"\nRESEND_FROM_EMAIL="AniMemo <test@example.test>"\n');
 assert.equal(parsed.RESEND_API_KEY,'test-key#with-symbols');assert.equal(parsed.R2_SECRET_ACCESS_KEY,'');
 assert.throws(()=>parseIntegrations('DATABASE_URL=overridden'),/Invalid/);
 assert.throws(()=>integrationValues({R2_BUCKET:42}),/Invalid/);
 assert.throws(()=>integrationValues({RESEND_FROM_EMAIL:'first\nsecond'}),/Invalid/);
 assert.equal(Object.keys(integrationValues()).length,9);
});

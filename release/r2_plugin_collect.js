// Executed only by the connected Cloudflare plugin host. Arguments are public.
async (request, cloudflare, accountId) => {
  const result = {
    schema: 'animemo.cloudflare-plugin-origin-response/v1',
    producer: 'CODEX_CLOUDFLARE_PLUGIN',
    request_id: request.request_id,
    request_digest: request.request_digest,
    collector_sha256: request.collector_sha256,
    started_at: new Date().toISOString(), completed_at: null,
    bucket_read: null, list_reads: [], object_reads: [], failure: null,
  };
  const fail = code => { result.failure = code; };
  try {
    const prefix = `yanyuhanyue/AniMemo/releases/download/${request.candidate_version}/`;
    const names = [`animemo-${request.candidate_version}-portable.tar`, 'checksums.txt',
      'deployment-contract.json', 'installer-materials.tar', 'mirror-receipt.json', 'release-manifest.json'];
    const expectedKeys = names.map(name => prefix + name);
    if (request.schema !== 'animemo.cloudflare-plugin-origin-request/v1'
        || !/^v2\.\d+\.\d+-rc\.[1-9][0-9]*$/.test(request.candidate_version)
        || accountId !== 'd6a6e23b63921b1ed386645022fa92a7' || request.account_id !== accountId
        || request.bucket !== 'animemo-release-mirror' || request.jurisdiction !== 'default'
        || request.prefix !== prefix || JSON.stringify(request.expected_keys) !== JSON.stringify(expectedKeys)
        || !['PRESTATE', 'POSTSTATE'].includes(request.role)
        || Date.parse(request.expires_at) <= Date.now()) {
      fail('SCOPE_INVALID');
      return result;
    }
    const base = `/accounts/${accountId}/r2/buckets/animemo-release-mirror`;
    const bucket = await cloudflare.request({method: 'GET', path: base});
    result.bucket_read = {method: 'GET', path: base, status: bucket.status, success: bucket.success,
      errors: (bucket.errors || []).map(e => e.code), name: bucket.result?.name, jurisdiction: bucket.result?.jurisdiction};
    if (!bucket.success || bucket.status !== 200 || bucket.result?.name !== request.bucket
        || bucket.result?.jurisdiction !== request.jurisdiction || (bucket.errors || []).length) {
      fail('BUCKET_UNVERIFIED');
      return result;
    }
    let cursor;
    const seen = new Set();
    for (let page = 0; page < 64; page++) {
      const query = {prefix, per_page: 1000};
      if (cursor) query.cursor = cursor;
      const reply = await cloudflare.request({method: 'GET', path: base + '/objects', query});
      const pagination = reply.result_info ?? null;
      result.list_reads.push({method: 'GET', path: base + '/objects', query,
        status: reply.status, success: reply.success, errors: (reply.errors || []).map(e => e.code),
        objects: Array.isArray(reply.result) ? reply.result.map(x => ({key: x.key, size: x.size})) : null,
        pagination, pagination_present: Object.prototype.hasOwnProperty.call(reply, 'result_info')});
      if (!reply.success || reply.status !== 200 || (reply.errors || []).length || !Array.isArray(reply.result)) {
        fail('LIST_FAILED'); return result;
      }
      if (reply.result.length || pagination?.delimited?.length) { fail('PREFIX_NON_EMPTY'); return result; }
      if (!pagination?.is_truncated && !pagination?.cursor) break;
      cursor = pagination?.cursor;
      if (typeof cursor !== 'string' || !cursor || seen.has(cursor) || page === 63) {
        fail('PAGINATION_INVALID'); return result;
      }
      seen.add(cursor);
    }
    for (const key of expectedKeys) {
      const path = base + '/objects/' + key;
      try {
        const reply = await cloudflare.request({method: 'GET', path});
        result.object_reads.push({method: 'GET', path, key, outcome: reply.success ? 'PRESENT' : 'API_ERROR',
          http_status: reply.status, cloudflare_error_code: null});
        fail(reply.success ? 'PREFIX_NON_EMPTY' : 'GET_FAILED'); return result;
      } catch (error) {
        const missing = error.message === 'Cloudflare API error: 10007: The specified key does not exist.';
        result.object_reads.push({method: 'GET', path, key, outcome: missing ? 'KEY_NOT_FOUND' : 'API_ERROR',
          http_status: null, cloudflare_error_code: missing ? 10007 : null});
        if (!missing) { fail('GET_FAILED'); return result; }
      }
    }
  } catch (_) {
    fail('PLUGIN_API_FAILED');
  } finally {
    result.completed_at = new Date().toISOString();
  }
  return result;
}

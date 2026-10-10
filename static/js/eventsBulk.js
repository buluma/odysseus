/**
 * Bulk-action helpers for the Events panel. Kept free of DOM access so the
 * verb list and the partial-failure handling can be tested under node.
 */

// Same verbs and short labels as the per-card buttons in events.js.
export const BULK_VERBS = [
  { verb: 'ack',         label: 'Ack' },
  { verb: 'investigate', label: 'Work' },
  { verb: 'resolve',     label: 'Done' },
  { verb: 'ignore',      label: 'Ignore' },
];

/**
 * Apply one verb to many events. A failure on one event must not stop the
 * rest, so the caller gets the updated events plus which ids succeeded/failed.
 */
export async function runBulkVerb(ids, verb, action) {
  const results = await Promise.allSettled(ids.map(id => action(id, verb)));
  const out = { updated: [], succeeded: [], failed: [] };
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') {
      out.succeeded.push(ids[i]);
      if (r.value) out.updated.push(r.value);
    } else {
      out.failed.push(ids[i]);
    }
  });
  return out;
}

import { sdk, type Schema } from './client';

export type ConnectionState = 'connecting' | 'online' | 'reconnecting' | 'offline';

/** A chunk-independent SSE parser: CRLF, comments and multi-line data are supported. */
export function createEventParser(onEvent: (event: Schema['Event']) => void) {
  let buffer = '';
  return (chunk: string) => {
    buffer += chunk;
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const frame = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (!data) continue;
      const value: unknown = JSON.parse(data);
      if (!value || typeof value !== 'object') throw new Error('状态通知格式错误');
      const event = value as Schema['Event'];
      if (!Number.isSafeInteger(event.id) || typeof event.type !== 'string' || !Number.isSafeInteger(event.revision)) throw new Error('状态通知格式错误');
      onEvent(event);
    }
    if (buffer.length > 1024 * 1024) throw new Error('状态通知超出大小限制');
  };
}

export function subscribeEvents(cursor: number, onChange: (event: Schema['Event']) => void, onState: (state: ConnectionState) => void, onResync: () => Promise<number>): () => void {
  const controller = new AbortController();
  let lastId = cursor;
  let connected = false;
  let retries = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const revisions = new Map<string, number>();
  async function connect() {
    if (controller.signal.aborted) return;
    onState(connected ? 'reconnecting' : 'connecting');
    let needsResync = false;
    try {
      const { data, error, response } = await sdk().GET('/v1/events', { params: { query: { after: lastId }, header: connected ? { 'Last-Event-ID': String(lastId) } : {} }, parseAs: 'stream', signal: controller.signal });
      if (!response.ok || error || !data) throw new Error('状态流暂不可用');
      connected = true; retries = 0; onState('online');
      const parser = createEventParser(event => {
        if (event.id <= lastId && event.type !== 'resync.required') return;
        if (event.type === 'resync.required') { needsResync = true; return; }
        lastId = event.id;
        const key = event.resource ? `${event.resource.kind}:${event.resource.id}` : event.job_id;
        if (key && event.revision <= (revisions.get(key) ?? -1)) return;
        if (key) revisions.set(key, event.revision);
        onChange(event);
      });
      const reader = data.getReader();
      const decoder = new TextDecoder();
      try {
        while (!controller.signal.aborted) {
          const result = await reader.read();
          if (result.done) break;
          parser(decoder.decode(result.value, { stream: true }));
          if (needsResync) break;
        }
      } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
      if (needsResync && !controller.signal.aborted) { lastId = await onResync(); revisions.clear(); connected = false; }
      if (!controller.signal.aborted) onState('reconnecting');
    } catch { if (!controller.signal.aborted) onState('offline'); }
    if (!controller.signal.aborted) timer = setTimeout(connect, Math.min(1000 * 2 ** retries++, 15000));
  }
  void connect();
  return () => { controller.abort(); if (timer) clearTimeout(timer); };
}

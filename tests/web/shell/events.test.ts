import { afterEach, describe, expect, it, vi } from 'vitest';
import { createEventParser, subscribeEvents } from '../../../apps/web/src/api/events';
import { sdk } from '../../../apps/web/src/api/client';
vi.mock('../../../apps/web/src/api/client', () => ({ sdk: vi.fn() }));

const event = (id: number, revision = id, type = 'resource.changed') => ({ id, type, occurred_at: '2026-10-12T02:00:00Z', resource: { kind: 'forecast', id: 'fc-demo' }, job_id: null, revision });
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });
describe('AC-19 fetch SSE', () => {
  it('跨网络分片解析CRLF、注释和完整事件', () => {
    const receive = vi.fn(); const parse = createEventParser(receive);
    const frame = `: heartbeat\r\n\r\nid: 3\r\nevent: resource.changed\r\ndata: ${JSON.stringify(event(3))}\r\n\r\n`;
    for (const char of frame) parse(char);
    expect(receive).toHaveBeenCalledExactlyOnceWith(event(3));
  });
  it('断线续读Last-Event-ID并丢弃重复及旧revision', async () => {
    vi.useFakeTimers();
    const receive = vi.fn(); const onState = vi.fn(); const resync = vi.fn();
    const get = vi.fn().mockImplementation(async () => ({ response: { ok: true }, data: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode([event(11), event(11), event(12, 9)].map(item => `data: ${JSON.stringify(item)}\n\n`).join(''))); controller.close(); } }) }));
    vi.mocked(sdk).mockReturnValue({ GET: get } as never);
    const stop = subscribeEvents(10, receive, onState, resync);
    await vi.advanceTimersByTimeAsync(1001);
    expect(get.mock.calls[0][1].params.query.after).toBe(10);
    expect(get.mock.calls[1][1].params.header['Last-Event-ID']).toBe('12');
    expect(receive).toHaveBeenCalledTimes(1);
    stop();
  });
  it('保留期过期后关闭旧流并重新取得快照游标', async () => {
    vi.useFakeTimers(); const get = vi.fn().mockImplementationOnce(async () => ({ response: { ok: true }, data: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event(20, 20, 'resync.required'))}\n\n`)); controller.close(); } }) })).mockImplementation(async () => ({ response: { ok: false } }));
    vi.mocked(sdk).mockReturnValue({ GET: get } as never); const resync = vi.fn().mockResolvedValue(40);
    const stop = subscribeEvents(10, vi.fn(), vi.fn(), resync);
    await vi.advanceTimersByTimeAsync(1001);
    expect(resync).toHaveBeenCalledOnce(); expect(get.mock.calls[1][1].params.query.after).toBe(40); stop();
  });
});

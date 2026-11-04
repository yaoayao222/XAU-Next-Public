import { describe, expect, it } from 'vitest';
import { decimalDisplay, label, money, ruleText } from '../../../apps/web/src/shared/format';
import { keepNewer } from '../../../apps/web/src/api/queries';
import { isLocalApi } from '../../../apps/web/src/api/client';

describe('AC-11 / AC-12 金额只显示，不改变后端判断', () => {
  it('以字符串按 half-up 显示，未知保留而非补零', () => {
    expect(decimalDisplay('2.004')).toBe('2.00');
    expect(decimalDisplay('2.005')).toBe('2.01');
    expect(decimalDisplay('-2.005')).toBe('-2.01');
    expect(decimalDisplay('9007199254740993.015')).toBe('9007199254740993.02');
    expect(money(null)).toBe('未知'); expect(money('0')).toBe('0.00 USD');
  });
  it('不将ACTIVE显示为已入场，不推测未知枚举', () => {
    expect(label('ACTIVE')).toBe('条件有效'); expect(label('future_state')).toContain('此版本暂不支持');
    expect(ruleText({ kind: 'manual_observation', instruction: '等待人工核对' })).toContain('需人工确认');
  });
});
describe('AC-19 状态与连接边界', () => {
  it('先发后到的旧revision不能覆盖较新快照', () => {
    const fresh = { revision: 4, event_cursor: 12 };
    expect(keepNewer(fresh, { revision: 3, event_cursor: 11 })).toBe(fresh);
    expect(keepNewer(fresh, { revision: 4, event_cursor: 10 })).toBe(fresh);
    expect(keepNewer(fresh, { revision: 5, event_cursor: 13 })).toEqual({ revision: 5, event_cursor: 13 });
  });
  it('不会向外站或含用户信息的URL发送本机会话令牌', () => {
    expect(isLocalApi('http://127.0.0.1:8765')).toBe(true);
    expect(isLocalApi('http://localhost:8765')).toBe(true);
    expect(isLocalApi('https://example.com')).toBe(false);
    expect(isLocalApi('http://user:secret@127.0.0.1')).toBe(false);
  });
});

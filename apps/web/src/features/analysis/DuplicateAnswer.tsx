import { useEffect, useId } from 'react';
import type { Schema } from '../../api/client';
import { downloadText } from '../../shared/format';
import './duplicate-answer.css';

export function DuplicateFieldNotice({ value, onLocate }: { value: Schema['DuplicateFieldDiagnostics']; onLocate: (line: number) => void }) {
  return <section className="notice duplicate-notice" aria-label="重复字段定位"><strong>回答出现重复字段，未通过检查</strong>{value.items.map((item, i) => <div key={`${item.path}-${i}`}><p>{item.label} <code>{item.path}</code></p><div className="actions"><button onClick={() => onLocate(item.first.line)}>查看首次位置 · 第{item.first.line}行</button><button onClick={() => onLocate(item.duplicate.line)}>查看重复位置 · 第{item.duplicate.line}行</button></div><p>{item.values_relation === 'identical' ? '两处内容相同；仍不能自动删除一份当作原回答。' : item.values_relation === 'conflicting' ? '两处内容不一致；系统不能代替模型选择其中一份。' : '两处内容是否一致无法可靠判断，继续保留失败。'}</p></div>)}{value.truncated && <p>共{value.total}处重复，先显示{value.items.length}处。</p>}<small>只定位保存原文，不自动修补、重新导入或重发模型。</small></section>;
}

export function LocatedAnswer({ text, value, line }: { text: string; value: Schema['DuplicateFieldDiagnostics']; line?: number }) {
  const id = useId(); const lines = text.split('\n');
  const marked = new Set(value.items.flatMap(item => [item.first.line, item.duplicate.line]));
  useEffect(() => {
    if (line) document.getElementById(`${id}-line-${line}`)?.scrollIntoView?.({ block: 'center', behavior: 'auto' });
  }, [id, line, text]);
  return <section className="located-answer" aria-label="带定位的保存原回答"><div className="row-between"><strong>已保存原回答（未修改）</strong><button onClick={() => downloadText('preserved-answer.json', text, 'application/json')}>下载原回答（不含行号）</button></div><p className="muted" aria-live="polite">{line ? `已定位第${line}行。` : '标记行是重复字段首次和再次出现的位置。'} 行号仅辅助查看，下载内容保持原文。</p><pre className="duplicate-original">{lines.map((content, i) => <span key={i} id={`${id}-line-${i + 1}`} className={`original-line${marked.has(i + 1) ? ' duplicate-mark' : ''}${line === i + 1 ? ' selected-line' : ''}`}><span className="original-line-number" aria-hidden="true">{i + 1}</span>{content}{i < lines.length - 1 ? '\n' : ''}</span>)}</pre></section>;
}

import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { Icon } from './Icon';

export function Dialog({ title, eyebrow, onClose, children, wide = false }: { title: string; eyebrow?: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleID = useId();
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    dialog?.querySelector<HTMLElement>('[data-initial-focus]')?.focus({ preventScroll: true });
    document.body.classList.add('dialog-open');
    return () => { dialog?.close(); document.body.classList.remove('dialog-open'); };
  }, []);
  return <dialog ref={ref} className={`dialog ${wide ? 'dialog-wide' : ''}`} aria-labelledby={titleID} onCancel={onClose}>
    <header className="dialog-header">
      <div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2 id={titleID}>{title}</h2></div>
      <button type="button" className="icon-button" onClick={onClose} aria-label="关闭窗口"><Icon name="close" /></button>
    </header>
    {children}
  </dialog>;
}

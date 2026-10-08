import { Dialog as Primitive } from '@base-ui/react/dialog';
import { useRef, type ReactNode } from 'react';
import { Icon } from './Icon';
import { Button } from './Button';

export function Dialog({ title, eyebrow, onClose, children, wide = false }: { title: string; eyebrow?: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const popup = useRef<HTMLDivElement>(null);
  return <Primitive.Root open onOpenChange={open => { if (!open) onClose(); }}>
    <Primitive.Portal>
      <Primitive.Backdrop className="dialog-backdrop" />
      <Primitive.Popup className={`dialog ${wide ? 'dialog-wide' : ''}`} ref={popup} initialFocus={() => popup.current?.querySelector<HTMLElement>('[data-initial-focus]') ?? popup.current}>
        <header className="dialog-header"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<Primitive.Title render={<h2 />}>{title}</Primitive.Title></div><Button type="button" className="icon-button" onClick={onClose} aria-label="关闭窗口"><Icon name="close" /></Button></header>
        {children}
      </Primitive.Popup>
    </Primitive.Portal>
  </Primitive.Root>;
}

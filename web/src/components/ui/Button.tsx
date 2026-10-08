import { Button as Primitive } from '@base-ui/react/button';
import type { ComponentPropsWithRef } from 'react';

// Preserve native form semantics behind the product's stable component API.
export function Button(props: ComponentPropsWithRef<'button'>) {
  return <Primitive {...props} type={props.type ?? 'submit'} />;
}

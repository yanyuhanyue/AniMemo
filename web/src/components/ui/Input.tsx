import { Input as Primitive } from '@base-ui/react/input';
import type { ComponentPropsWithRef } from 'react';

export function Input(props: ComponentPropsWithRef<'input'>) {
  return <Primitive {...props} />;
}

// next/dynamic for a plain page: the loaded component, inside Suspense
import { createElement, lazy, Suspense } from 'react';
export default function dynamic(load: () => Promise<any>) {
  const Lazy = lazy(() => load().then((c) => ({ default: c.default ?? c })));
  return (props: any) => createElement(Suspense, { fallback: null }, createElement(Lazy, props));
}

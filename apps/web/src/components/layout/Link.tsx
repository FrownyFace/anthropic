import type { MouseEvent } from 'react'

import { hrefFor, navigate, type Route } from '@/lib/router'

/**
 * Props for an `<a>` that navigates in-app on a plain left click and behaves like a normal link
 * otherwise (middle click, cmd/ctrl click, right click → open in a new tab / copy link).
 */
export function linkProps(route: Route, opts: { replace?: boolean } = {}) {
  return {
    href: hrefFor(route),
    onClick: (e: MouseEvent<HTMLAnchorElement>) => {
      if (e.defaultPrevented || e.button !== 0) return
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      navigate(route, opts)
    },
  }
}

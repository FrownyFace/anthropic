import { Fragment, type ReactNode } from 'react'
import { PanelRight } from 'lucide-react'

import { linkProps } from '@/components/layout/Link'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { SidebarTrigger } from '@/components/ui/sidebar'
import type { Route } from '@/lib/router'

export interface Crumb {
  label: string
  route?: Route
}

export function TopBar({
  crumbs,
  children,
  onToggleWorkspace,
  workspaceOpen,
}: {
  crumbs: Crumb[]
  /** Right-aligned content (status chips). */
  children?: ReactNode
  onToggleWorkspace?: () => void
  workspaceOpen?: boolean
}) {
  return (
    <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b border-border bg-background/85 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/70">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-1 h-4" />
      <Breadcrumb className="min-w-0">
        <BreadcrumbList className="flex-nowrap">
          {crumbs.map((c, i) => {
            const last = i === crumbs.length - 1
            return (
              <Fragment key={`${c.label}-${i}`}>
                <BreadcrumbItem className="min-w-0">
                  {last || !c.route ? (
                    <BreadcrumbPage className="truncate">{c.label}</BreadcrumbPage>
                  ) : (
                    <BreadcrumbLink className="truncate" {...linkProps(c.route)}>
                      {c.label}
                    </BreadcrumbLink>
                  )}
                </BreadcrumbItem>
                {/* The separator is an <li> too, so it must be a sibling of the item, not a child. */}
                {!last ? <BreadcrumbSeparator /> : null}
              </Fragment>
            )
          })}
        </BreadcrumbList>
      </Breadcrumb>
      <span className="flex-1" />
      <div className="flex min-w-0 items-center gap-2 overflow-x-auto">{children}</div>
      {onToggleWorkspace ? (
        <Button
          variant={workspaceOpen ? 'secondary' : 'ghost'}
          size="sm"
          className="ml-1 h-8 gap-1.5"
          aria-pressed={workspaceOpen}
          onClick={onToggleWorkspace}
          title="Toggle the workspace panel (files, diffs, logs)"
        >
          <PanelRight aria-hidden />
          <span className="hidden md:inline">Workspace</span>
        </Button>
      ) : null}
    </header>
  )
}

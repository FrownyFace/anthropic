import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useDefaultLayout, usePanelRef, type LayoutStorage } from 'react-resizable-panels'

import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { WorkspaceTabs } from '@/components/workspace/WorkspaceColumn'
import { useIsWide } from '@/hooks/useWorkspaceLayout'
import type { ViewState } from '@/lib/reducer'

const LAYOUT_ID = 'faultline.run-layout'
const PANEL_IDS = ['transcript', 'workspace']

function storageOrUndefined(): LayoutStorage | undefined {
  try {
    const s = window.localStorage
    // A blocked storage throws on access; probe once.
    s.getItem(LAYOUT_ID)
    return s
  } catch {
    return undefined
  }
}

/**
 * Transcript column + workspace panel.
 *
 * ≥1280 px: shadcn Resizable panels — drag the handle, double-click it to reset, collapse the
 * workspace to zero; the split is remembered per browser (localStorage). Below that: the
 * transcript alone, with the workspace as an overlay sheet (the Sheet is portaled, so it is not
 * rendered at all on wide screens).
 */
export function RunLayout({
  children,
  state,
  live,
  open,
  onOpenChange,
}: {
  children: ReactNode
  state: ViewState | null
  live: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const wide = useIsWide()
  const panelRef = usePanelRef()
  const storage = useMemo(() => storageOrUndefined(), [])
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: LAYOUT_ID,
    panelIds: PANEL_IDS,
    storage,
    onlySaveAfterUserInteractions: true,
  })
  // A remembered split is only reused when the workspace was actually visible in it: a collapsed
  // or near-zero workspace is never restored, so every new conversation or replay opens with the
  // panel on screen (the user can still collapse it for the current page).
  const restoredLayout = useMemo(() => {
    const w = defaultLayout?.workspace
    return defaultLayout && typeof w === 'number' && w >= 15 ? defaultLayout : undefined
  }, [defaultLayout])
  // Ignore the panel's resize callback until it has mounted with its initial size, so a transient
  // collapsed measurement during mount cannot flip the open state.
  const mounted = useRef(false)
  useEffect(() => {
    const t = setTimeout(() => {
      mounted.current = true
    }, 0)
    return () => {
      clearTimeout(t)
      mounted.current = false
    }
  }, [])

  // Keep the imperative panel in step with the `open` state (top-bar toggle, breakpoint changes).
  useEffect(() => {
    if (!wide) return
    const p = panelRef.current
    if (!p) return
    if (open && p.isCollapsed()) p.expand()
    else if (!open && !p.isCollapsed()) p.collapse()
  }, [open, wide, panelRef])

  if (!wide) {
    return (
      <>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
        <Sheet open={open} onOpenChange={onOpenChange}>
          <SheetContent side="right" className="flex w-[92vw] flex-col gap-0 p-0 sm:max-w-[520px]">
            <SheetHeader className="border-b border-border px-4 py-3">
              <SheetTitle>Workspace</SheetTitle>
              <SheetDescription>Files, diffs and logs for this run.</SheetDescription>
            </SheetHeader>
            <WorkspaceTabs state={state} live={live} />
          </SheetContent>
        </Sheet>
      </>
    )
  }

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={restoredLayout}
      onLayoutChanged={onLayoutChanged}
      className="h-auto min-h-0 flex-1"
    >
      <ResizablePanel id="transcript" defaultSize="64" minSize={420}>
        <div className="flex h-full min-h-0 min-w-0 flex-col">{children}</div>
      </ResizablePanel>
      <ResizableHandle
        withHandle
        aria-label="Resize the workspace panel"
        className="w-1.5 bg-transparent hover:bg-border/60 data-[resize-handle-active]:bg-brand/40"
      />
      <ResizablePanel
        id="workspace"
        defaultSize="36"
        minSize={300}
        maxSize="65"
        collapsible
        collapsedSize={0}
        panelRef={panelRef}
        onResize={() => {
          // Dragging the handle to zero collapses the panel; reflect that in the toggle state.
          if (!mounted.current) return
          const collapsed = panelRef.current?.isCollapsed() ?? false
          if (collapsed === open) onOpenChange(!collapsed)
        }}
      >
        <aside className="flex h-full min-h-0 flex-col border-l border-border bg-card/30" aria-label="Workspace">
          <WorkspaceTabs state={state} live={live} />
        </aside>
      </ResizablePanel>
    </ResizablePanelGroup>
  )
}

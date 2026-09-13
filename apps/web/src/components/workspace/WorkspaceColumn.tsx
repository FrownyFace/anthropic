import { useMemo, useState } from 'react'

import { CodeBlock, FileStatusTable } from '@/components/bui'
import { LogsPanel } from '@/components/LogsPanel'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { ViewState } from '@/lib/reducer'
import type { FileDiff, FileEntry } from '@/lib/types'

const NO_FILES: FileEntry[] = []
const NO_DIFFS: FileDiff[] = []

export function WorkspaceTabs({ state, live }: { state: ViewState | null; live: boolean }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState('files')
  const files = state?.files ?? NO_FILES
  const diffs = state?.diffs ?? NO_DIFFS
  const changed = useMemo(() => files.filter((f) => f.status !== 'unchanged').length, [files])
  const ordered = useMemo(
    () => (selected ? [...diffs].sort((a, b) => (a.path === selected ? -1 : b.path === selected ? 1 : 0)) : diffs),
    [diffs, selected],
  )

  return (
    <Tabs value={tab} onValueChange={(v) => setTab(String(v))} className="flex min-h-0 flex-1 flex-col gap-0">
      <div className="flex items-center gap-3 border-b border-border px-3 py-2">
        <TabsList variant="line">
          <TabsTrigger value="files">Files</TabsTrigger>
          <TabsTrigger value="diffs">Diffs</TabsTrigger>
          <TabsTrigger value="logs">Logs</TabsTrigger>
        </TabsList>
        <span className="flex-1" />
        {files.length > 0 ? (
          <Badge variant="outline" className="font-mono text-[11px] text-muted-foreground">
            {changed} changed
          </Badge>
        ) : null}
      </div>
      <TabsContent value="files" className="min-h-0 flex-1 overflow-y-auto p-3">
        {files.length === 0 ? (
          <p className="p-2 text-sm text-muted-foreground">
            No workspace snapshot yet — it arrives with the first mutating step.
          </p>
        ) : (
          <FileStatusTable
            files={files}
            selected={selected}
            onSelect={(p) => {
              setSelected(p)
              if (diffs.some((d) => d.path === p)) setTab('diffs')
            }}
          />
        )}
      </TabsContent>
      <TabsContent value="diffs" className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {ordered.length === 0 ? (
          <p className="p-2 text-sm text-muted-foreground">Nothing has changed on disk yet.</p>
        ) : (
          ordered.map((d) => <CodeBlock key={d.path} diff={d.unified} filename={d.path} />)
        )}
      </TabsContent>
      <TabsContent value="logs" className="min-h-0 flex-1 overflow-hidden">
        {state ? <LogsPanel state={state} live={live} /> : null}
      </TabsContent>
    </Tabs>
  )
}

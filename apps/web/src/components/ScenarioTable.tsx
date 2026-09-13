import { useMemo } from 'react'
import { createColumnHelper, flexRender, tableFeatures, useTable, type ColumnDef } from '@tanstack/react-table'
import { Loader2, Play, Rewind, Zap } from 'lucide-react'

import { FaultKindBadge, FaultPublicBadge } from '@/components/FaultBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { faultWords } from '@/lib/format'
import { demoFor } from '@/lib/replay'
import type { Scenario } from '@/lib/types'

const features = tableFeatures({})

/** Fixed layout: long descriptions wrap in their own column instead of squeezing the others. */
const COLUMN_WIDTH: Record<string, string> = {
  title: '22%',
  description: '36%',
  steps: '9%',
  faults: '17%',
  run: '8%',
  replay: '8%',
}

export interface ScenarioTableProps {
  scenarios: Scenario[]
  loading?: boolean
  error?: string | null
  selectedId: string | null
  /** id of the scenario whose run is being started */
  starting: string | null
  canRun: boolean
  onSelect: (id: string) => void
  onRun: (scenario: Scenario) => void
  onReplay: (demoId: string) => void
}

/**
 * shadcn data table (TanStack Table + shadcn Table) of the bundled scenarios: title, what goes
 * wrong, step budget, the failures in play (one badge per `faults_public` row, with its origin
 * word), and one column each for Run (a live run) and Replay (a recorded run, no server). Clicking
 * a row loads it into the composer.
 */
export function ScenarioTable({
  scenarios,
  loading = false,
  error = null,
  selectedId,
  starting,
  canRun,
  onSelect,
  onRun,
  onReplay,
}: ScenarioTableProps) {
  const columnHelper = useMemo(() => createColumnHelper<typeof features, Scenario>(), [])
  // oxlint-disable-next-line typescript/no-explicit-any -- TanStack's accessor unions need a wide TValue
  const columns = useMemo<ColumnDef<typeof features, Scenario, any>[]>(
    () => [
      columnHelper.accessor('title', {
        id: 'title',
        header: 'Scenario',
        cell: ({ row }) => {
          const s = row.original
          const checks = s.checks ?? []
          return (
            <div className="min-w-0">
              <div className="text-[13.5px] leading-snug font-medium">{s.title}</div>
              <div className="mt-1 font-mono text-[11px] text-muted-foreground">{s.id}</div>
              {checks.length > 0 ? (
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <span className="mt-1 inline-block cursor-help text-[11px] text-muted-foreground/80 underline decoration-dotted underline-offset-2" />
                    }
                  >
                    scored on hidden tests + {checks.length} recovery {checks.length === 1 ? 'check' : 'checks'}
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">
                    <ul className="list-disc space-y-0.5 pl-4 text-[12px]">
                      {checks.map((c) => (
                        <li key={c.id}>{c.description}</li>
                      ))}
                    </ul>
                  </TooltipContent>
                </Tooltip>
              ) : null}
            </div>
          )
        },
      }),
      columnHelper.accessor('description', {
        id: 'description',
        header: 'What goes wrong',
        cell: ({ getValue }) => (
          <p className="text-[12.5px] leading-relaxed text-muted-foreground">{getValue()}</p>
        ),
      }),
      columnHelper.accessor('max_steps', {
        id: 'steps',
        header: () => <span className="whitespace-nowrap">Step budget</span>,
        cell: ({ getValue }) => (
          <span className="font-mono text-[12.5px] tabular-nums">{getValue()}</span>
        ),
      }),
      columnHelper.display({
        id: 'faults',
        header: 'Failure',
        cell: ({ row }) => {
          const s = row.original
          // The catalogue's per-origin list is the truth (a staged deletion and a simulated
          // refusal are different failures); fall back to kinds + harness faults for an older harness.
          const pub = s.faults_public ?? []
          const real = s.harness_faults ?? []
          const kinds = s.fault_kinds ?? []
          return (
            <div className="flex min-w-0 flex-wrap gap-1.5">
              {pub.length > 0 ? (
                pub.map((f, i) => <FaultPublicBadge key={`${f.origin}-${f.kind}-${i}`} fault={f} />)
              ) : (
                <>
                  {kinds.map((k) => (
                    <FaultKindBadge key={k} kind={k} />
                  ))}
                  {real.map((f) => (
                    <Tooltip key={`${f.kind}-${f.path}-${f.nth}`}>
                      <TooltipTrigger
                        render={
                          <Badge variant="outline" className="border-rose-500/30 bg-rose-500/10 font-mono text-rose-700 dark:text-rose-300">
                            <Zap aria-hidden /> real: {faultWords(f.kind)}
                          </Badge>
                        }
                      />
                      <TooltipContent>
                        Real, not simulated: {faultWords(f.kind)} on {f.tool} {f.path}
                        {f.nth > 1 ? ` (attempt ${f.nth})` : ''}.
                      </TooltipContent>
                    </Tooltip>
                  ))}
                </>
              )}
              {pub.length === 0 && kinds.length === 0 && real.length === 0 ? (
                <span className="text-[12px] text-muted-foreground">none listed</span>
              ) : null}
            </div>
          )
        },
      }),
      columnHelper.display({
        id: 'run',
        header: 'Run',
        cell: ({ row }) => {
          const s = row.original
          const busy = starting === s.id
          return (
            <Button
              size="sm"
              className="w-full justify-center"
              onClick={(e) => {
                e.stopPropagation()
                onRun(s)
              }}
              disabled={!canRun || busy}
              title={canRun ? `Start a live run of ${s.id}` : 'Live runs are offline'}
            >
              {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Play aria-hidden />}
              {busy ? 'Starting…' : 'Run'}
            </Button>
          )
        },
      }),
      columnHelper.display({
        id: 'replay',
        header: 'Replay',
        cell: ({ row }) => {
          const s = row.original
          const demo = demoFor(s.id)
          return demo ? (
            <Button
              size="sm"
              variant="outline"
              className="w-full justify-center"
              onClick={(e) => {
                e.stopPropagation()
                onReplay(demo.id)
              }}
              title={`Replay a recorded ${s.id} run (no server needed)`}
            >
              <Rewind aria-hidden /> Replay
            </Button>
          ) : (
            <Tooltip>
              <TooltipTrigger
                render={
                  <span className="inline-flex w-full">
                    <Button size="sm" variant="outline" className="w-full justify-center" disabled>
                      <Rewind aria-hidden /> Replay
                    </Button>
                  </span>
                }
              />
              <TooltipContent>No recording for this scenario yet.</TooltipContent>
            </Tooltip>
          )
        },
      }),
    ],
    [columnHelper, starting, canRun, onRun, onReplay],
  )

  const table = useTable<typeof features, Scenario>({
    features,
    columns,
    data: scenarios,
    getRowId: (row) => row.id,
  })

  return (
    <section aria-label="Scenarios" className="w-full">
      <div className="mb-3 space-y-1">
        <h2 className="text-sm font-medium">Scenarios</h2>
        <p className="text-[12.5px] text-muted-foreground">
          {canRun
            ? 'Run starts a live run with the model chosen above. Replay plays a recorded run in the browser and needs no server.'
            : 'Live runs are offline. Replay still works.'}
        </p>
      </div>
      <div className="overflow-x-auto rounded-xl border border-border bg-card/30">
        <Table className="w-full min-w-[900px] table-fixed">
          <TableHeader>
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id} className="hover:bg-transparent">
                {hg.headers.map((h) => (
                  <TableHead
                    key={h.id}
                    style={{ width: COLUMN_WIDTH[h.column.id] }}
                    className={h.column.id === 'run' || h.column.id === 'replay' ? 'text-center' : undefined}
                  >
                    {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {loading ? (
              [0, 1, 2].map((i) => (
                <TableRow key={`sk-${i}`}>
                  <TableCell><Skeleton className="h-5 w-40" /><Skeleton className="mt-2 h-3 w-20" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-full" /><Skeleton className="mt-2 h-4 w-3/4" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-8" /></TableCell>
                  <TableCell><Skeleton className="h-5 w-24" /></TableCell>
                  <TableCell><Skeleton className="h-8 w-full" /></TableCell>
                  <TableCell><Skeleton className="h-8 w-full" /></TableCell>
                </TableRow>
              ))
            ) : table.getRowModel().rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="py-8 text-center text-sm whitespace-normal text-muted-foreground">
                  {error ? `Could not load the scenarios (${error}).` : 'No scenarios.'} Recorded runs in the sidebar still work.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => {
                const active = row.original.id === selectedId
                return (
                  <TableRow
                    key={row.id}
                    data-state={active ? 'selected' : undefined}
                    aria-selected={active}
                    tabIndex={0}
                    onClick={() => onSelect(row.original.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onSelect(row.original.id)
                      }
                    }}
                    className={`cursor-pointer align-top ${active ? 'bg-muted/40' : ''}`}
                  >
                    {row.getAllCells().map((cell) => (
                      <TableCell
                        key={cell.id}
                        className={
                          cell.column.id === 'run' || cell.column.id === 'replay'
                            ? 'py-3 align-middle whitespace-normal'
                            : 'py-3 whitespace-normal'
                        }
                      >
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}

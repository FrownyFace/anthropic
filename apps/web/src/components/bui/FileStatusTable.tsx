/**
 * FileStatusTable — adapted from Beautiful UI `DiffTable` (MIT, © 2026 Shane Levine).
 *
 * The original was a "proposed edits" table with toggleable removals and an Apply button. This
 * keeps the card, the tinted rows and the dotted status pill, and swaps the demo menu for the
 * workspace snapshot (`FileEntry[]`): status, path, size, short sha. Changed files sort first.
 */

import { fmtBytes, shortSha } from '@/lib/format'
import type { FileEntry, FileStatus } from '@/lib/types'
import { cn } from '@/lib/utils'

export interface FileStatusTableProps {
  files: FileEntry[]
  /** Makes rows focusable/clickable. */
  onSelect?: (path: string) => void
  selected?: string | null
}

const ORDER: Record<FileStatus, number> = { added: 0, modified: 1, deleted: 2, unchanged: 3 }

const DOT: Record<FileStatus, string> = {
  added: 'bg-emerald-500 dark:bg-emerald-400',
  modified: 'bg-amber-500 dark:bg-amber-400',
  deleted: 'bg-rose-500 dark:bg-rose-400',
  unchanged: 'bg-ink-3',
}

const LABEL: Record<FileStatus, string> = {
  added: 'text-emerald-700 dark:text-emerald-300',
  modified: 'text-amber-700 dark:text-amber-300',
  deleted: 'text-rose-700 dark:text-rose-300',
  unchanged: 'text-ink-2',
}

const ROW: Record<FileStatus, string> = {
  added: 'bg-emerald-500/[0.06]',
  modified: '',
  deleted: 'bg-rose-500/[0.06]',
  unchanged: '',
}

/** Changed files first (added, modified, deleted), otherwise the snapshot's own order. */
export function sortFiles(files: FileEntry[]): FileEntry[] {
  return [...files].sort((a, b) => ORDER[a.status] - ORDER[b.status])
}

export function FileStatusTable({ files, onSelect: select, selected = null }: FileStatusTableProps) {
  const rows = sortFiles(files)
  const count = (s: FileStatus) => files.filter((f) => f.status === s).length
  const changed = files.length - count('unchanged')
  const interactive = typeof select === 'function'

  return (
    <div className="w-full overflow-hidden rounded-xl border border-line bg-surface" data-slot="file-status-table">
      <div className="flex h-10 items-center justify-between gap-2 border-b border-line px-3">
        <span className="text-[12.5px] font-medium text-ink">Workspace</span>
        <span className="font-mono text-[11px] text-ink-3 tabular-nums">
          {files.length === 0 ? 'no snapshot' : `${changed} changed · ${files.length} files`}
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full table-fixed border-collapse text-left">
          <colgroup>
            <col className="w-[112px]" />
            <col />
            <col className="w-[84px]" />
            <col className="w-[84px]" />
          </colgroup>
          <thead>
            <tr className="border-b border-line">
              {['status', 'path', 'size', 'sha'].map((h) => (
                <th
                  key={h}
                  scope="col"
                  className={cn(
                    'px-3 py-1.5 text-[10.5px] font-medium tracking-wide text-ink-3 uppercase',
                    h === 'size' && 'text-right',
                  )}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-[12.5px] text-ink-3">
                  No workspace snapshot yet — it arrives with the first mutating step.
                </td>
              </tr>
            ) : null}
            {rows.map((f, i) => {
              const isSelected = interactive && selected === f.path
              const deleted = f.status === 'deleted'
              return (
                <tr
                  key={f.path}
                  data-status={f.status}
                  tabIndex={interactive ? 0 : undefined}
                  aria-selected={interactive ? isSelected : undefined}
                  onClick={select ? () => select(f.path) : undefined}
                  onKeyDown={
                    select
                      ? (event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            select(f.path)
                          }
                        }
                      : undefined
                  }
                  className={cn(
                    'bui-fade-up border-b border-line transition-colors duration-150 last:border-0',
                    ROW[f.status],
                    interactive &&
                      'cursor-pointer hover:bg-muted/40 focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset focus-visible:outline-none',
                    isSelected && 'bg-muted/50',
                  )}
                  style={{ animationDelay: `${Math.min(i, 10) * 35}ms` }}
                >
                  <td className="px-3 py-1.5">
                    <span className="inline-flex h-5.5 items-center gap-1.5 rounded-full bg-muted/50 px-2 text-[11px] font-medium ring-1 ring-line ring-inset">
                      <span className={cn('size-1.5 rounded-full', DOT[f.status])} aria-hidden />
                      <span className={LABEL[f.status]}>{f.status}</span>
                    </span>
                  </td>
                  <td
                    className={cn(
                      'truncate px-3 py-1.5 font-mono text-[12px]',
                      deleted ? 'text-rose-700 dark:text-rose-300 line-through decoration-rose-500/50' : f.status === 'added' ? 'text-emerald-700 dark:text-emerald-300' : 'text-ink',
                    )}
                    title={f.path}
                  >
                    {f.path}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-[11.5px] text-ink-3 tabular-nums">
                    {fmtBytes(f.size)}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-[11px] text-ink-3">{shortSha(f.sha256)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {files.length > 0 ? (
        <div className="flex min-h-9 items-center border-t border-line px-3 text-[11.5px] text-ink-3 tabular-nums">
          {count('added')} added · {count('modified')} modified · {count('deleted')} deleted
        </div>
      ) : null}
    </div>
  )
}

import { useMemo, useState } from 'react'
import {
  Check,
  Copy,
  FileX2,
  GitBranchPlus,
  Lock,
  MessageSquare,
  Plus,
  RotateCcw,
  Rewind,
  ShieldCheck,
  Timer,
  Zap,
} from 'lucide-react'

import { linkProps } from '@/components/layout/Link'
import { ThemeSwitcher } from '@/components/layout/ThemeSwitcher'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarRail,
} from '@/components/ui/sidebar'
import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState, HealthPhase } from '@/hooks/useHarness'
import type { IdentityState } from '@/hooks/useIdentity'
import { getLogger } from '@/lib/log'
import { DEMOS } from '@/lib/replay'
import type { Route } from '@/lib/router'
import { gradeOf, statusDot, statusTitle } from '@/lib/runStatus'
import type { ConversationSummary } from '@/lib/types'

const log = getLogger('web')

const SCENARIO_ICON: Record<string, typeof MessageSquare> = {
  'missing-config': FileX2,
  'locked-file': Lock,
  'lost-ack': Timer,
  gauntlet: Zap,
}

/** Three states: a cold harness takes ~12 s to answer /health, and that is "checking", not down. */
const HEALTH_DOT: Record<HealthPhase, string> = {
  resolving: 'bg-muted-foreground/60',
  checking: 'bg-muted-foreground/60 animate-pulse',
  reachable: 'bg-emerald-500 dark:bg-emerald-400',
  unreachable: 'bg-rose-500 dark:bg-rose-400',
}

const HEALTH_WORD: Record<HealthPhase, string> = {
  resolving: 'resolving…',
  checking: 'checking…',
  reachable: 'reachable',
  unreachable: 'unreachable',
}

function dayBucket(iso: string, now = new Date()): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Earlier'
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const t = d.getTime()
  if (t >= startOfToday) return 'Today'
  if (t >= startOfToday - 86_400_000) return 'Yesterday'
  if (t >= startOfToday - 6 * 86_400_000) return 'Previous 7 days'
  return 'Earlier'
}

function groupByDay(items: ConversationSummary[]): { label: string; items: ConversationSummary[] }[] {
  const order = ['Today', 'Yesterday', 'Previous 7 days', 'Earlier']
  const buckets = new Map<string, ConversationSummary[]>()
  for (const c of items) {
    const key = dayBucket(c.updated_at || c.created_at)
    const arr = buckets.get(key) ?? []
    arr.push(c)
    buckets.set(key, arr)
  }
  return order.filter((k) => buckets.has(k)).map((k) => ({ label: k, items: buckets.get(k)! }))
}

function shortId(userId: string | null): string {
  if (!userId) return '—'
  return userId.length > 12 ? `${userId.slice(0, 10)}…` : userId
}

function ConversationRow({ c, active }: { c: ConversationSummary; active: boolean }) {
  const Icon = SCENARIO_ICON[c.scenario_id] ?? MessageSquare
  const status = c.last_run?.status ?? null
  // The summary carries only a score: 100 means every test and check passed, anything less means
  // something failed, null means no grade — so "ok" is green only when it really was a pass.
  const grade = gradeOf(null, c.last_run?.score)
  const route: Route = { kind: 'conversation', id: c.id }
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        isActive={active}
        tooltip={c.title}
        render={<a {...linkProps(route)} />}
        className="group/row"
      >
        <Icon aria-hidden className="text-muted-foreground" />
        <span className="truncate">{c.title}</span>
      </SidebarMenuButton>
      {status ? (
        <SidebarMenuBadge className="gap-1.5 font-mono text-[10px] text-muted-foreground">
          {c.last_run?.score !== null && c.last_run?.score !== undefined
            ? Math.round(c.last_run.score)
            : ''}
          <span
            aria-label={`last run ${statusTitle(status, grade)}`}
            title={statusTitle(status, grade)}
            className={`inline-block size-1.5 rounded-full ${statusDot(status, grade)}`}
          />
        </SidebarMenuBadge>
      ) : null}
    </SidebarMenuItem>
  )
}

export function AppSidebar({
  harness,
  conversations,
  route,
  identity,
}: {
  harness: HarnessState
  conversations: ConversationsState
  route: Route
  identity: IdentityState
}) {
  const [copied, setCopied] = useState(false)
  const groups = useMemo(() => groupByDay(conversations.items), [conversations.items])
  const activeConversation = route.kind === 'conversation' ? route.id : null
  const activeReplay = route.kind === 'replay' ? route.demoId : null
  const userId = identity.userId ?? null

  const copyId = async () => {
    if (!userId) return
    try {
      await navigator.clipboard.writeText(userId)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch (err) {
      log.warn('identity.copy_failed', 'clipboard unavailable', { error: String(err) })
    }
  }

  const resetIdentity = () => {
    identity.reset()
    log.info('identity.reset', 'minted a new browser user id; reloading')
    window.location.assign('/')
  }

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              size="lg"
              tooltip="Faultline"
              render={<a {...linkProps({ kind: 'home' })} />}
              className="data-[state=open]:bg-sidebar-accent"
            >
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-brand text-brand-ink">
                <GitBranchPlus className="size-4" aria-hidden />
              </div>
              <div className="grid flex-1 text-left leading-tight">
                <span className="truncate font-medium">Faultline</span>
                <span className="truncate text-[11px] text-muted-foreground">agent failure lab</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="New run"
              isActive={route.kind === 'home'}
              render={<a {...linkProps({ kind: 'home' })} />}
            >
              <Plus aria-hidden />
              <span>New run</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Conversations</SidebarGroupLabel>
          <SidebarGroupContent>
            {conversations.loading && conversations.items.length === 0 ? (
              <SidebarMenu>
                {[0, 1, 2].map((i) => (
                  <SidebarMenuItem key={i}>
                    <SidebarMenuSkeleton showIcon />
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            ) : conversations.items.length === 0 ? (
              <p className="px-2 py-1 text-[11px] leading-snug text-muted-foreground group-data-[collapsible=icon]:hidden">
                {conversations.error
                  ? `Could not load conversations: ${conversations.error}`
                  : 'No conversations yet for this browser. Start a run to create one.'}
              </p>
            ) : (
              groups.map((g) => (
                <div key={g.label} className="mb-1">
                  <div className="px-2 pt-1 pb-0.5 text-[10px] tracking-wide text-muted-foreground/70 uppercase group-data-[collapsible=icon]:hidden">
                    {g.label}
                  </div>
                  <SidebarMenu>
                    {g.items.map((c) => (
                      <ConversationRow key={c.id} c={c} active={c.id === activeConversation} />
                    ))}
                  </SidebarMenu>
                </div>
              ))
            )}
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Replays</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {DEMOS.map((d) => (
                <SidebarMenuItem key={d.id}>
                  <SidebarMenuButton
                    tooltip={`Replay ${d.label}`}
                    isActive={d.id === activeReplay}
                    render={<a {...linkProps({ kind: 'replay', demoId: d.id })} />}
                  >
                    <Rewind aria-hidden />
                    <span className="truncate">{d.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <SidebarMenuButton
                    size="lg"
                    tooltip="Browser identity"
                    className="data-[popup-open]:bg-sidebar-accent data-[state=open]:bg-sidebar-accent"
                  >
                    <Avatar className="size-8 rounded-lg">
                      <AvatarFallback className="rounded-lg font-mono text-[11px] uppercase">
                        {userId ? userId.slice(2, 4) : '??'}
                      </AvatarFallback>
                    </Avatar>
                    <div className="grid flex-1 text-left leading-tight">
                      <span className="truncate text-[13px]">Browser user</span>
                      <span className="truncate font-mono text-[11px] text-muted-foreground">
                        {shortId(userId)}
                      </span>
                    </div>
                  </SidebarMenuButton>
                }
              />
              <DropdownMenuContent side="top" align="start" className="w-72">
                {/* Base UI: labels must live inside a group. */}
                <DropdownMenuGroup>
                  <DropdownMenuLabel className="space-y-0.5">
                    <div className="text-xs font-medium">Anonymous browser identity</div>
                    <div className="font-mono text-[11px] break-all text-muted-foreground">{userId ?? '—'}</div>
                    <div className="text-[11px] font-normal text-muted-foreground">
                      Minted in this browser, kept in localStorage with a cookie mirror, sent as{' '}
                      <code className="font-mono">X-Faultline-User</code>. Conversations are scoped to it.
                    </div>
                  </DropdownMenuLabel>
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  {/* Not a DropdownMenuLabel: Base UI renders group labels aria-hidden, which would
                      hide the Theme radiogroup from assistive tech. */}
                  <div role="presentation" className="flex items-center justify-between gap-3 px-2 py-1.5">
                    <span className="text-xs text-muted-foreground">Theme</span>
                    <ThemeSwitcher />
                  </div>
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  <DropdownMenuItem onClick={() => void copyId()}>
                    {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
                    {copied ? 'Copied' : 'Copy user id'}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={resetIdentity}>
                    <RotateCcw aria-hidden />
                    Reset identity (new id, empty history)
                  </DropdownMenuItem>
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  {/* Status, not a label (see above): readable by assistive tech. */}
                  <div role="presentation" className="space-y-0.5 px-2 py-1.5">
                    <div className="flex items-center gap-1.5 text-[11px]" role="status">
                      <span className={`inline-block size-1.5 rounded-full ${HEALTH_DOT[harness.phase]}`} aria-hidden />
                      <span className="text-muted-foreground">harness {HEALTH_WORD[harness.phase]}</span>
                    </div>
                    {harness.health?.has_provider_key === false ? (
                      <div className="flex items-center gap-1.5 text-[11px] text-emerald-700 dark:text-emerald-300">
                        <ShieldCheck className="size-3" aria-hidden /> no provider key on the api function
                      </div>
                    ) : null}
                  </div>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}

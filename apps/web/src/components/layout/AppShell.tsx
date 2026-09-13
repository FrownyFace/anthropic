import type { ReactNode } from 'react'

import { AppSidebar } from '@/components/app-sidebar'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState } from '@/hooks/useHarness'
import type { IdentityState } from '@/hooks/useIdentity'
import type { Route } from '@/lib/router'

/**
 * shadcn sidebar block: a left rail that collapses to icons (cmd/ctrl+B, or the trigger in the
 * top bar) and a `<main>` inset that the pages fill. The rail lists the conversations that belong
 * to this browser's anonymous user (X-Faultline-User), the bundled replays, and the identity menu.
 */
export function AppShell({
  harness,
  conversations,
  route,
  identity,
  children,
}: {
  harness: HarnessState
  conversations: ConversationsState
  route: Route
  identity: IdentityState
  children: ReactNode
}) {
  return (
    <SidebarProvider defaultOpen>
      <AppSidebar harness={harness} conversations={conversations} route={route} identity={identity} />
      <SidebarInset className="min-h-dvh min-w-0 bg-background">{children}</SidebarInset>
    </SidebarProvider>
  )
}

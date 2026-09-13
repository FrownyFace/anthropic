import { TooltipProvider } from '@/components/ui/tooltip'
import { AppShell } from '@/components/layout/AppShell'
import { useConversations } from '@/hooks/useConversations'
import { useHarness } from '@/hooks/useHarness'
import { useIdentity } from '@/hooks/useIdentity'
import { useRoute } from '@/lib/router'
import { ConversationPage } from '@/pages/ConversationPage'
import { HomePage } from '@/pages/HomePage'
import { ReplayPage } from '@/pages/ReplayPage'
import { RunPage } from '@/pages/RunPage'

/**
 * Routes (path based, no router dependency — see src/lib/router.ts):
 *   /                        new run (composer + scenarios)
 *   /conversations/:id       a conversation loaded from the SQLite store via REST; ?run=<id> picks a run
 *   /runs/:id                a run outside a conversation (harness without the store)
 *   /replay/:demoId          bundled recorded run
 */
export default function App() {
  const route = useRoute()
  const identity = useIdentity()
  const harness = useHarness()
  const conversations = useConversations(harness.client)

  let page: React.ReactNode
  switch (route.kind) {
    case 'conversation':
      page = (
        <ConversationPage
          key={route.id}
          harness={harness}
          conversations={conversations}
          conversationId={route.id}
          runId={route.runId ?? null}
        />
      )
      break
    case 'run':
      page = <RunPage key={route.runId} harness={harness} runId={route.runId} />
      break
    case 'replay':
      page = <ReplayPage key={route.demoId} harness={harness} demoId={route.demoId} />
      break
    default:
      page = <HomePage harness={harness} conversations={conversations} />
  }

  return (
    <TooltipProvider>
      <AppShell harness={harness} conversations={conversations} route={route} identity={identity}>
        {page}
      </AppShell>
    </TooltipProvider>
  )
}

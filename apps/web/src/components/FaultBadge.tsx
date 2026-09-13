import { AlertTriangle, CheckCircle2, FileX2, Lock, OctagonAlert, Timer, Zap } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { originText, taxonomyLabel } from '@/lib/codes'
import { FAULT_KIND_BLURB, faultBlurb, faultWords } from '@/lib/format'
import type { ToolCallView } from '@/lib/reducer'
import type { ErrorClass, ErrorOrigin, FaultFired, FaultKind, FaultPublic } from '@/lib/types'

const ICON: Record<FaultKind, typeof FileX2> = {
  missing_file: FileX2,
  denied_write: Lock,
  ack_lost: Timer,
}

/**
 * Colour by origin, as docs/error-taxonomy.md prescribes: injected ("simulated") and staged are
 * both amber — nothing real broke, or it broke on purpose at reset — and only a real failure is
 * red. The word on the badge is what keeps the two amber cases apart.
 */
const ORIGIN_TONE: Record<ErrorOrigin, string> = {
  injected: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  staged: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  real: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
}

/** A scenario's fault kind (scenario table): either mode of the kind may be in play. */
export function FaultKindBadge({ kind, className }: { kind: FaultKind; className?: string }) {
  const Icon = ICON[kind] ?? AlertTriangle
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge variant="outline" className={`${ORIGIN_TONE.injected} ${className ?? ''} font-mono`}>
            <Icon aria-hidden />
            {faultWords(kind)}
          </Badge>
        }
      />
      <TooltipContent>{FAULT_KIND_BLURB[kind] ?? 'Simulated failure.'}</TooltipContent>
    </Tooltip>
  )
}

/**
 * One fault class from the scenario catalogue (`Scenario.faults_public`): the origin word the
 * transcript will later use ("staged" / "simulated" / "real") plus the kind in plain words, so the
 * table teaches the vocabulary the run is narrated in. Amber for staged and simulated, red for real.
 */
export function FaultPublicBadge({ fault }: { fault: FaultPublic }) {
  const Icon = ICON[fault.kind as FaultKind] ?? (fault.origin === 'real' ? Zap : AlertTriangle)
  const tone = ORIGIN_TONE[fault.origin] ?? ORIGIN_TONE.injected
  const label = taxonomyLabel(fault.origin, fault.kind, fault.layer)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge variant="outline" className={`${tone} font-mono`} data-origin={fault.origin}>
            <Icon aria-hidden />
            {originText(fault.origin)}: {faultWords(fault.kind)}
          </Badge>
        }
      />
      <TooltipContent className="max-w-xs">
        {label ? <div className="font-mono text-[11px]">{label}</div> : null}
        <div>{faultBlurb(fault.kind, fault.origin) || fault.description}</div>
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The badge on a call that a fault hit. Carries the origin word ("simulated" for an injected
 * fault, "staged" for a condition set up at reset) so a reader never mistakes it for a real error;
 * the tooltip is the taxonomy's fixed label plus what actually happened on disk.
 */
export function FaultFiredBadge({ fault }: { fault: FaultFired }) {
  const Icon = ICON[fault.kind] ?? AlertTriangle
  const origin: ErrorOrigin = fault.origin ?? 'injected'
  const label = taxonomyLabel(origin, fault.kind, fault.layer ?? null)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge variant="outline" className={`${ORIGIN_TONE[origin]} font-mono`} data-origin={origin}>
            <Icon aria-hidden />
            {originText(origin)}: {faultWords(fault.kind)}
          </Badge>
        }
      />
      <TooltipContent className="max-w-xs">
        {label ? <div className="font-mono text-[11px]">{label}</div> : null}
        <div>{fault.description || faultBlurb(fault.kind, origin) || 'Simulated failure.'}</div>
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The badge for a classified error that no fault explains (docs/error-taxonomy.md `ErrorClass`):
 * red for a real failure, amber for an injected / staged one the record classified without a
 * `fault` payload.
 */
export function ErrorOriginBadge({ errorClass }: { errorClass: ErrorClass }) {
  const real = errorClass.origin === 'real'
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge variant="outline" className={`${ORIGIN_TONE[errorClass.origin]} font-mono`} data-origin={errorClass.origin}>
            {real ? <OctagonAlert aria-hidden /> : <AlertTriangle aria-hidden />}
            {originText(errorClass.origin)}: {errorClass.code}
          </Badge>
        }
      />
      <TooltipContent className="max-w-xs">
        <div className="font-mono text-[11px]">{errorClass.label}</div>
        {errorClass.outcome_known === false ? (
          <div>Whether the call ran is unknown — the right move is to read the state back before retrying.</div>
        ) : null}
        {errorClass.detail ? <div className="opacity-80">{errorClass.detail}</div> : null}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The badge trio for one tool call, in one place so every renderer agrees: the fault that hit it
 * (origin word), the classified error when it is real or unexplained by a fault, and the
 * read-back hint. Rendered once per call — by ToolCallChips, or by ThinkingTrace's rail rows when
 * no chips are nested inside it.
 */
export function CallBadges({ call }: { call: Pick<ToolCallView, 'fault' | 'result' | 'recovered'> }) {
  const ec = call.result?.errorClass ?? null
  return (
    <>
      {call.fault ? <FaultFiredBadge fault={call.fault} /> : null}
      {ec && (ec.origin === 'real' || !call.fault) ? <ErrorOriginBadge errorClass={ec} /> : null}
      {call.recovered ? <RecoveredBadge /> : null}
    </>
  )
}

/**
 * A live hint, not a verdict: the reducer saw a later call on this path succeed after a
 * successful read of it. The grader's checks (ScoreRows) are the authoritative word on recovery.
 */
export function RecoveredBadge() {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Badge
            variant="outline"
            className="border-emerald-500/30 bg-emerald-500/15 font-mono text-emerald-700 dark:text-emerald-300"
            data-hint="read-back"
          >
            <CheckCircle2 aria-hidden />
            read-back seen
          </Badge>
        }
      />
      <TooltipContent className="max-w-xs">
        Heuristic from the call order: a later call on this path succeeded, and a successful read
        of the same path came first — the agent verified state instead of retrying blind. The
        grader's checks are authoritative.
      </TooltipContent>
    </Tooltip>
  )
}

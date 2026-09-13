import { Monitor, Moon, Sun } from 'lucide-react'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { THEMES, useTheme, type Theme } from '@/lib/theme'

const META: Record<Theme, { icon: typeof Sun; label: string }> = {
  light: { icon: Sun, label: 'Light' },
  dark: { icon: Moon, label: 'Dark' },
  system: { icon: Monitor, label: 'System' },
}

/** Segmented icon control: light · dark · system. Persisted per browser. */
export function ThemeSwitcher({ className }: { className?: string }) {
  const { theme, resolved, setTheme } = useTheme()
  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className={cn('flex items-center rounded-md border border-border bg-muted/30 p-0.5', className)}
    >
      {THEMES.map((t) => {
        const { icon: Icon, label } = META[t]
        const active = theme === t
        return (
          <Tooltip key={t}>
            <TooltipTrigger
              render={
                <button
                  type="button"
                  role="radio"
                  aria-checked={active}
                  aria-label={`${label} theme`}
                  onClick={() => setTheme(t)}
                  className={cn(
                    'inline-flex size-7 items-center justify-center rounded-[5px] text-muted-foreground transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/50',
                    active ? 'bg-background text-foreground shadow-sm' : 'hover:text-foreground',
                  )}
                >
                  <Icon className="size-3.5" aria-hidden />
                </button>
              }
            />
            <TooltipContent>
              {label}
              {t === 'system' ? ` (currently ${resolved})` : ''}
            </TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}

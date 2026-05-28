import { AppHeader } from './shared/AppHeader'

interface Props {
  /** Текст приходит с бэкенда (emergency_message / message) и показывается дословно. */
  emergencyText: string
  onRestart: () => void
}

/**
 * Превращает телефонные номера в backend-тексте в кликабельные tel:-ссылки.
 *
 * Правила:
 *  - UI ничего не добавляет: линкуются только номера, реально присутствующие
 *    в `res.emergency_message` / `res.message`.
 *  - Никаких литералов конкретных кризисных номеров (103/112/...) в коде —
 *    короткий код матчится только структурно как «отдельно стоящие 3 цифры»
 *    и только при наличии телефонной лексики рядом.
 *  - Защита от мед-чисел: 3-значный кандидат отвергается, если он примыкает
 *    к цифре/`.`/`,`/`:`/`/` или за ним следует единица измерения
 *    («мг», «мл», «г», «°», «%», «мм», «рт», «ч», «мин» и т. п.).
 *    Иначе «давление 120», «принял 250 мг», «через 120 минут» залинковались
 *    бы как телефоны.
 *  - Если уверенности нет — оставляем plain text. Лучше не залинковать
 *    реальный номер, чем сделать tel:-ссылку из дозировки.
 */

// Phone-context lexicon: linkify short codes only when one of these
// appears in the same clause as the digit candidate.
const PHONE_KEYWORD = /(?:звон|набер|вызов|вызыв|вызвать|скор|телефон|\bтел\b)/i

// Long Russian phone formats: +7..., 8-..., 8(...)...
const LONG_PHONE = /(\+7[\s\-()\d]{9,}|8[\s\-()][\s\-()\d]{8,})/g

// 3-digit candidate at word boundary, NOT followed by another digit.
// Lookahead is fine in modern browsers; lookbehind avoided for older Safari —
// preceding char is checked manually.
const SHORT_CANDIDATE = /\b(\d{3})(?!\d)/g

// Units that disqualify the candidate (medical / metric trailing context).
// Matched against the substring starting right after the 3-digit token.
const TRAILING_UNIT = /^\s*(?:мг|мл|мкг|г\b|°|%|мм|рт|ч\b|мин|сек|сс?м\b)/i

// Forbidden preceding chars — digit means we're inside a longer number,
// punctuation suggests continuation (e.g. "120/80", "37.5", "3,14", "12:30").
const FORBID_PREV = /[\d.,:/]/

function findClauseBounds(text: string, pos: number): [number, number] {
  // Sentence-ish bounds: split on terminators + line breaks.
  const TERMINATORS = /[.!?;\n\r]/
  let start = 0
  for (let i = pos - 1; i >= 0; i--) {
    if (TERMINATORS.test(text[i])) { start = i + 1; break }
  }
  let end = text.length
  for (let i = pos; i < text.length; i++) {
    if (TERMINATORS.test(text[i])) { end = i; break }
  }
  return [start, end]
}

function linkifyPhones(text: string): React.ReactNode[] {
  type Match = { start: number; end: number; raw: string; tel: string }
  const matches: Match[] = []
  let m: RegExpExecArray | null

  // Pass 1 — long phones.
  LONG_PHONE.lastIndex = 0
  while ((m = LONG_PHONE.exec(text)) !== null) {
    matches.push({
      start: m.index,
      end: m.index + m[0].length,
      raw: m[0],
      tel: m[0].replace(/[\s\-()]/g, ''),
    })
  }

  // Pass 2 — short codes, only with phone-keyword context and unit-guard.
  SHORT_CANDIDATE.lastIndex = 0
  while ((m = SHORT_CANDIDATE.exec(text)) !== null) {
    const start = m.index
    const end = start + 3
    // Inside another already-matched long phone? skip.
    if (matches.some((mm) => start >= mm.start && start < mm.end)) continue
    // Preceding char check (no lookbehind for compat).
    const prev = start > 0 ? text[start - 1] : ''
    if (prev && FORBID_PREV.test(prev)) continue
    // Trailing unit check (медицинские/метрические единицы).
    if (TRAILING_UNIT.test(text.slice(end))) continue
    // Same-clause phone-keyword check.
    const [cs, ce] = findClauseBounds(text, start)
    const clause = text.slice(cs, ce)
    if (!PHONE_KEYWORD.test(clause)) continue
    matches.push({ start, end, raw: m[1], tel: m[1] })
  }

  if (matches.length === 0) return [text]
  matches.sort((a, b) => a.start - b.start)

  const parts: React.ReactNode[] = []
  let cursor = 0
  let i = 0
  for (const mm of matches) {
    if (mm.start < cursor) continue // overlap guard
    if (mm.start > cursor) parts.push(text.slice(cursor, mm.start))
    parts.push(
      <a key={`tel-${i++}`} href={`tel:${mm.tel}`}>
        {mm.raw}
      </a>
    )
    cursor = mm.end
  }
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}

export function CrisisScreen({ emergencyText, onRestart }: Props) {
  return (
    <>
      <AppHeader />
      <main className="crisis" role="alert" aria-live="assertive">
        <div className="crisis-card">
          <div className="crisis-label">Внимание</div>
          <div className="crisis-text">{linkifyPhones(emergencyText)}</div>
          <div className="crisis-foot">
            Диалог приостановлен · обратитесь за очной помощью
          </div>
        </div>

        <button type="button" className="crisis-restart" onClick={onRestart}>
          Начать новый опрос
        </button>
      </main>
    </>
  )
}

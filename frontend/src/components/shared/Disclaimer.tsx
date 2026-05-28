/**
 * Canonical disclaimer string (verbatim, must match backend / CLAUDE.md §3 wording).
 * Do NOT abbreviate without owner approval — short forms go to strings-to-approve.md.
 */
export const DISCLAIMER_TEXT =
  'Информация носит справочный характер и не заменяет консультацию врача. Сервис не ставит диагнозы и не назначает лечение.'

interface Props {
  variant?: 'compact' | 'prominent'
}

export function Disclaimer({ variant = 'compact' }: Props) {
  if (variant === 'prominent') {
    return (
      <p className="disclaimer-prominent" role="note">
        {DISCLAIMER_TEXT}
      </p>
    )
  }
  return (
    <p className="disclaimer" role="note">
      <span className="disclaimer-ico" aria-hidden="true">i</span>
      <span>{DISCLAIMER_TEXT}</span>
    </p>
  )
}

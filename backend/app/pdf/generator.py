"""PDF renderer for the "маршрутный лист".

Reads a pre-assembled view model (see ``app.pdf.view_model``) and emits
either the routine routing PDF or the crisis PDF. No LLM calls. No
session-state reads beyond what the view model already carries.

Two top-level branches:
- routine (``is_crisis_only=False``): static "next step" card + KB-derived
  patient-described list + static prep/questions/urgent-care. No raw LLM
  reason, no per-specialist preparation, no causal explanations, no
  "Описание для врача" section.
- crisis (``is_crisis_only=True``, set by PR #8): crisis banner with the
  hotline + filtered conversation history + red-flags section. PR #11
  visual contract is preserved.

The old ``_build_symptoms_section`` and ``_build_specialists_section``
have been removed — they have no caller in the new layout. The old
``URGENCY_MAP`` (label + colour tuple) is also gone; urgency labelling
now lives in ``view_model.URGENCY_TIMEFRAME_MAP`` and is rendered as
text in the primary-route card.
"""

from html import escape

from weasyprint import HTML


class PDFGenerator:
    @staticmethod
    def generate(view_model: dict, session_id: str) -> bytes:
        html_content = PDFGenerator._build_html(view_model, session_id)
        return HTML(string=html_content).write_pdf()

    @staticmethod
    def _build_html(view_model: dict, session_id: str) -> str:
        is_crisis_only = bool(view_model.get("is_crisis_only", False))

        if is_crisis_only:
            body_content = PDFGenerator._build_crisis_body(view_model)
        else:
            body_content = PDFGenerator._build_routine_body(view_model)

        now = escape(view_model.get("generated_at", ""))
        sid_short = escape(view_model.get("session_id_short", session_id[:8]))
        disclaimer_text = escape(view_model.get("disclaimer", ""))

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<style>
    @page {{
        size: A4;
        margin: 2cm;
        @bottom-center {{
            content: "Страница " counter(page) " из " counter(pages);
            font-size: 8pt;
            color: #999;
        }}
    }}
    body {{
        font-family: sans-serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #1E293B;
    }}
    .header {{
        border-bottom: 3px solid #2563EB;
        padding-bottom: 12px;
        margin-bottom: 24px;
    }}
    .header h1 {{
        color: #2563EB;
        margin: 0 0 4px 0;
        font-size: 18pt;
    }}
    .header .subtitle {{
        color: #475569;
        font-size: 11pt;
        margin: 4px 0 6px 0;
    }}
    .header .meta {{
        color: #64748B;
        font-size: 10pt;
    }}
    h2 {{
        color: #1E40AF;
        font-size: 13pt;
        border-bottom: 1px solid #E2E8F0;
        padding-bottom: 6px;
        margin-top: 24px;
        margin-bottom: 12px;
    }}
    p {{
        margin: 4px 0 8px 0;
    }}
    ul, ol {{
        padding-left: 24px;
        margin: 4px 0 12px 0;
    }}
    li {{
        margin-bottom: 4px;
    }}
    .primary-route {{
        background: #EFF6FF;
        border: 1px solid #BFDBFE;
        border-radius: 8px;
        padding: 16px 18px;
        margin: 12px 0 20px 0;
        page-break-inside: avoid;
    }}
    .primary-route .specialty {{
        color: #1E40AF;
        font-size: 16pt;
        font-weight: 700;
        margin: 0 0 4px 0;
        display: block;
    }}
    .primary-route .urgency-label {{
        color: #1E293B;
        font-size: 11pt;
        font-weight: 600;
        margin: 0 0 10px 0;
    }}
    .primary-route .one-liner {{
        color: #334155;
        font-size: 10pt;
        margin: 0;
    }}
    .secondary-routes-text {{
        color: #334155;
        margin: 6px 0 12px 0;
    }}
    .urgent-care {{
        background: #FEF3C7;
        border-left: 4px solid #F59E0B;
        padding: 12px 16px;
        margin: 16px 0;
        border-radius: 0 8px 8px 0;
        page-break-inside: avoid;
    }}
    .urgent-care p {{
        margin: 0;
        color: #1E293B;
    }}
    .red-flags {{
        background: #FEE2E2;
        border: 2px solid #EF4444;
        border-radius: 8px;
        padding: 12px 16px;
        margin: 16px 0;
        page-break-inside: avoid;
    }}
    .red-flags h2 {{
        color: #DC2626;
        border-bottom: none;
        margin-top: 0;
        padding-bottom: 0;
    }}
    .crisis-banner {{
        background: #FECACA;
        border: 2px solid #B91C1C;
        border-radius: 8px;
        padding: 18px 20px;
        margin: 16px 0 24px 0;
        page-break-inside: avoid;
    }}
    .crisis-banner .crisis-title {{
        color: #991B1B;
        font-size: 14pt;
        font-weight: 700;
        margin: 0 0 10px 0;
        display: block;
    }}
    .crisis-banner p {{
        margin: 6px 0;
        color: #1E293B;
    }}
    .crisis-banner ul {{
        margin: 8px 0;
        padding-left: 24px;
    }}
    .crisis-banner li {{
        margin-bottom: 4px;
    }}
    .crisis-banner .crisis-phone {{
        font-weight: 700;
        color: #991B1B;
    }}
    .crisis-banner .crisis-reassurance {{
        font-style: italic;
        color: #475569;
        margin-top: 10px;
    }}
    .history-item {{
        margin-bottom: 6px;
        font-size: 10pt;
    }}
    .history-item .label {{
        font-weight: 600;
        color: #475569;
    }}
    .history-item .text {{
        color: #1E293B;
    }}
    .disclaimer {{
        margin-top: 24px;
        padding: 10px 14px;
        background: #F1F5F9;
        border: 1px solid #CBD5E1;
        border-radius: 8px;
        font-size: 9pt;
        color: #475569;
        page-break-inside: avoid;
    }}
    .footer {{
        margin-top: 16px;
        font-size: 8pt;
        color: #94A3B8;
        text-align: center;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>MedNavigator — маршрутный лист</h1>
    <div class="subtitle">Помогает подготовиться к визиту и выбрать следующий шаг</div>
    <div class="meta">Дата формирования: {now} · Сессия: {sid_short}</div>
</div>

{body_content}

<div class="disclaimer">{disclaimer_text}</div>

<div class="footer">
    MedNavigator — информационный сервис медицинской маршрутизации | mednavigator.ru
</div>

</body>
</html>"""

    # ------------------------------------------------------------------
    # Routine (non-crisis) body
    # ------------------------------------------------------------------

    @staticmethod
    def _build_routine_body(view_model: dict) -> str:
        sections: list[str] = []

        primary = PDFGenerator._build_primary_route(view_model)
        if primary:
            sections.append(primary)

        described = PDFGenerator._build_what_patient_described(view_model)
        if described:
            sections.append(described)

        secondary = PDFGenerator._build_secondary_routes(view_model)
        if secondary:
            sections.append(secondary)

        sections.append(PDFGenerator._build_preparation_checklist(view_model))
        sections.append(PDFGenerator._build_questions(view_model))
        sections.append(PDFGenerator._build_urgent_care(view_model))

        return "\n\n".join(sections)

    @staticmethod
    def _build_primary_route(view_model: dict) -> str:
        primary = view_model.get("primary_route")
        if not primary:
            return ""
        urgency_label = view_model.get("urgency", {}).get("label", "")
        return (
            '<h2>Рекомендуемый следующий шаг</h2>'
            '<div class="primary-route">'
            f'<span class="specialty">{escape(primary["specialty"])}</span>'
            f'<div class="urgency-label">{escape(urgency_label)}</div>'
            f'<p class="one-liner">{escape(primary["safe_one_liner"])}</p>'
            '</div>'
        )

    @staticmethod
    def _build_what_patient_described(view_model: dict) -> str:
        items = view_model.get("what_patient_described") or []
        if not items:
            return ""
        bullets = "".join(f"<li>{escape(item)}</li>" for item in items)
        return f"<h2>Что вы описали</h2><ul>{bullets}</ul>"

    @staticmethod
    def _build_secondary_routes(view_model: dict) -> str:
        secondary = view_model.get("secondary_routes") or []
        if not secondary:
            return ""
        names = [escape(s["specialty"]) for s in secondary if isinstance(s, dict) and s.get("specialty")]
        if not names:
            return ""
        if len(names) == 1:
            who = names[0]
        else:
            who = " или ".join(names)
        return (
            '<h2>Также может пригодиться</h2>'
            f'<p class="secondary-routes-text">'
            f'В зависимости от очной оценки врач может направить к другому специалисту, '
            f'например к {who}.'
            '</p>'
        )

    @staticmethod
    def _build_preparation_checklist(view_model: dict) -> str:
        items = view_model.get("preparation_checklist") or []
        bullets = "".join(f"<li>{escape(item)}</li>" for item in items)
        return f"<h2>Подготовка к визиту</h2><ul>{bullets}</ul>"

    @staticmethod
    def _build_questions(view_model: dict) -> str:
        items = view_model.get("questions_for_doctor") or []
        bullets = "".join(f"<li>{escape(item)}</li>" for item in items)
        return f"<h2>Возможные вопросы врачу</h2><ol>{bullets}</ol>"

    @staticmethod
    def _build_urgent_care(view_model: dict) -> str:
        block = view_model.get("urgent_care_block", "")
        return (
            '<h2>Когда нужно действовать срочнее</h2>'
            f'<div class="urgent-care"><p>{escape(block)}</p></div>'
        )

    # ------------------------------------------------------------------
    # Crisis body (PR #11 contract preserved)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_crisis_body(view_model: dict) -> str:
        sections: list[str] = []
        sections.append(PDFGenerator._build_crisis_banner())

        history = PDFGenerator._build_history_section(view_model)
        if history:
            sections.append(history)

        red_flags_section = PDFGenerator._build_red_flags_section(view_model)
        if red_flags_section:
            sections.append(red_flags_section)

        return "\n\n".join(sections)

    @staticmethod
    def _build_crisis_banner() -> str:
        return (
            '<div class="crisis-banner">'
            '<span class="crisis-title">Это срочное обращение, требующее немедленной помощи</span>'
            '<p>Пожалуйста, обратитесь за помощью прямо сейчас:</p>'
            '<ul>'
            '<li>Телефон доверия: <span class="crisis-phone">8-800-2000-122</span> (бесплатно, круглосуточно)</li>'
            '<li>Скорая помощь: <span class="crisis-phone">103 или 112</span></li>'
            '</ul>'
            '<p class="crisis-reassurance">Вы не одиноки, и помощь доступна.</p>'
            '</div>'
        )

    @staticmethod
    def _build_history_section(view_model: dict) -> str:
        """Used only in the crisis branch. Iterates already-filtered history."""
        history_lines = view_model.get("history_lines", [])
        if not history_lines:
            return ""

        qa_pairs = []
        for line in history_lines:
            if line.startswith("["):
                continue
            if line.startswith("Пациент: "):
                qa_pairs.append(("Пациент", line[len("Пациент: "):]))
            elif line.startswith("Ассистент: "):
                qa_pairs.append(("Ассистент", line[len("Ассистент: "):]))

        if not qa_pairs:
            return ""

        items = "".join(
            f'<div class="history-item">'
            f'<span class="label">{escape(label)}:</span> '
            f'<span class="text">{escape(text)}</span>'
            f'</div>'
            for label, text in qa_pairs
        )
        return f"<h2>Ход опроса</h2>{items}"

    @staticmethod
    def _build_red_flags_section(view_model: dict) -> str:
        """Used only in the crisis branch. View model populates red_flags
        only when is_crisis_only is True, so this is effectively a no-op
        for routine PDFs."""
        red_flags = view_model.get("red_flags", [])
        if not red_flags:
            return ""
        items = "".join(f"<li>{escape(str(rf))}</li>" for rf in red_flags)
        return (
            '<div class="red-flags">'
            '<h2>Внимание: тревожные признаки</h2>'
            f'<ul>{items}</ul>'
            '<p><strong>При наличии этих симптомов рекомендуется немедленно обратиться за медицинской помощью.</strong></p>'
            '</div>'
        )

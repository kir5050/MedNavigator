from datetime import datetime, timezone
from html import escape

from weasyprint import HTML


class PDFGenerator:
    URGENCY_MAP = {
        "low": ("Плановый визит", "#10B981", "#D1FAE5"),
        "medium": ("Рекомендуется записаться в ближайшие 1-2 дня", "#F59E0B", "#FEF3C7"),
        "high": ("Рекомендуется обратиться к врачу сегодня", "#EF4444", "#FEE2E2"),
        "emergency": ("ТРЕБУЕТСЯ ЭКСТРЕННАЯ ПОМОЩЬ — позвоните 103", "#DC2626", "#FEE2E2"),
    }

    @staticmethod
    def generate(data: dict, session_id: str) -> bytes:
        html_content = PDFGenerator._build_html(data, session_id)
        return HTML(string=html_content).write_pdf()

    @staticmethod
    def _build_html(data: dict, session_id: str) -> str:
        now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        urgency = data.get("urgency", "medium")
        urgency_text, urgency_color, urgency_bg = PDFGenerator.URGENCY_MAP.get(
            urgency, PDFGenerator.URGENCY_MAP["medium"]
        )

        # Crisis-only sessions (suicide/self-harm trigger fired) get a different
        # urgency block and skip routine intake-derived sections. A routine
        # "schedule a visit in 1-2 days" banner next to a hotline number is
        # cognitively dissonant and potentially harmful for a patient in crisis.
        is_crisis_only = bool(data.get("is_crisis_only", False))

        if is_crisis_only:
            urgency_block = PDFGenerator._build_crisis_banner()
        else:
            urgency_block = (
                f'<div class="urgency-box">'
                f'<strong>{escape(urgency_text)}</strong>'
                f'</div>'
            )

        sections = []

        # --- 1. Symptoms / complaints (skipped for crisis-only — empty intake) ---
        if not is_crisis_only:
            symptoms_section = PDFGenerator._build_symptoms_section(data)
            if symptoms_section:
                sections.append(symptoms_section)

        # --- 2. User's answers from conversation (kept for context) ---
        history_section = PDFGenerator._build_history_section(data)
        if history_section:
            sections.append(history_section)

        # --- 3. Uploaded document analyses (kept; may carry pre-crisis context) ---
        docs_section = PDFGenerator._build_documents_section(data)
        if docs_section:
            sections.append(docs_section)

        # --- 4. Specialists (skipped for crisis-only — no routing was run) ---
        if not is_crisis_only:
            specialists_section = PDFGenerator._build_specialists_section(data)
            if specialists_section:
                sections.append(specialists_section)

        # --- 5. Routing explanation (skipped for crisis-only).
        # The existing `if explanation` gate already swallows the empty default
        # for crisis sessions; the not-is_crisis_only check is defence in depth
        # in case future LLM logic starts populating routing_explanation for
        # crisis sessions too.
        explanation = data.get("routing_explanation", "")
        if explanation and not is_crisis_only:
            sections.append(
                f'<h2>Пояснение</h2><p>{escape(explanation)}</p>'
            )

        # --- 6. Questions for doctor (skipped for crisis-only) ---
        questions = data.get("questions_for_doctor", [])
        if questions and not is_crisis_only:
            items = "".join(f"<li>{escape(str(q))}</li>" for q in questions)
            sections.append(f"<h2>Вопросы для врача</h2><ol>{items}</ol>")

        # --- 7. What to bring (skipped for crisis-only) ---
        what_to_bring = data.get("what_to_bring", [])
        if what_to_bring and not is_crisis_only:
            items = "".join(f"<li>{escape(str(item))}</li>" for item in what_to_bring)
            sections.append(f"<h2>Что взять с собой</h2><ul>{items}</ul>")

        # --- 8. Red flags (kept; for crisis sessions this is the hotline section) ---
        red_flags_section = PDFGenerator._build_red_flags_section(data)
        if red_flags_section:
            sections.append(red_flags_section)

        body_content = "\n\n".join(sections)

        html_content = f"""<!DOCTYPE html>
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
        color: #64748B;
        font-size: 10pt;
    }}
    .urgency-box {{
        background: {urgency_bg};
        border-left: 5px solid {urgency_color};
        padding: 12px 16px;
        margin: 16px 0 24px 0;
        border-radius: 0 8px 8px 0;
    }}
    .urgency-box strong {{
        color: {urgency_color};
        font-size: 13pt;
    }}
    h2 {{
        color: #1E40AF;
        font-size: 13pt;
        border-bottom: 1px solid #E2E8F0;
        padding-bottom: 6px;
        margin-top: 24px;
        margin-bottom: 12px;
    }}
    h3 {{
        color: #1E3A5F;
        font-size: 11pt;
        margin: 12px 0 4px 0;
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
    .specialist-card {{
        background: #F8FAFC;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 12px;
        page-break-inside: avoid;
    }}
    .specialist-card h3 {{
        color: #2563EB;
        margin: 0 0 4px 0;
    }}
    .specialist-card .reason {{
        color: #475569;
        font-size: 10pt;
        margin-bottom: 8px;
    }}
    .specialist-card .prep-title {{
        font-weight: 600;
        font-size: 10pt;
        margin-bottom: 4px;
    }}
    .specialist-card ul {{
        margin: 0;
        font-size: 10pt;
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
    .symptom-tag {{
        display: inline-block;
        background: #EFF6FF;
        border: 1px solid #BFDBFE;
        border-radius: 6px;
        padding: 2px 10px;
        margin: 2px 4px 2px 0;
        font-size: 10pt;
        color: #1E40AF;
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
    .docs-disclaimer {{
        font-size: 9pt;
        color: #64748B;
        font-style: italic;
        margin: 4px 0 12px 0;
    }}
    .disclaimer {{
        margin-top: 32px;
        padding: 12px 16px;
        background: #F1F5F9;
        border: 1px solid #CBD5E1;
        border-radius: 8px;
        font-size: 9pt;
        color: #64748B;
        line-height: 1.5;
    }}
    .disclaimer strong {{
        color: #475569;
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
    <h1>MedNavigator — Информационная выписка</h1>
    <div class="subtitle">Дата формирования: {now} | Сессия: {session_id[:8]}</div>
</div>

{urgency_block}

{body_content}

<div class="disclaimer">
    <strong>Важно:</strong> Данный документ носит исключительно информационный и справочный характер.
    Он НЕ является медицинским заключением, диагнозом или назначением лечения.
    Информация подготовлена автоматически на основе описанных вами симптомов и предназначена
    для подготовки к визиту к врачу. Обязательно проконсультируйтесь с квалифицированным
    специалистом для постановки диагноза и назначения лечения.
</div>

<div class="footer">
    MedNavigator — информационный сервис медицинской маршрутизации | mednavigator.ru
</div>

</body>
</html>"""

        return html_content

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
    def _build_symptoms_section(data: dict) -> str:
        complaints_simple = data.get("complaints_simple", "")
        complaints_medical = data.get("complaints_medical", "")
        symptoms = data.get("symptoms_list", [])

        if not complaints_simple and not symptoms:
            return ""

        parts = ['<h2>Жалобы и симптомы</h2>']

        if complaints_simple:
            parts.append(f'<p>{escape(complaints_simple)}</p>')

        if symptoms:
            tags = ""
            for s in symptoms:
                if isinstance(s, dict):
                    name = s.get("name", "")
                    area = s.get("area", "")
                    duration = s.get("duration", "")
                    detail = name
                    extras = []
                    if area:
                        extras.append(area)
                    if duration:
                        extras.append(duration)
                    if extras:
                        detail += f" ({', '.join(extras)})"
                else:
                    detail = str(s)
                if detail:
                    tags += f'<span class="symptom-tag">{escape(detail)}</span> '
            if tags:
                parts.append(f'<p>{tags}</p>')

        if complaints_medical:
            parts.append(
                f'<h2>Описание для врача</h2>'
                f'<p><em>{escape(complaints_medical)}</em></p>'
            )

        return "\n".join(parts)

    @staticmethod
    def _build_history_section(data: dict) -> str:
        history_lines = data.get("history_lines", [])
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

        items = ""
        for label, text in qa_pairs:
            items += (
                f'<div class="history-item">'
                f'<span class="label">{escape(label)}:</span> '
                f'<span class="text">{escape(text)}</span>'
                f'</div>'
            )

        return f"<h2>Ход опроса</h2>{items}"

    @staticmethod
    def _build_documents_section(data: dict) -> str:
        uploaded_files = data.get("uploaded_files", [])
        if not uploaded_files:
            return ""

        # Always-on disclaimer when any document was uploaded — independent of
        # whether the analysis text survived the output_safety filter.
        # Document content is referenced, never reproduced verbatim.
        parts = [
            '<h2>Загруженные документы</h2>',
            '<p class="docs-disclaimer">'
            'Данные из загруженных вами документов учтены при подготовке этого листа. '
            'Содержимое документов остаётся при вас; врач увидит оригиналы на приёме.'
            '</p>',
        ]
        for f in uploaded_files:
            if not isinstance(f, dict):
                continue
            filename = escape(f.get("filename", "файл"))
            analysis = (f.get("analysis") or "").strip()
            if analysis:
                parts.append(
                    f'<div class="specialist-card">'
                    f'<h3>{filename}</h3>'
                    f'<p>{escape(analysis)}</p>'
                    f'</div>'
                )
            else:
                # Filename only — preserves the fact of upload without leaking
                # unfiltered / empty analysis text.
                parts.append(
                    f'<div class="specialist-card"><h3>{filename}</h3></div>'
                )
        return "\n".join(parts)

    @staticmethod
    def _build_specialists_section(data: dict) -> str:
        specialists = data.get("specialists", [])
        if not specialists:
            return ""

        cards = ""
        for i, spec in enumerate(specialists, 1):
            if not isinstance(spec, dict):
                continue
            name = escape(spec.get("specialty", "Специалист"))
            reason = spec.get("reason", "")
            prep = spec.get("preparation", [])

            card = f'<div class="specialist-card"><h3>{i}. {name}</h3>'
            if reason:
                card += f'<p class="reason">{escape(reason)}</p>'
            if prep:
                card += '<p class="prep-title">Подготовка к визиту:</p><ul>'
                for item in prep:
                    card += f"<li>{escape(str(item))}</li>"
                card += "</ul>"
            card += "</div>"
            cards += card

        return f"<h2>Рекомендуемые специалисты</h2>{cards}"

    @staticmethod
    def _build_red_flags_section(data: dict) -> str:
        red_flags = data.get("red_flags", [])
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

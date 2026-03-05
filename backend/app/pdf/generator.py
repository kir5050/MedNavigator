import io
from datetime import datetime, timezone

from weasyprint import HTML


class PDFGenerator:
    @staticmethod
    def generate(data: dict, session_id: str) -> bytes:
        now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

        complaints_medical = data.get("complaints_medical", "")
        complaints_simple = data.get("complaints_simple", "")
        timeline = data.get("timeline", "")
        questions = data.get("questions_for_doctor", [])
        what_to_bring = data.get("what_to_bring", [])
        specialists = data.get("specialists", [])
        urgency = data.get("urgency", "medium")
        preparation = data.get("preparation", [])

        urgency_map = {
            "low": "Плановый визит",
            "medium": "В ближайшие 1-2 дня",
            "high": "Рекомендуется обратиться сегодня",
            "emergency": "ЭКСТРЕННАЯ ПОМОЩЬ",
        }
        urgency_text = urgency_map.get(urgency, urgency)

        specialists_html = ""
        for i, spec in enumerate(specialists, 1):
            name = spec.get("specialty", "") if isinstance(spec, dict) else str(spec)
            reason = spec.get("reason", "") if isinstance(spec, dict) else ""
            prep = spec.get("preparation", []) if isinstance(spec, dict) else []
            specialists_html += f"<h3>{i}. {name}</h3>"
            if reason:
                specialists_html += f"<p>{reason}</p>"
            if prep:
                specialists_html += "<p><strong>Подготовка к визиту:</strong></p><ul>"
                for item in prep:
                    specialists_html += f"<li>{item}</li>"
                specialists_html += "</ul>"

        questions_html = ""
        if questions:
            questions_html = "<h2>Вопросы для врача</h2><ol>"
            for q in questions:
                questions_html += f"<li>{q}</li>"
            questions_html += "</ol>"

        bring_html = ""
        if what_to_bring:
            bring_html = "<h2>Что взять с собой</h2><ul>"
            for item in what_to_bring:
                bring_html += f"<li>{item}</li>"
            bring_html += "</ul>"

        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<style>
    @page {{
        size: A4;
        margin: 2cm;
    }}
    body {{
        font-family: sans-serif;
        font-size: 12pt;
        line-height: 1.5;
        color: #333;
    }}
    .header {{
        border-bottom: 2px solid #2563eb;
        padding-bottom: 10px;
        margin-bottom: 20px;
    }}
    .header h1 {{
        color: #2563eb;
        margin: 0;
        font-size: 18pt;
    }}
    .header .subtitle {{
        color: #666;
        font-size: 10pt;
    }}
    .urgency {{
        background: #fef3c7;
        border-left: 4px solid #f59e0b;
        padding: 10px 15px;
        margin: 15px 0;
    }}
    .urgency.high {{
        background: #fee2e2;
        border-left-color: #ef4444;
    }}
    h2 {{
        color: #1e40af;
        font-size: 14pt;
        border-bottom: 1px solid #ddd;
        padding-bottom: 5px;
    }}
    h3 {{
        color: #1e3a5f;
        font-size: 12pt;
        margin-bottom: 5px;
    }}
    .disclaimer {{
        margin-top: 30px;
        padding: 10px;
        background: #f3f4f6;
        border: 1px solid #d1d5db;
        font-size: 9pt;
        color: #666;
    }}
    .footer {{
        margin-top: 20px;
        font-size: 8pt;
        color: #999;
        text-align: center;
    }}
    ul, ol {{
        padding-left: 20px;
    }}
    li {{
        margin-bottom: 5px;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>MedNavigator — Выписка для визита к врачу</h1>
    <div class="subtitle">Дата: {now} | Сессия: {session_id[:8]}</div>
</div>

<div class="urgency {"high" if urgency in ("high", "emergency") else ""}">
    <strong>Рекомендуемая срочность:</strong> {urgency_text}
</div>

<h2>Ваши жалобы</h2>
<p>{complaints_simple}</p>

{"<h2>Описание для врача</h2><p>" + complaints_medical + "</p>" if complaints_medical else ""}

{"<h2>Хронология</h2><p>" + timeline + "</p>" if timeline else ""}

<h2>Рекомендуемые специалисты</h2>
{specialists_html if specialists_html else "<p>Обратитесь к терапевту для первичного осмотра.</p>"}

{questions_html}

{bring_html}

<div class="disclaimer">
    <strong>Важно:</strong> Данный документ носит исключительно информационный и справочный характер.
    Он НЕ является медицинским заключением, диагнозом или назначением лечения.
    Обязательно проконсультируйтесь с квалифицированным врачом. Информация подготовлена
    автоматически на основе описанных вами симптомов и не заменяет очный осмотр специалиста.
</div>

<div class="footer">
    MedNavigator — информационный сервис медицинской маршрутизации
</div>

</body>
</html>"""

        pdf_bytes = HTML(string=html_content).write_pdf()
        return pdf_bytes

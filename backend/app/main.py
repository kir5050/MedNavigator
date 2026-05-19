import json
import logging
import traceback as tb_module

import base64
import httpx

from fastapi import Depends, FastAPI, File, HTTPException, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func, select, text

from app.config import settings
from app.llm import (
    LLMManager,
    LLMProvider,
    OpenRouterProvider,
)
from app.medical_kb import MedicalKB
from app.models.database import (
    Feedback,
    Message,
    Session,
    TriageResult,
    get_engine,
    get_session_maker,
)
from app.pdf import PDFGenerator
from app.pdf.view_model import build_view_model
from app.services.output_safety import FALLBACKS, safe_generate_text
from app.services.triage_engine import TriageEngine

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger(__name__)

DISCLAIMER = "Информация носит справочный характер и не заменяет консультацию врача."
MAX_SESSION_MESSAGES = 30

# Last-resort fallback for crisis-locked sessions when neither session state
# nor the suicidal entry in red_flags.yaml is available. Kept as an explicit
# named module-level constant so reviewers can spot drift from the YAML
# message. Must remain an exact copy of red_flags.yaml > suicidal.message.
CRISIS_FALLBACK_MESSAGE = (
    "Пожалуйста, обратитесь за помощью прямо сейчас. "
    "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
    "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна."
)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="MedNavigator API", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda request, exc: JSONResponse(
        status_code=429,
        content={"detail": "Слишком много запросов. Пожалуйста, подождите и попробуйте снова."},
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Telegram error alerts ---

async def send_telegram_alert(text: str):
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception:
        logger.warning("Failed to send Telegram alert")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace = tb_module.format_exc()
    logger.error("Unhandled error: %s\n%s", exc, trace)
    alert = (
        f"<b>MedNavigator ERROR</b>\n"
        f"<b>URL:</b> {request.method} {request.url.path}\n"
        f"<b>Error:</b> {type(exc).__name__}: {str(exc)[:200]}\n"
        f"<pre>{trace[-500:]}</pre>"
    )
    await send_telegram_alert(alert)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Global state
db_session_maker = None
triage_engine = None


@app.get("/health")
async def health():
    return {
        "db_ready": db_session_maker is not None,
        "triage_ready": triage_engine is not None,
    }


def build_providers() -> list[LLMProvider]:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set in .env")
    return [OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_model)]


@app.on_event("startup")
async def startup():
    global db_session_maker, triage_engine

    try:
        engine = await get_engine(settings.database_url)
        db_session_maker = get_session_maker(engine)

        # Migrate: add pdf_cache columns for existing databases
        async with engine.begin() as conn:
            try:
                await conn.execute(text("ALTER TABLE triage_results ADD COLUMN pdf_cache BLOB"))
            except Exception:
                pass  # Column already exists
            try:
                await conn.execute(text("ALTER TABLE triage_results ADD COLUMN pdf_generated_at DATETIME"))
            except Exception:
                pass  # Column already exists

            # output_safety v1: always wipe pdf_cache on startup to avoid
            # serving stale unsafe cached PDFs. This wipes safe cached
            # PDFs too, which is acceptable in MVP (pre-revenue, no real
            # user impact — next PDF request regenerates and re-caches).
            # NOT an idempotent one-shot migration: runs on every backend
            # restart by design, until cache versioning is introduced.
            await conn.execute(text(
                "UPDATE triage_results SET pdf_cache = NULL, pdf_generated_at = NULL"
            ))

        logger.info("Database initialized: %s", settings.database_url)

        providers = build_providers()
        llm = LLMManager(providers, settings.cache_dir)
        kb = MedicalKB()
        triage_engine = TriageEngine(llm, kb)

        provider_names = [p.name for p in providers]
        logger.info("Started with providers: %s", provider_names)
        await send_telegram_alert(
            f"<b>MedNavigator STARTED</b>\nProviders: {', '.join(provider_names)}"
        )
    except Exception:
        trace = tb_module.format_exc()
        logger.error("STARTUP FAILED:\n%s", trace)
        await send_telegram_alert(
            f"<b>MedNavigator STARTUP FAILED</b>\n<pre>{trace[-500:]}</pre>"
        )
        raise


# --- Request/Response models ---

class MessageRequest(BaseModel):
    text: str = Field(..., max_length=5000)
    file_description: str | None = None

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Текст сообщения не может быть пустым")
        return v

class FeedbackRequest(BaseModel):
    session_id: str
    rating: int
    comment: str | None = Field(default=None, max_length=2000)
    was_helpful: bool | None = None


# --- Endpoints ---

@app.post("/api/v1/session/start", status_code=201)
@limiter.limit("10/minute")
async def start_session(request: Request):
    try:
        if db_session_maker is None:
            logger.error("db_session_maker is None — startup may have failed")
            raise HTTPException(500, "Database not initialized")

        async with db_session_maker() as db:
            session = Session()
            db.add(session)
            await db.commit()
            await db.refresh(session)

        return {
            "session_id": session.id,
            "message": "Здравствуйте! Я помогу вам разобраться, к какому специалисту обратиться. Расскажите, что вас беспокоит?",
            "disclaimer": DISCLAIMER,
        }
    except HTTPException:
        raise
    except Exception:
        logger.error("start_session failed:\n%s", tb_module.format_exc())
        raise


@app.post("/api/v1/session/{session_id}/message")
@limiter.limit("20/minute")
async def send_message(request: Request, session_id: str, req: MessageRequest):
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status == "expired":
            raise HTTPException(410, "Session expired")

        state = json.loads(session.state_json)

        # Persistent crisis lock: once a suicide/self-harm trigger has fired,
        # every subsequent message in this session returns the same crisis
        # message instead of resuming intake. The lock is checked BEFORE the
        # message-count limit because the crisis loop doesn't call the LLM
        # (no resource the limit exists to cap), and a 30-message 409 in a
        # crisis context would be both technically misleading and harmful UX.
        if state.get("crisis_locked"):
            locked_message = (
                state.get("crisis_message")
                or triage_engine.kb.red_flags.get("suicidal", {}).get("message")
                or CRISIS_FALLBACK_MESSAGE
            )
            user_msg = Message(session_id=session_id, role="user", text=req.text)
            db.add(user_msg)
            assistant_msg = Message(
                session_id=session_id, role="assistant", text=locked_message,
            )
            db.add(assistant_msg)
            await db.commit()
            return {
                "message": locked_message,
                "session_status": "emergency",
                "disclaimer": DISCLAIMER,
                "extracted_symptoms": [],
                "is_emergency": True,
                "emergency_message": locked_message,
            }

        if session.message_count >= MAX_SESSION_MESSAGES:
            raise HTTPException(
                status_code=409,
                detail="Достигнут лимит сообщений в сессии (30). Пожалуйста, нажмите «Получить рекомендацию» для завершения консультации."
            )

        # Save user message
        user_msg = Message(session_id=session_id, role="user", text=req.text)
        db.add(user_msg)

        # Build history
        history_lines = state.get("history_lines", [])
        history_lines.append(f"Пациент: {req.text}")
        history = "\n".join(history_lines)
        state["history"] = history

        # Process
        result = await triage_engine.process_message(req.text, state)

        # Update state
        state["symptoms"] = result.get("symptoms", state.get("symptoms", []))
        state["clarification_count"] = result.get(
            "clarification_count", state.get("clarification_count", 0)
        )
        if result.get("triage"):
            state["triage"] = result["triage"]
        if result.get("routing"):
            state["routing"] = result["routing"]

        if result.get("is_crisis"):
            state["crisis_locked"] = True
            state["crisis_message"] = result["response"]

        history_lines.append(f"Ассистент: {result['response']}")
        state["history_lines"] = history_lines

        session.state_json = json.dumps(state, ensure_ascii=False)
        session.status = result["status"]
        session.message_count += 1

        # Save assistant message
        assistant_msg = Message(
            session_id=session_id,
            role="assistant",
            text=result["response"],
            extracted_symptoms=result.get("symptoms"),
        )
        db.add(assistant_msg)

        await db.commit()

    return {
        "message": result["response"],
        "session_status": result["status"],
        "disclaimer": DISCLAIMER,
        "extracted_symptoms": [
            s.get("name", s) if isinstance(s, dict) else s
            for s in result.get("symptoms", [])
        ],
        "is_emergency": result.get("is_emergency", False),
        "emergency_message": result["response"] if result.get("is_emergency") else None,
    }


@app.post("/api/v1/session/{session_id}/triage")
@limiter.limit("5/minute")
async def run_triage(request: Request, session_id: str):
    """User explicitly requests triage result."""
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status in ("completed", "emergency"):
            raise HTTPException(409, "Session already completed")

        state = json.loads(session.state_json)

        # Run triage
        result = await triage_engine.run_triage(state)

        # Save to state — include specialists with preparation in routing
        state["triage"] = result["triage"]
        routing = result["routing"]
        routing["specialists"] = result.get("specialists", routing.get("specialists", []))
        state["routing"] = routing
        session.state_json = json.dumps(state, ensure_ascii=False)
        session.status = "completed"

        # Save triage result to DB
        triage_record = TriageResult(
            session_id=session_id,
            urgency=result["triage"].get("urgency", "medium"),
            specialists=result.get("specialists", []),
            symptoms_summary=result["triage"].get("summary", ""),
        )
        db.add(triage_record)
        await db.commit()

    specialists = result.get("specialists", [])

    return {
        "urgency": result["triage"].get("urgency", "medium"),
        "specialists": specialists,
        "symptoms_summary": result["triage"].get("summary", ""),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/v1/session/{session_id}/upload")
@limiter.limit("5/minute")
async def upload_file(request: Request, session_id: str, file: UploadFile = File(...)):
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status == "expired":
            raise HTTPException(410, "Session expired")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10MB)")

    filename = file.filename or "file"
    content_type = file.content_type or ""

    analysis = ""

    file_analysis_system = """Ты — помощник для извлечения контекста из загруженных медицинских документов. Не ставишь диагноз, не интерпретируешь документ как врач и не даёшь медицинских советов.

Сформируй краткое нейтральное описание документа для контекста маршрутизации. Текст должен быть безопасен для пользовательского интерфейса и PDF.

Извлекай только явно указанные факты:
- тип документа: анализы, снимок, заключение, рецепт или другое;
- какие показатели, жалобы, разделы или результаты упомянуты;
- к какой общей области относится документ: анализы, обследование, врачебное заключение, назначения врача или другое.

НЕ копируй и НЕ называй:
- конкретные диагнозы;
- коды МКБ;
- названия лекарств, БАДов или добавок;
- дозировки;
- схемы лечения;
- рекомендации из документа дословно.

Если документ содержит такие сведения, опиши сам факт нейтрально:
«документ содержит ранее поставленное врачом заключение»,
«документ содержит назначения от врача»,
«документ содержит данные анализов»,
«документ содержит результаты обследования».

Не делай выводов о причинах симптомов и не добавляй собственных рекомендаций.

Ответь кратко, списком фактов.""".strip()

    # Analyze images via LLM vision
    if content_type.startswith("image/"):
        b64 = base64.b64encode(content).decode()
        images = [{"media_type": content_type, "data": b64}]
        try:
            llm = triage_engine.llm

            async def _call_image(sys_arg, *, temperature, use_cache):
                return await llm.generate(
                    "Извлеки медицинские данные из этого изображения.",
                    sys_arg, temperature=temperature, use_cache=use_cache,
                    images=images,
                )

            analysis = await safe_generate_text(
                _call_image, file_analysis_system,
                channel="file_analysis", field_name="image",
                fallback=FALLBACKS.file_analysis,
            )
        except Exception as e:
            logger.warning("Image analysis failed: %s", e)

    # Analyze PDFs — extract text and send to LLM
    elif content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=content, filetype="pdf")
            pdf_text = ""
            for page in doc:
                pdf_text += page.get_text()
            doc.close()

            if pdf_text.strip():
                llm = triage_engine.llm
                pdf_prompt = f"Извлеки медицинские данные из этого документа:\n\n{pdf_text[:4000]}"

                async def _call_pdf(sys_arg, *, temperature, use_cache):
                    return await llm.generate(
                        pdf_prompt, sys_arg,
                        temperature=temperature, use_cache=use_cache,
                    )

                analysis = await safe_generate_text(
                    _call_pdf, file_analysis_system,
                    channel="file_analysis", field_name="pdf",
                    fallback=FALLBACKS.file_analysis,
                )
            else:
                analysis = "PDF без текста — возможно отсканированный документ."
        except Exception as e:
            logger.warning("PDF analysis failed: %s", e)

    # Save to session state
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        state = json.loads(session.state_json)

        # Add analysis to history
        history_lines = state.get("history_lines", [])
        history_lines.append(f"[Пациент загрузил файл: {filename}]")
        if analysis:
            history_lines.append(f"[Анализ документа: {analysis}]")
        state["history_lines"] = history_lines
        state["history"] = "\n".join(history_lines)

        files = state.get("uploaded_files", [])
        files.append({
            "filename": filename,
            "type": content_type,
            "analysis": analysis,
        })
        state["uploaded_files"] = files
        session.state_json = json.dumps(state, ensure_ascii=False)
        await db.commit()

    return {
        "status": "ok",
        "filename": filename,
        "analysis": analysis,
    }


@app.get("/api/v1/session/{session_id}/result")
async def get_result(session_id: str):
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status not in ("completed", "emergency"):
            raise HTTPException(409, "Session not yet completed")

        state = json.loads(session.state_json)

    specialists = state.get("routing", {}).get("specialists", [])
    triage = state.get("triage", {})

    return {
        "urgency": triage.get("urgency", "medium"),
        "specialists": specialists,
        "symptoms_summary": triage.get("summary", ""),
        "preparation": "",
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/v1/session/{session_id}/pdf")
@limiter.limit("5/minute")
async def download_pdf(request: Request, session_id: str):
    from datetime import datetime as dt

    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status not in ("completed", "emergency"):
            raise HTTPException(409, "Session not yet completed")

        state = json.loads(session.state_json)

        # Check for cached PDF
        result = await db.execute(
            select(TriageResult).where(TriageResult.session_id == session_id)
        )
        triage_record = result.scalar_one_or_none()

        if triage_record and triage_record.pdf_cache:
            return Response(
                content=triage_record.pdf_cache,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=mednavigator_{session_id[:8]}.pdf"},
            )

    # PDF redesign (feat/pdf-redesign): no LLM calls happen on the PDF
    # download path. The view model builder is a pure function over
    # session state + curated static blocks + KB-normalised symptom
    # labels. All LLM-derived fields (complaints_*, routing.explanation,
    # specialists[].reason/preparation, questions_for_doctor, what_to_bring,
    # uploaded_files[].analysis) are intentionally NOT consumed by the
    # renderer — they remain in state for other surfaces (ResultScreen),
    # but the PDF is now a presentation layer over routing-time output
    # plus static content.
    view_model = build_view_model(state, session_id, triage_engine.kb)
    pdf_bytes = PDFGenerator.generate(view_model, session_id)

    # Cache the generated PDF
    if triage_record:
        async with db_session_maker() as db:
            result = await db.execute(
                select(TriageResult).where(TriageResult.session_id == session_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.pdf_cache = pdf_bytes
                record.pdf_generated_at = dt.utcnow()
                await db.commit()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=mednavigator_{session_id[:8]}.pdf"},
    )


@app.post("/api/v1/feedback", status_code=201)
async def submit_feedback(req: FeedbackRequest):
    async with db_session_maker() as db:
        session = await db.get(Session, req.session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        feedback = Feedback(
            session_id=req.session_id,
            rating=req.rating,
            comment=req.comment,
            was_helpful=req.was_helpful,
        )
        db.add(feedback)
        await db.commit()

    return {"status": "ok"}


async def verify_admin(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "")
    if token != settings.admin_token:
        raise HTTPException(401, "Unauthorized")


@app.get("/api/v1/analytics/dashboard", dependencies=[Depends(verify_admin)])
async def get_dashboard():
    async with db_session_maker() as db:
        total = (await db.execute(select(func.count(Session.id)))).scalar() or 0
        completed = (
            await db.execute(
                select(func.count(Session.id)).where(Session.status == "completed")
            )
        ).scalar() or 0
        emergency_count = (
            await db.execute(
                select(func.count(Session.id)).where(Session.status == "emergency")
            )
        ).scalar() or 0
        avg_rating = (
            await db.execute(select(func.avg(Feedback.rating)))
        ).scalar()

    return {
        "total_sessions": total,
        "completed_sessions": completed,
        "avg_rating": round(avg_rating, 2) if avg_rating else None,
        "top_specialties": [],
        "emergency_count": emergency_count,
        "period_days": 30,
    }

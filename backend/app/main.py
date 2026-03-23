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
from sqlalchemy import func, select

from app.config import settings
from app.llm import (
    GigaChatProvider,
    LLMManager,
    LLMProvider,
    OpenRouterProvider,
    YandexGPTProvider,
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
from app.services.triage_engine import TriageEngine

logging.basicConfig(level=getattr(logging, settings.log_level.upper()))
logger = logging.getLogger(__name__)

DISCLAIMER = "Информация носит справочный характер и не заменяет консультацию врача."

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
    providers: list[LLMProvider] = []
    primary = settings.llm_primary_provider

    def add_openrouter():
        if settings.openrouter_api_key:
            providers.append(
                OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_model)
            )

    def add_yandex():
        if settings.yandex_api_key and settings.yandex_folder_id:
            providers.append(
                YandexGPTProvider(
                    settings.yandex_api_key, settings.yandex_folder_id, settings.yandex_model
                )
            )

    def add_gigachat():
        if settings.gigachat_client_id and settings.gigachat_client_secret:
            providers.append(
                GigaChatProvider(settings.gigachat_client_id, settings.gigachat_client_secret)
            )

    # Primary first, then others as fallback
    order = {"openrouter": [add_openrouter, add_yandex, add_gigachat],
             "yandexgpt": [add_yandex, add_gigachat, add_openrouter],
             "gigachat": [add_gigachat, add_yandex, add_openrouter]}

    for fn in order.get(primary, [add_openrouter, add_yandex, add_gigachat]):
        fn()

    if not providers:
        raise RuntimeError("No LLM providers configured. Set API keys in .env")

    return providers


@app.on_event("startup")
async def startup():
    global db_session_maker, triage_engine

    try:
        engine = await get_engine(settings.database_url)
        db_session_maker = get_session_maker(engine)
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

        # Save user message
        user_msg = Message(session_id=session_id, role="user", text=req.text)
        db.add(user_msg)

        # Build history
        state = json.loads(session.state_json)
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

    file_analysis_system = """Ты — медицинский информационный ассистент. Пациент прислал документ.
Извлеки медицинские данные для внутреннего использования (НЕ для показа пациенту):
- Тип документа (анализы, снимок, заключение, рецепт)
- Диагнозы, если указаны (код МКБ)
- Ключевые показатели и отклонения от нормы
- Назначения/рекомендации
Ответь КРАТКО, списком фактов."""

    # Analyze images via LLM vision
    if content_type.startswith("image/"):
        b64 = base64.b64encode(content).decode()
        images = [{"media_type": content_type, "data": b64}]
        try:
            result = await triage_engine.llm.generate(
                "Извлеки медицинские данные из этого изображения.",
                file_analysis_system, images=images, use_cache=False,
            )
            analysis = result.text
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
                result = await triage_engine.llm.generate(
                    f"Извлеки медицинские данные из этого документа:\n\n{pdf_text[:4000]}",
                    file_analysis_system, use_cache=False,
                )
                analysis = result.text
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
async def download_pdf(session_id: str):
    async with db_session_maker() as db:
        session = await db.get(Session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if session.status not in ("completed", "emergency"):
            raise HTTPException(409, "Session not yet completed")

        state = json.loads(session.state_json)

    # LLM enrichment: medical terminology, questions, what to bring
    llm_data = await triage_engine.generate_pdf_data(state)

    # Build full PDF data from session state + LLM enrichment
    routing = state.get("routing", {})
    triage = state.get("triage", {})
    specialists = routing.get("specialists", [])

    # If preparation is missing (old sessions), regenerate via LLM
    needs_preparation = any(
        isinstance(spec, dict) and not spec.get("preparation")
        for spec in specialists
    )
    if needs_preparation and specialists:
        symptoms_json = json.dumps(state.get("symptoms", []), ensure_ascii=False)
        triage_summary = triage.get("summary", "")
        from app.prompts import PromptTemplates
        specialists_for_prompt = json.dumps(
            [{"specialty": s.get("specialty", ""), "reason": s.get("reason", "")}
             for s in specialists if isinstance(s, dict)],
            ensure_ascii=False,
        )
        system, prompt = PromptTemplates.preparation(
            specialists_for_prompt, symptoms_json, triage_summary
        )
        try:
            prep_result = await triage_engine.llm.generate(prompt, system, use_cache=False)
            from app.services.triage_engine import extract_json as ej
            prep_data = ej(prep_result.text)
            preparations = prep_data.get("preparations", {})
            for spec in specialists:
                if isinstance(spec, dict) and not spec.get("preparation"):
                    spec_name = spec.get("specialty", "")
                    if spec_name in preparations:
                        spec["preparation"] = preparations[spec_name]
        except Exception:
            logger.warning("PDF preparation generation failed, using KB fallback")
            for spec in specialists:
                if isinstance(spec, dict) and not spec.get("preparation"):
                    spec_name = spec.get("specialty", "")
                    for key, kb_data in triage_engine.kb.specialties.items():
                        if kb_data["name"] == spec_name:
                            spec["preparation"] = kb_data.get("preparation", [])
                            break

    # Detect red flags from conversation
    red_flags = []
    for line in state.get("history_lines", []):
        if line.startswith("Пациент: "):
            text = line[len("Пациент: "):]
            flag = triage_engine.kb.check_red_flags(text)
            if flag and flag not in red_flags:
                red_flags.append(flag)

    pdf_data = {
        # LLM-generated enrichments
        "complaints_medical": llm_data.get("complaints_medical", ""),
        "complaints_simple": llm_data.get("complaints_simple", triage.get("summary", "")),
        "questions_for_doctor": llm_data.get("questions_for_doctor", []),
        "what_to_bring": llm_data.get("what_to_bring", []),
        # Direct session data
        "urgency": triage.get("urgency", "medium"),
        "symptoms_list": state.get("symptoms", []),
        "specialists": specialists,
        "routing_explanation": routing.get("explanation", ""),
        "history_lines": state.get("history_lines", []),
        "uploaded_files": state.get("uploaded_files", []),
        "red_flags": red_flags,
    }

    pdf_bytes = PDFGenerator.generate(pdf_data, session_id)

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

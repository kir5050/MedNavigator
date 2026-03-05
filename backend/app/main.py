import json
import logging

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
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

app = FastAPI(title="MedNavigator API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
db_session_maker = None
triage_engine = None


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

    engine = await get_engine(settings.database_url)
    db_session_maker = get_session_maker(engine)

    providers = build_providers()
    llm = LLMManager(providers, settings.cache_dir)
    kb = MedicalKB()
    triage_engine = TriageEngine(llm, kb)

    logger.info(
        "Started with providers: %s", [p.name for p in providers]
    )


# --- Request/Response models ---

class MessageRequest(BaseModel):
    text: str

class FeedbackRequest(BaseModel):
    session_id: str
    rating: int
    comment: str | None = None
    was_helpful: bool | None = None


# --- Endpoints ---

@app.post("/api/v1/session/start", status_code=201)
async def start_session():
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


@app.post("/api/v1/session/{session_id}/message")
async def send_message(session_id: str, req: MessageRequest):
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

        # Save triage result if completed
        if result["status"] == "completed" and result.get("triage"):
            triage = TriageResult(
                session_id=session_id,
                urgency=result["triage"].get("urgency", "medium"),
                specialists=result.get("specialists", []),
                symptoms_summary=result["triage"].get("summary", ""),
            )
            db.add(triage)

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

    # Generate PDF data via LLM
    pdf_data = await triage_engine.generate_pdf_data(state)

    # Enrich with structured data
    pdf_data["specialists"] = state.get("routing", {}).get("specialists", [])
    pdf_data["urgency"] = state.get("triage", {}).get("urgency", "medium")

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

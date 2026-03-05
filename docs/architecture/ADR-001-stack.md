# ADR-001: Technology Stack

## Status: PROPOSED (awaiting user approval)

## Context
MedNavigator — MVP informational service for medical triage routing.
Constraints: mobile-first PWA, Russian-hosted LLM (YandexGPT/GigaChat),
single developer, budget ~10k RUB/month for LLM API, must run locally with one command.

---

## 1. Frontend

| Option | Pros | Cons |
|--------|------|------|
| **React + Vite** | Fast dev, huge ecosystem, easy PWA via vite-plugin-pwa, lightweight | No SSR out of the box (not needed for chat UI) |
| Next.js | SSR/SSG, full-stack, good PWA support | Overkill for chat app, heavier, Vercel-centric |
| Streamlit | Fastest prototyping, Python-only | Not production-grade, ugly on mobile, no PWA |

**Recommendation:** React + Vite — minimal, fast, great mobile support, easy PWA setup.

## 2. PDF Generation

| Option | Pros | Cons |
|--------|------|------|
| **WeasyPrint** | HTML/CSS -> PDF, Python-native, good Cyrillic support | System deps (cairo, pango) |
| ReportLab | Pure Python, no system deps | Low-level API, harder to style |
| Puppeteer | Full browser rendering | Node.js dependency, heavy |

**Recommendation:** WeasyPrint — best balance of quality and ease for Russian text.

## 3. Database for Sessions

| Option | Pros | Cons |
|--------|------|------|
| **SQLite** | Zero config, file-based, perfect for MVP | Single-writer, no remote access |
| PostgreSQL | Full-featured, concurrent writes | Overkill for MVP, extra infra |
| No DB (in-memory) | Simplest | Data lost on restart, no analytics |

**Recommendation:** SQLite — zero overhead, sufficient for MVP, easy migration to PostgreSQL later.

## 4. LLM Response Caching

| Option | Pros | Cons |
|--------|------|------|
| **File-based (diskcache)** | Persistent, no extra services, Python-native | Slower than Redis |
| Redis | Fast, TTL support | Extra service to run, overkill for MVP |
| In-memory (dict/lru_cache) | Fastest, zero setup | Lost on restart, no persistence |

**Recommendation:** diskcache (file-based) — persistent, no extra infra, good enough for MVP.

## 5. Backend

FastAPI (as specified in requirements) — async, auto-docs, Pydantic validation.

## Summary of Proposed Stack

| Component | Choice |
|-----------|--------|
| Backend | FastAPI + Python 3.11+ |
| Frontend | React + Vite + TypeScript |
| PWA | vite-plugin-pwa |
| Database | SQLite (via SQLAlchemy/aiosqlite) |
| PDF | WeasyPrint |
| LLM Cache | diskcache |
| LLM | OpenRouter (dev/test) / YandexGPT (prod primary) / GigaChat (prod fallback) |
| Containerization | Docker Compose |

## Decision
Awaiting user confirmation.

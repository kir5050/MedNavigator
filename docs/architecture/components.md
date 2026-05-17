# Component Architecture

## System Overview

```
                    +------------------+
                    |   User (Mobile)  |
                    +--------+---------+
                             |
                    +--------v---------+
                    |  Frontend (PWA)  |
                    |  React + Vite    |
                    +--------+---------+
                             |
                        REST API
                             |
                    +--------v---------+
                    |  Backend (FastAPI)|
                    |                  |
                    |  +------------+  |
                    |  | Session    |  |
                    |  | Manager    |  |
                    |  +-----+------+  |
                    |        |         |
                    |  +-----v------+  |
                    |  | Triage     |  |
                    |  | Engine     |  |
                    |  +-----+------+  |
                    |        |         |
                    +--------+---------+
                             |
              +--------------+--------------+
              |              |              |
    +---------v---+  +-------v------+ +-----v--------+
    | LLM Service |  | Medical KB   | | PDF Generator|
    | (OpenRouter)|  | (symptoms,   | | (WeasyPrint) |
    |             |  |  specialties)| |              |
    +------+------+  +-------+------+ +--------------+
           |                 |
    +------v------+  +-------v------+
    | LLM Cache   |  | SQLite DB    |
    | (diskcache) |  | (sessions,   |
    |             |  |  results)    |
    +-------------+  +--------------+
```

## Components

### 1. Frontend (PWA)
- React + Vite + TypeScript
- Chat-like interface for symptom collection
- Result display with triage recommendation
- PDF download button
- Mobile-first responsive design
- Offline-capable via service worker

### 2. Backend API (FastAPI)
- REST API with OpenAPI docs
- Session management
- Request validation (Pydantic)
- Rate limiting

### 3. Session Manager
- Creates/manages conversation sessions
- Stores message history
- Tracks session state (collecting -> triaging -> completed)

### 4. Triage Engine
- Orchestrates LLM prompt chain:
  1. Symptom extraction from user messages
  2. Clarifying questions generation
  3. Triage assessment (urgency level)
  4. Specialist routing
  5. Visit preparation summary
- Red flag detection (immediate emergency routing)

### 5. LLM Service
- Provider abstraction via `LLMProvider` ABC; current implementation: OpenRouter
- Prompt templates management
- Response parsing and validation
- Provider fallback chain (trivial with a single provider, extensible)

### 6. Medical Knowledge Base
- Specialty definitions and routing rules
- Red flag symptoms list
- Common symptom-specialty mappings
- Static data, loaded at startup

### 7. PDF Generator
- Generates visit preparation document
- Includes: symptoms summary, suspected area, questions for doctor
- WeasyPrint with HTML/CSS templates
- Cyrillic font support

### 8. LLM Cache
- Caches LLM responses for common symptom patterns
- File-based (diskcache) with configurable TTL
- Cache key: normalized symptom set + prompt template hash

### 9. SQLite Database
- Sessions, messages, triage results, feedback
- No personal data linkage
- Analytics queries

## Data Flow: Typical Session

1. User opens PWA -> Frontend sends POST /session/start
2. Backend creates session, returns greeting + first question
3. User describes symptoms -> POST /session/{id}/message
4. Triage Engine extracts symptoms via LLM, checks red flags
5. If red flags -> immediate emergency recommendation
6. If needs clarification -> generates follow-up question
7. If enough data -> runs triage, returns result
8. User requests PDF -> GET /session/{id}/pdf
9. PDF Generator creates document, returns file

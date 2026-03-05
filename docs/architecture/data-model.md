# Data Model

## Overview
SQLite database, no personal data linkage. Sessions are anonymous.

## Entities

### Session
| Field | Type | Description |
|-------|------|-------------|
| id | UUID (PK) | Session identifier |
| status | ENUM | collecting, triaging, completed, emergency, expired |
| created_at | DATETIME | Session start time |
| updated_at | DATETIME | Last activity |
| message_count | INT | Number of messages exchanged |
| language | VARCHAR(5) | Always "ru" for MVP |

### Message
| Field | Type | Description |
|-------|------|-------------|
| id | UUID (PK) | Message identifier |
| session_id | UUID (FK) | Reference to session |
| role | ENUM | user, assistant |
| text | TEXT | Message content |
| created_at | DATETIME | Timestamp |
| extracted_symptoms | JSON | Symptoms extracted from this message (nullable) |

### TriageResult
| Field | Type | Description |
|-------|------|-------------|
| id | UUID (PK) | Result identifier |
| session_id | UUID (FK, unique) | One result per session |
| urgency | ENUM | low, medium, high, emergency |
| specialists | JSON | Array of {specialty, reason, priority} |
| symptoms_summary | TEXT | Human-readable symptom summary |
| preparation | TEXT | Visit preparation text |
| llm_provider | VARCHAR(20) | Which LLM was used |
| tokens_total | INT | Total tokens consumed |
| created_at | DATETIME | Timestamp |

### Feedback
| Field | Type | Description |
|-------|------|-------------|
| id | UUID (PK) | Feedback identifier |
| session_id | UUID (FK) | Reference to session |
| rating | INT | 1-5 |
| comment | TEXT | Optional free text |
| was_helpful | BOOLEAN | Quick yes/no |
| created_at | DATETIME | Timestamp |

## Relationships
```
Session 1--* Message
Session 1--1 TriageResult
Session 1--0..1 Feedback
```

## Indexes
- Session: status, created_at
- Message: session_id, created_at
- TriageResult: session_id (unique)
- Feedback: session_id, rating

## Privacy
- No user accounts, no login
- Session ID is the only identifier (UUID, not guessable)
- Sessions auto-expire after 24 hours (status -> expired)
- No IP addresses or device fingerprints stored
- Analytics are aggregate only

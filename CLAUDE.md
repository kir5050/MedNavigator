# MedNavigator — AI-навигатор первичной медицинской маршрутизации

## 1. О проекте
Информационный сервис, который помогает пациенту:
1) понять, к какому врачу обратиться по описанным жалобам;
2) подготовиться к визиту (чеклист + PDF-выписка).

Это **НЕ** медицинское изделие, **НЕ** телемедицина, **НЕ** диагностическая
система. Юрисдикция — РФ; инфраструктура и LLM — российские
(YandexGPT/GigaChat). OpenRouter — только для разработки.

---

## 2. Правила работы команды агентов
- Каждый агент работает ТОЛЬКО в своей директории.
- Все спорные решения — эскалация к пользователю.
- Между этапами — пауза для согласования; следующий этап не начинается
  без явного «ОК» от пользователя.
- Изменения в `prompts/templates.py`, `medical_kb/*.yaml`, endpoints с
  медицинским контентом — требуют ревью пользователя.

---

## 3. ЮРИДИЧЕСКИЕ ГРАНИЦЫ (КРИТИЧНО — нарушение = юридический риск)

- **НИКОГДА не ставить диагноз.** Запрещены формулировки «у вас [болезнь]».
  Допустимо: «ваши симптомы могут быть связаны с заболеваниями ЖКТ».
- **НИКОГДА не назначать лечение, лекарства, дозировки, процедуры, БАДы.**
- **Каждый ответ API с медицинским контентом содержит дисклеймер**
  (см. список endpoints в §4):
  `«Информация носит справочный характер и не заменяет консультацию врача.»`
  Константа: `DISCLAIMER` в `backend/app/main.py`.
- **При red flags** (см. `medical_kb/red_flags.yaml`) — немедленно вернуть
  рекомендацию вызвать 103/112 и прекратить опрос. Проверка red flags
  ВСЕГДА выполняется ДО вызова LLM
  (`backend/app/services/triage_engine.py`, `process_message`).
- **В неоднозначных случаях** — всегда рекомендовать обращение к врачу,
  округлять срочность вверх.
- **Не хранить персональные медицинские данные с привязкой к личности.**
  Сессии анонимны (UUID), без логина, без IP, авто-истечение через 24 ч.

Все LLM-промпты обязаны включать `LEGAL_GUARDRAILS` из
`backend/app/prompts/templates.py`. При добавлении нового промпта —
встраивайте этот блок в system message.

### 3.1. Запрещённые слова в пользовательском тексте (grep-checkable)

Не должны появляться в текстах для пациента (UI-копи, ответы LLM,
сообщения API, тексты лендинга, маркетинг):

```
диагноз, диагностика, диагностировать, диагностический
лечение, лечить, лечебный, вылечить
терапия (как лечебная процедура)
препарат, лекарство, медикамент, таблетка (как назначение)
фармпрепарат
дозировка, доза (как назначение)
процедура (в значении «назначенная медпроцедура»)
рецепт (как назначение)
БАД, биодобавка
```

Допустимы ТОЛЬКО в:
- технических комментариях и именах идентификаторов в коде;
- PDF-выписке для врача (`backend/app/pdf/generator.py`) — там нужна
  медицинская терминология;
- внутренней документации (`docs/`, `CLAUDE.md`);
- системных промптах, обращённых к LLM (внутренние инструкции).

Grep перед мерджем (любое попадание → ручной разбор):

```bash
grep -rEn 'диагноз|диагностик|лечени|вылеч|лечеб|препарат|лекарств|дозировк|рецепт|бад|биодобавк|фармпрепарат' \
  frontend/src landing backend/app/main.py backend/app/prompts/ \
  backend/app/medical_kb/symptoms.yaml backend/app/medical_kb/specialties.yaml \
  backend/app/medical_kb/red_flags.yaml
```

---

## 4. Инварианты кода, которые НЕ нарушать

- Red-flag check вызывается ПЕРВЫМ в `TriageEngine.process_message` —
  до любых LLM-запросов и до проверки осмысленности текста.
- `DISCLAIMER` присутствует в ответах endpoints с медицинским контентом:
  - `POST /api/v1/session/start`
  - `POST /api/v1/session/{id}/message`
  - `POST /api/v1/session/{id}/triage`
  - `GET  /api/v1/session/{id}/result`
  - `GET  /api/v1/session/{id}/pdf` (внутри тела PDF)

  При добавлении нового публичного endpoint, возвращающего медицинский
  контент, — добавлять дисклеймер.
- `MAX_CLARIFICATIONS = 4` (`services/triage_engine.py`) — лимит
  уточняющих вопросов. Если он исчерпан без симптомов, сессия не должна
  выходить в `ready`.
- `MAX_SESSION_MESSAGES = 30` (`main.py`) — жёсткий лимит на сессию.
- `run_triage` отказывается выдавать рекомендации без подтверждённых
  симптомов — не ослаблять эту проверку.
- LLM-маршрутизация валидируется через KB-скоринг по `symptoms_hint`
  (`MedicalKB.validate_llm_routing`). Если все LLM-специалисты имеют
  score < 0.2 — fallback на KB-маршрутизацию. Эту защитную полосу
  снимать только осознанно, с обновлением тестов.
- Симптомы извлекаются ТОЛЬКО из реплик пациента, не из вопросов
  ассистента (`prompts/templates.py`, `symptom_extraction`).

---

## 5. Язык и тон
- UI/тексты для пациента: русский, обращение на «вы».
- Тон: спокойный, уверенный, не пугающий.
- Медицинская терминология: только в PDF-выписке для врача
  (`backend/app/pdf/generator.py`), не в чат-интерфейсе.
- Код, имена идентификаторов, технические комментарии, commit messages:
  английский.

---

## 6. Архитектура (карта)

```
Frontend (React+Vite, PWA, mobile-first)
        ▼ REST
Backend (FastAPI)
  ├─ SessionManager  → SQLite (sessions, messages, triage_results, feedback)
  ├─ TriageEngine    → orchestration:
  │     red-flag check → symptom extraction → clarification (×0–4)
  │     → triage → routing → preparation → PDF summary
  ├─ MedicalKB       → YAML (symptoms / specialties / red_flags), синонимы,
  │                    scoring symptoms_hint, валидация LLM-роутинга
  ├─ LLMManager      → каскад провайдеров + diskcache
  │     OpenRouter (dev) / YandexGPT (prod primary) / GigaChat (prod fallback)
  └─ PDFGenerator    → WeasyPrint (HTML/CSS → PDF), кэш в TriageResult.pdf_cache
```

Доп. подсистемы: `slowapi` rate-limiting на чувствительных endpoints,
глобальный exception handler → Telegram-алерт.

---

## 7. Структура репозитория (ключевые файлы)

```
backend/
  app/
    main.py                       # FastAPI app, endpoints, DISCLAIMER, лимиты
    config.py                     # Settings (env vars), CORS, TTL кэша
    services/triage_engine.py     # TriageEngine — оркестрация LLM-цепочки
    medical_kb/
      knowledge_base.py           # MedicalKB: red flags, синонимы, scoring
      red_flags.yaml              # шаблоны экстренных симптомов + сообщения
      symptoms.yaml               # ~50 симптомов с синонимами и area
      specialties.yaml            # специалисты, areas, symptoms_hint, preparation
    llm/
      base.py                     # LLMProvider ABC, LLMResponse
      manager.py                  # каскад + diskcache
      yandexgpt.py, gigachat.py, openrouter.py
    prompts/templates.py          # LEGAL_GUARDRAILS + все системные промпты
    models/database.py            # SQLAlchemy: Session, Message, TriageResult, Feedback
    pdf/generator.py              # WeasyPrint-сборка PDF-выписки
  tests/                          # pytest, async
  requirements.txt
frontend/                         # React+Vite+TS PWA
landing/                          # статический лендинг
design/                           # mockups, tokens
docs/architecture/                # ADR-001 + components/data-model/llm-provider
docker-compose.yml                # backend (8080) + frontend (3000)
.github/workflows/test.yml        # CI: pytest on PR/push to main
```

---

## 8. Триаж-пайплайн

`process_message` (на каждое сообщение пациента):
1. **Red-flag check** по `red_flags.yaml` (substring match, до LLM).
2. Отсев бессмысленного ввода (regex на буквы).
3. **Symptom extraction** (LLM, JSON-ответ; кэшируется).
4. KB-match: подсветка симптомов по синонимам.
5. Решение **clarify vs ready**: `len(symptoms) >= 2` и
   `clarification_count >= 1`, до `MAX_CLARIFICATIONS = 4`.
6. Если уточнений хватило, но симптомов нет — сброс счётчика,
   повторный запрос.

`run_triage` (по явному запросу пользователя):
1. Защита от пустого набора симптомов.
2. **Triage** (urgency + medical_areas + summary, LLM JSON).
3. **Routing** (1–3 специалиста, LLM JSON).
4. **KB-валидация роутинга** через `symptoms_hint`-overlap:
   - все LLM-специалисты <0.2 → полный fallback на KB-роутинг;
   - иначе — добавляем KB-suggestions ≥0.5, которые LLM пропустил.
5. **Preparation**: LLM генерирует персонализированный чеклист;
   при ошибке — статический fallback из `specialties.yaml`.

`generate_pdf_data` собирает данные для PDF, включая анализ загруженных
файлов (изображения через vision, PDF через PyMuPDF) — см. `main.py`,
`upload_file` / `download_pdf`.

---

## 9. LLM-слой и кэширование

- Интерфейс — `LLMProvider` (`llm/base.py`), реализации: OpenRouter,
  YandexGPT, GigaChat. Порядок задаётся `LLM_PRIMARY_PROVIDER` —
  выбранный провайдер первый, остальные — fallback (`main.py`,
  `build_providers`).
- `LLMManager.generate` (`llm/manager.py`):
  - SHA-256 cache key по `(system, prompt)`;
  - diskcache на диске, TTL по умолчанию 24 ч,
    конфиг через `CACHE_TTL_CLARIFICATION` / `CACHE_TTL_TRIAGE`;
  - image-запросы НЕ кэшируются;
  - при провале провайдера — переход к следующему, в логи warning.
- **Бюджет**: ~10 000 ₽/мес. Любое расширение цепочки промптов должно
  оценивать стоимость и максимизировать кэшируемость
  (детерминированные промпты, `use_cache=True` по умолчанию).
- Запросы с `use_cache=False` явно помечены в коде (`clarification`,
  `triage`, `routing`, `preparation`, `pdf_summary`) — там персонализация
  важнее экономии.

---

## 10. База знаний

- `symptoms.yaml`: ключ → `name`, `synonyms[]`, `area`.
- `specialties.yaml`: ключ → `name`, `description`, `areas[]`,
  `symptoms_hint[]`, `preparation[]`.
- `red_flags.yaml`: категория → `patterns[]`, `message`.
- Изменения KB **не требуют** правки кода, но **требуют**:
  - сохранять синонимы в нижнем регистре;
  - не использовать диагнозы в `name`/`description`;
  - red-flag `message` всегда содержит явное указание на 103/112
    (или 8-800-2000-122 для суицидальных).

---

## 11. Данные и приватность

- SQLite, async (`aiosqlite`), модель в `models/database.py`.
- Сессии анонимны (UUID), без аккаунтов и IP-трекинга.
- TTL сессии: 24 часа (`SESSION_TTL_HOURS`), затем `status = expired`.
- PDF кэшируется в `TriageResult.pdf_cache` (BLOB) после первой
  генерации.
- Аналитика — агрегированная (`/api/v1/analytics/dashboard`, защищён
  `ADMIN_TOKEN`). Значение `ADMIN_TOKEN` хранится только в `.env` и в
  секретах GitHub Actions; никогда не коммитить, не печатать в логах,
  не вставлять в PDF/выписки/UI.

---

## 12. Честность данных

Запрещено выдавать непроверенные данные за факты — это касается кода,
тестовых моков, демо-сценариев, лендинга, презентаций и любых
маркетинговых материалов.

Конкретно:
- **Никаких фиктивных отзывов пациентов и врачей.** Если отзывы есть —
  только реальные, с разрешения автора. Демо-моки помечать как
  «пример» / «demo».
- **Никакой непроверенной статистики.** Метрики точности, скорости,
  удовлетворённости, экономии времени, NPS — только если есть
  измеренный источник. Источник указывается рядом или в комментарии в коде.
- **Никакого fabricated social proof.** Логотипы клиник/партнёров,
  «упоминания в СМИ», числа пользователей, рейтинги — только при наличии
  письменного подтверждения.
- **Никаких медицинских claim'ов без источника** (например, «снижает
  риск осложнений на N%», «соответствует клиническим рекомендациям X»).
- **Прогноз vs факт.** Если данные — внутренние оценки, прогноз или
  ранний замер, формулировка должна это отражать: «по нашим внутренним
  замерам», «прогноз», «ожидаемое значение», а НЕ как утверждение
  о свершившемся факте.

Если агент видит в задаче или в правке непроверяемое утверждение —
эскалировать пользователю до коммита.

---

## 13. Команды разработки

В репозитории нет `Makefile`, `pyproject.toml`, `pytest.ini` или
корневого `package.json`. Канонические команды:

```bash
# Полный стек локально
docker compose up --build
# backend → http://localhost:8080
# frontend → http://localhost:3000

# Backend локально (Python 3.12, как в CI)
pip install -r backend/requirements.txt
pip install pytest pytest-asyncio    # pytest НЕ в requirements.txt
uvicorn app.main:app --reload --port 8080   # из каталога backend/

# Тесты (как в .github/workflows/test.yml)
python -m pytest backend/tests/ -v

# Frontend (из каталога frontend/)
npm install
npm run dev       # vite dev server
npm run build     # tsc && vite build
npm run preview   # превью прод-сборки
```

`.env` создаётся из `.env.example`. Минимум для запуска — один LLM-ключ
(в dev обычно `OPENROUTER_API_KEY`).

---

## 14. Конвенции кода и тестов

- Python: 3.12 (как в CI), FastAPI, async везде где есть I/O.
- Все LLM-ответы парсятся через `extract_json`
  (`services/triage_engine.py`) — устойчив к markdown-code-fences и шуму.
- При сбое парсинга — graceful fallback (warning в лог, безопасный
  ответ), никогда не 500 наружу с медицинским контентом.
- Любая новая ветка пайплайна должна иметь:
  - тест на red-flag-обход (red flag не должен теряться);
  - тест на пустой/мусорный ввод (не должен приводить к выдаче
    рекомендации) — см. `tests/test_empty_symptoms_guard.py` как образец;
  - проверку, что `DISCLAIMER` уходит в ответе.
- Frontend: TypeScript строгий, mobile-first, PWA через
  `vite-plugin-pwa`.

---

## 15. Git-конвенция

- **Ветки** (kebab-case после префикса):
  - `feat/<short-name>` — новая функциональность;
  - `fix/<short-name>` — исправление бага;
  - `update/<short-name>` — улучшение/доработка существующего
    (внутренняя конвенция проекта; в коммитах не используется);
  - `chore/<short-name>` — инфраструктура, конфиги, зависимости, доки.
- **Коммиты на английском**, по
  [Conventional Commits](https://www.conventionalcommits.org/).
  Допустимые типы:
  `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`, `perf:`,
  `ci:`, `build:`, `style:`.
  Сообщение в повелительном наклонении, без точки в конце subject-строки.
- **`main` защищён.** Прямой push в `main` запрещён — только через PR,
  с зелёным CI (`.github/workflows/test.yml`).
- **Force-push в `main`** — никогда.
- Коммиты с сгенерированным/AI-кодом помечаются `Co-Authored-By`.

---

## 16. Деплой

Агенту **запрещено**:
- мерджить PR в `main`;
- создавать и пушить git-теги (`git tag` / `git push --tags`);
- менять CD-workflow и любые workflow в `.github/workflows/`
  без явного запроса пользователя;
- собирать и пушить Docker-образы в реестр;
- запускать деплой-команды (включая `docker compose` на проде,
  миграции на проде, ручные операции с проднодом);
- коммитить файлы с секретами (`.env`, `*.key`, `*.pem`, любые API-ключи,
  токены, OAuth client_secret в открытом виде);
- выводить значения секретов из `.env` в логи, commit messages,
  описания PR, тестовые фикстуры, демо-скрипты.

Агенту **разрешено**:
- готовить ветку и PR;
- обновлять локальные конфиги в репозитории (без секретов);
- запускать тесты и линтеры локально;
- предлагать чек-лист деплоя в описании PR.

Merge, тегирование релиза и сам деплой выполняет только пользователь.

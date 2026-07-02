# PROJECT_MEMORY.md — Availabl Agent Brain
> **AGENT RULE:** Read this file FIRST at the start of every session. Update it LAST before finishing.
> Never re-scan the whole codebase if the answer is here.

---

## Last updated: 2026-07-02

---

## 1. WHAT THIS PROJECT IS

**Availabl** — SaaS платформа для миграции AWS окружений между аккаунтами/регионами.
- Backend Python движок: discovery → plan → deploy → validate → artifacts
- Frontend: multi-page app (Cloudflare Pages)
- Auth: Firebase (Google OAuth)
- DB: Firestore
- Payments: Stripe
- Backend API: Render (Python HTTP server)

---

## 2. DEPLOYED URLS

| Что | URL |
|-----|-----|
| Frontend (prod) | https://availabl.pages.dev |
| Backend API | https://availabl-backend.onrender.com |
| Firebase project | availabl-1f709 |
| Cloudflare account | fc22c21f68493e5cb86b169b7aa57ea3 |
| Render service ID | srv-d920s55ckfvc738fra1g |
| GitHub repo | https://github.com/Rartiushkov/aws-dev-agent |

---

## 3. CREDENTIALS

Все секреты хранятся в `.env` (gitignored) и в Windsurf agent memory.
Не добавлять ключи в этот файл — GitHub Push Protection заблокирует push.

| Ключ | Где искать |
|------|------------|
| Cloudflare API token (wrangler) | .env → CLOUDFLARE_API_TOKEN |
| Render API key | .env → RENDER_API_KEY |
| GitHub token | .env → GITHUB_TOKEN |
| Stripe publishable key | .env → STRIPE_PUBLISHABLE_KEY |
| Stripe secret key | .env → STRIPE_SECRET_KEY (также в Render env vars) |
| Stripe webhook secret | .env → STRIPE_WEBHOOK_SECRET (также в Render env vars) |
| Stripe Pro price ID | price_1ToiXpE5xonjsdoogiiEaYfW ($299/mo) |
| Stripe webhook endpoint ID | we_1ToiaiE5xonjsdooC08zFUCJ |
| Firebase API key (public, в коде) | AIzaSyC2s8vy7THhcs9YO5Ro5lwenICXZpzmgD8 |

---

## 4. DEPLOY COMMANDS

```powershell
# Frontend → Cloudflare Pages (токены из .env)
$env:CLOUDFLARE_API_TOKEN=$env_CLOUDFLARE_API_TOKEN
$env:CLOUDFLARE_ACCOUNT_ID="fc22c21f68493e5cb86b169b7aa57ea3"
npx wrangler pages deploy frontend --project-name availabl --branch main --commit-dirty=true

# Backend → Render (через GitHub push — автодеплой ~3 мин)
git add render_backend.py; git commit -m "..."; git push

# Триггер деплоя Render вручную через API (RENDER_API_KEY из .env)
$headers = @{ "Authorization" = "Bearer $RENDER_API_KEY"; "Content-Type" = "application/json" }
Invoke-RestMethod -Uri "https://api.render.com/v1/services/srv-d920s55ckfvc738fra1g/deploys" -Method POST -Headers $headers -Body "{}"
```

---

## 5. АРХИТЕКТУРА ФРОНТЕНДА

```
frontend/
  index.html          — лендинг (публичный)
  login.html          — Firebase Google OAuth
  dashboard.html      — главная после входа (auth guard)
  migrations.html     — список миграций (auth guard)
  connect.html        — подключение AWS (auth guard)
  onboarding.html     — форма новой миграции (auth guard)
  settings.html       — настройки / удаление аккаунта (auth guard)
  pricing.html        — страница тарифов + Stripe checkout

  auth.js             — Firebase auth (signIn, signOut, requireAuth, watchAuth, getIdToken)
  db.js               — Firestore CRUD (upsertUser, getAwsConnections, getMigrations, addAwsConnection, deleteUser)
  stripe.js           — startCheckout() → /api/checkout → Stripe redirect
  toast.js            — showToast(msg, type) глобальные уведомления
  dashboard.js        — legacy demo data loader (оставить, не трогать)
  dashboard.css       — общие стили всех страниц дашборда + toast стили
```

---

## 6. БЭКЕНД API (render_backend.py)

| Метод | Путь | Auth | Описание |
|-------|------|------|----------|
| GET | /health | нет | статус + список роутов |
| GET | /api/demo | нет | демо данные из state/ |
| GET | /api/me | Bearer | возвращает uid |
| GET | /api/checkout | Bearer | создаёт Stripe Checkout session, возвращает {url} |
| POST | /api/scan | Bearer | ставит AWS скан в очередь |
| POST | /api/webhook | Stripe-Signature | обрабатывает Stripe события |
| GET/POST | /api/cloudflare | нет | проксирует lambda_function.py |

**Firebase token verification:** `identitytoolkit.googleapis.com/v1/accounts:lookup?key=<API_KEY>`
- Принимает idToken, возвращает `localId` (uid)
- НЕ использовать `oauth2.googleapis.com/tokeninfo` — он для OAuth, не для Firebase ID token

**Stripe webhook events обрабатываемые:**
- `checkout.session.completed` → `plan: pro` в Firestore
- `customer.subscription.deleted` → `plan: starter`
- `customer.subscription.paused` → `plan: starter`

---

## 7. FIRESTORE КОЛЛЕКЦИИ

```
users/{uid}
  displayName, email, photoURL, lastLoginAt, plan, createdAt

aws_connections/{uid}_{timestamp}
  name, src_account, src_region, src_role_arn, tgt_account, tgt_region, tgt_role_arn, createdAt, uid

migrations/{uid}_{timestamp}
  name, status, src_account, tgt_account, createdAt, uid
```

**Security rules:** `firestore.rules` — аутентифицированный пользователь может читать/писать только свои документы.

---

## 8. STRIPE ПЛАНЫ

| План | Цена | Описание |
|------|------|----------|
| Starter | $0 | 1 scan, sandbox only |
| Pro | $299/mo | unlimited scans, production accounts |
| Enterprise | contact sales | custom SLA, dedicated engineer |

---

## 9. RENDER — ВАЖНЫЕ ОСОБЕННОСТИ

- **Бесплатный план засыпает через 15 мин** без запросов → первый запрос ждёт ~50 сек
- Деплоит из GitHub ветки `main` автоматически при push
- Env vars установлены: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRO_PRICE_ID`, `FRONTEND_URL`
- `FIREBASE_PROJECT_ID` — захардкожен в коде как fallback `availabl-1f709`

---

## 10. AWS АККАУНТЫ

| Аккаунт | Статус |
|---------|--------|
| 978184426928 | текущий (активный после миграции) |
| 027087672282 | legacy — удалить из конфигов при встрече |

---

## 11. ИСТОРИЯ ИЗМЕНЕНИЙ (changelog)

### 2026-07-02 — Сессия 3
- Добавлен `/api/checkout` GET — создаёт Stripe Checkout Session
- Добавлен `/api/webhook` POST — верификация подписи Stripe, обновляет plan в Firestore
- Добавлен `stripe.js` на фронте — `startCheckout()` ждёт auth через `watchAuth`
- `pricing.html` — кнопка Pro подключена к реальному checkout
- `dashboard.html` — toast при успешном upgrade (`?upgraded=1`)
- Исправлен Firebase token verification: `tokeninfo` → `accounts:lookup`
- `toast.js` — глобальная система уведомлений
- `dashboard.css` — стили toast
- Исправлены `href="#"` во всех sidebar (Inventory/Artifacts/Settings)
- `migrations.html` — badge и subtitle из Firestore
- `index.html` — бренд лого, Pricing в footer, `/summary` → `/api/demo`
- Все env vars добавлены на Render через API

### 2026-07-01 — Сессия 2
- Firebase token verification добавлен в `render_backend.py`
- Добавлены `/api/me` и `/api/scan` endpoints
- `auth.js` — добавлен `getIdToken()`
- `connect.html` — auth guard, dynamic user info
- `dashboard.html` — реальные данные из Firestore
- `settings.html` — убран localStorage, удалена мёртвая `saveName`
- `db.js` — убраны debug console.log
- Все sidebar бренд-лого → `/index.html`

### 2026-06-30 — Сессия 1
- Firebase Auth (Google OAuth) подключён
- Firestore коллекции `users`, `aws_connections`, `migrations`
- `upsertUser` — merge без pre-read (fixes Firestore rules block)
- Logout redirect исправлен на абсолютный URL
- Circular dependency `auth.js` ↔ `db.js` устранён

---

## 12. ИЗВЕСТНЫЕ ПРОБЛЕМЫ / TODO

- [ ] Реальный AWS SDK скан в `/api/scan` (сейчас только queued, без boto3)
- [ ] `og-image.png` не существует в `/assets/` — сломан OG preview
- [ ] Render free tier засыпает — рассмотреть keep-alive или upgrade $7/mo
- [ ] `migrations.html` показывает demo данные из backend, не из Firestore
- [ ] Webhook signature verification использует `hmac.new` — должен быть `hmac.new` (OK, stdlib)

---

## 13. ПРОДУКТОВЫЕ ЗАМЕТКИ

- Не позиционировать как законченную платформу
- Сильная сторона: discovery + cross-account/cross-region recreation за минуты
- Same-region cloning только с `allow_same_scope=true`
- Lead with real engine, not UI

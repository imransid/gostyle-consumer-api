# GoStyle Login — The Easy Guide

This explains how login works in our app, in the simplest way.
No hard words. A styled web version is also published (ask for the link).

Code lives in [`apps/accounts/`](../apps/accounts/).

---

## 🏋️ Think of a gym membership

Login works just like joining a **gym**. Remember this and you understand it all:

| At the gym | In our app |
|---|---|
| Fill a form to join | **Register** |
| Gym texts a code to check your phone | **OTP code** |
| Your secret PIN | **Password** |
| A day pass (works a few hours) | **Access token** |
| Your membership card (lasts long) | **Refresh token** |
| Forgot your PIN, get a new one | **Reset password** |
| Cancel your card | **Logout** |

---

## 📖 5 words to know

- **OTP** — a 6-digit code we text/email you. Like `481920`.
- **Password** — your secret. Only you know it.
- **Access token** — a short pass (30 min). Sent with every request.
- **Refresh token** — a long pass (30 days). Gets you new short passes.
- **Verify** — prove the phone/email is really yours by typing the code.

---

## 👤 The 6 things a user can do

Each one has a tiny picture. Read left to right. ➜

### 1. Sign up
```
📝 name + phone/email + password   ➜   📱 we send a code   ➜   ⏳ almost in
```

### 2. Verify
```
🔢 type the code   ➜   ✅ we check it   ➜   🎫 you get 2 passes (you're in!)
```

### 3. Log in (next time)
```
📧 phone/email + 🔑 password   ➜   ✅ correct   ➜   🎫 2 new passes
```

### 4. Stay logged in
```
⌛ short pass expires   ➜   🔄 app uses long pass   ➜   🎫 fresh short pass
```
You never notice this. It happens quietly.

### 5. Forgot password
```
📧 type your email   ➜   📱 we send a code   ➜   🔢 code + 🔑 new password   ➜   ✅ done
```
Bonus: this also kicks out anyone else in your account.

### 6. Log out
```
🚪 you log out   ➜   ❌ we cancel your long pass
```

---

## 🗺️ Where is each thing in the code?

"I want to know X → open this file."

| I want to understand… | Open this file |
|---|---|
| Email or phone? Cleaning it up | [`identifiers.py`](../apps/accounts/identifiers.py) |
| How the code is sent | [`notifications.py`](../apps/accounts/notifications.py) |
| Password rules (8+, a number…) | [`validators.py`](../apps/accounts/validators.py) |
| Stopping spam / too many tries | [`throttling.py`](../apps/accounts/throttling.py) |
| What we save in the database | [`models.py`](../apps/accounts/models.py) |
| The real rules (make/check code) | [`services.py`](../apps/accounts/services.py) |
| Checking incoming data is valid | [`serializers.py`](../apps/accounts/serializers.py) |
| Each button (endpoint) | [`views.py`](../apps/accounts/views.py) |
| The web addresses (URLs) | [`urls.py`](../apps/accounts/urls.py) |

---

## 🧱 What we save about a user

Just two simple things:

**A member:** name, phone, email, password, "is phone verified?", "is email
verified?", "accepted terms?"

**A code:** where we sent it, the code, wrong tries, when it expires.

> We never save the real password or the real code. We save a **scrambled**
> version. Even we cannot read them. We only check "does it match?"

---

## 🧪 Try it yourself

Start the app: `python manage.py runserver`
In dev, the code is **printed in that terminal** (no real SMS needed).

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1. Sign up. Look at the terminal for:  >>> OTP for +88...: 481920
curl -X POST $BASE/auth/register -H 'Content-Type: application/json' -d '{
  "full_name":"Kevin","identifier":"01712345678",
  "password":"Str0ng!Pass","confirm_password":"Str0ng!Pass","accept_terms":true}'

# 2. Verify with the code you saw
curl -X POST $BASE/auth/otp/verify -H 'Content-Type: application/json' -d '{
  "identifier":"01712345678","code":"481920"}'
# You get back: {"access":"...","refresh":"..."}

# 3. Later, log in with just password
curl -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "identifier":"01712345678","password":"Str0ng!Pass"}'
```

Or open `http://127.0.0.1:8000/api/docs/` in a browser.

---

## 📋 All the endpoints

All start with `/api/v1/`.

| What it does | Address |
|---|---|
| Sign up | `POST /auth/register` |
| Verify the code | `POST /auth/otp/verify` |
| Send the code again | `POST /auth/otp/resend` |
| Log in | `POST /auth/login` |
| Ask for a reset code | `POST /auth/password/forgot` |
| Set a new password | `POST /auth/password/reset` |
| Get a new short pass | `POST /auth/token/refresh` |
| Log out | `POST /auth/logout` |
| See / edit my profile | `GET` or `PATCH /auth/me` |

---

## 🛡️ Why we added safety (simple words)

- **A code dies in 5 minutes.** Old codes can't be reused.
- **Only 5 wrong tries.** A thief can't keep guessing.
- **Wait 60 sec for a new code.** Nobody can spam your phone.
- **Login only says "wrong credentials".** We never reveal which emails exist.
- **"Forgot password" always replies the same.** Nobody can go fishing.
- **Resetting the password logs everyone else out.** Kicks out a thief.
- **The short pass lasts only 30 min.** If it leaks, it dies fast.

---

## 🔌 Not done yet (on purpose)

- **Real SMS/WhatsApp** — codes print to the terminal for now. Add a provider
  (like Twilio) in [`notifications.py`](../apps/accounts/notifications.py).
- **Real email server** — set email settings in production.
- **Google/Apple login, points, push, guest mode** — room is left for these.

---

## ✅ In one line

Sign up → prove it with a code → get a long + short pass → log in with password
after that → reset or log out any time. That's the whole thing. 🙌

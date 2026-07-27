# GoStyle Login: The Easy Guide

This explains how login works in our app, in the simplest way. No hard words.

Code lives in [`apps/accounts/`](../apps/accounts/).

---

## 🏋️ Think of a gym membership

Login works a lot like joining a **gym**, with one twist: you prove your phone
or email FIRST, and only THEN is the membership created. No half-made members.

| At the gym | In our app |
|---|---|
| Ask the desk to send you a code | **Request a code (OTP)** |
| Type the code to prove it is you | **Verify** |
| Fill the form to join | **Register** |
| Your secret PIN | **Password** |
| A day pass (works a few hours) | **Access token** |
| Your membership card (lasts long) | **Refresh token** |
| Cancel your card | **Logout** |

---

## 📖 Words to know

- **OTP**: a 6-digit code we send you. Like `481920`.
- **destination**: the phone number or email we send the code to.
- **destination_type**: which kind it is, `phone` or `email`.
- **Password**: your secret. Only you know it.
- **Access token**: a short pass (30 min). Sent with every request.
- **Refresh token**: a long pass (30 days). Gets you new short passes.
- **Verify**: prove the phone/email is really yours by typing the code.

---

## 👤 The steps a user goes through

Read left to right. ➜

### 1. Ask for a code (request)
```
📱 destination_type + destination   ➜   we send a 6-digit code
```
No account is made yet. Phone codes go over WhatsApp, email codes over email.
In dev they print to the terminal.

### 2. Verify the code
```
🔢 type the code   ➜   ✅ we check it   ➜   we remember "this contact is verified" for 10 minutes
```
You do NOT get a token here. We only record that the contact was proven.

### 3. Register (create the account)
```
📝 name + password for the verified contact   ➜   🎫 account made, you get 2 passes
```
This works only if that contact was verified in the last 10 minutes. This is
the first moment an account exists.

### 4. Log in (next time)
```
📧 destination_type + destination + 🔑 password   ➜   ✅ correct   ➜   🎫 2 new passes
```

### 5. Stay logged in
```
⌛ short pass expires   ➜   🔄 app uses the long pass   ➜   🎫 fresh short pass
```
You never notice this. It happens quietly.

### 6. Log out
```
🚪 you log out   ➜   ❌ we cancel your long pass
```

---

## 🗺️ Where is each thing in the code?

"I want to know X, open this file."

| I want to understand... | Open this file |
|---|---|
| Cleaning up a phone/email (E.164, lowercase) | [`identifiers.py`](../apps/accounts/identifiers.py) |
| How the code is sent (WhatsApp, email, console) | [`notifications.py`](../apps/accounts/notifications.py) |
| Password rules (8+, a number, and so on) | [`validators.py`](../apps/accounts/validators.py) |
| Stopping spam / too many tries (Redis limits) | [`ratelimit.py`](../apps/accounts/ratelimit.py) |
| What we save in the database | [`models.py`](../apps/accounts/models.py) |
| The real rules (make/check code, verify, register) | [`services.py`](../apps/accounts/services.py) |
| Checking incoming data is valid | [`serializers.py`](../apps/accounts/serializers.py) |
| Each button (endpoint) | [`views.py`](../apps/accounts/views.py) |
| The web addresses (URLs) | [`urls.py`](../apps/accounts/urls.py) |

---

## 🧱 What we save about a user

**A member (ConsumerAccount):** name, phone, email, password, "is phone
verified?", "is email verified?", "accepted terms?"

**A code (OtpCode):** the destination, its type, the purpose, the code, wrong
tries, and when it expires.

**A verification (Verification):** proof that a contact passed the code check,
good for 10 minutes and usable once. Register spends it.

> We never save the real password or the real code. We save a **scrambled**
> version. Even we cannot read them. We only check "does it match?"

---

## 🧪 Try it yourself

Start the app: `python manage.py runserver`
In dev, `OTP_SENDER=console`, so the code is printed in that terminal (no real
WhatsApp or email needed).

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1. Ask for a code. Watch the terminal for:  >>> OTP for +8801712345678 [phone]: 481920
curl -X POST $BASE/auth/otp/request -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","purpose":"register"}'

# 2. Verify with the code you saw
curl -X POST $BASE/auth/otp/verify -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","purpose":"register","code":"481920"}'
# You get back: {"verified":true,"account_exists":false}

# 3. Register (only right after a successful verify). You get the passes here.
curl -X POST $BASE/auth/register -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","purpose":"register",
  "full_name":"Kevin","password":"Str0ng!Pass","confirm_password":"Str0ng!Pass","accept_terms":true}'
# You get back: {"access":"...","refresh":"..."}

# 4. Later, log in with just the password
curl -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","password":"Str0ng!Pass"}'
```

Or open `http://127.0.0.1:8000/api/docs/` in a browser.

Note: `01712345678` is cleaned to `+8801712345678` (Bangladesh) before we use it.

---

## 📋 All the endpoints

All start with `/api/v1/`.

| What it does | Address |
|---|---|
| Ask for a code | `POST /auth/otp/request` |
| Send the code again (same as request) | `POST /auth/otp/resend` |
| Verify the code | `POST /auth/otp/verify` |
| Create the account | `POST /auth/register` |
| Log in | `POST /auth/login` |
| Get a new short pass | `POST /auth/token/refresh` |
| Log out | `POST /auth/logout` |
| See / edit my profile | `GET` or `PATCH /auth/me` |

---

## 🛡️ Why we added safety (simple words)

- **No account before you verify.** We never make a member until the code is proven.
- **A code dies in 5 minutes.** Old codes cannot be reused.
- **Only 5 wrong tries.** A thief cannot keep guessing, and the wrong-try count sticks even when we return the error.
- **Wait 60 sec for a new code**, plus hourly and daily limits per contact and per IP (kept in Redis). Nobody can spam a phone.
- **Asking for a code always replies the same** whether or not an account exists. Nobody can go fishing for who is registered.
- **The short pass lasts only 30 min.** If it leaks, it dies fast.

---

## 📨 About sending codes

- **Phone: WhatsApp only.** Phone codes go through the WhatsApp Cloud API. There is no SMS.
- **Email:** sent through the normal email server in production.
- **Dev:** keep `OTP_SENDER=console` (the local default) and codes print to the terminal.

Details live in [`notifications.py`](../apps/accounts/notifications.py) and the settings files.

---

## 🔌 Room left for later

- **Google / Apple login, points, push, guest mode:** space is left for these.

---

## ✅ In one line

Ask for a code ➜ prove it by typing the code ➜ register to create the account
and get a long + short pass ➜ log in with the password after that. That is the
whole thing. 🙌

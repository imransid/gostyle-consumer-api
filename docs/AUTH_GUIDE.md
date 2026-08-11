# GoStyle Login: The Easy Guide

This explains how login works in our app, in the simplest way. No hard words.

Code lives in [`apps/accounts/`](../apps/accounts/).

---

## 🏋️ Think of a gym membership

Joining works a lot like joining a **gym**: you fill in the form, and they hand
you your card there and then. Only afterwards does the desk check that the
phone number you wrote down is really yours.

| At the gym | In our app |
|---|---|
| Fill the form and join | **Register** |
| They hand you a card right away | **Access + refresh token** |
| The desk texts your number to check it | **Request a code (OTP)** |
| You read the code back to them | **Verify** |
| Your secret PIN | **Password** |
| A day pass | **Access token** |
| Your membership card (lasts long) | **Refresh token** |
| Cancel your card | **Logout** |

**The important bit:** the desk only ever sends the code to the number *already
on your file*. You cannot ask them to send it to someone else's phone. Even if
you write a different number on the slip, they ignore it and text your own.

Until you read that code back, you are a member with an unchecked number. You
can look around, but you cannot log in again from scratch.

---

## 📖 Words to know

- **OTP**: a 6-digit code we send you. Like `481920`.
- **destination**: the phone number or email we send the code to.
- **destination_type**: which kind it is, `phone` or `email`.
- **Password**: your secret. Only you know it.
- **Access token**: the pass sent with every request (7 days).
- **Refresh token**: a longer pass (30 days). Gets you new access tokens.
- **Verify**: prove the phone/email is really yours by typing the code.
- **Verified**: we have checked your contact. New accounts start **not** verified.

---

## 👤 The steps a user goes through

Read left to right. ➜

### 1. Register (create the account)
```
📝 name + contact + password   ➜   🎫 account made, you get 2 passes
```
This is the first step, not the last. The account exists straight away and you
are logged in straight away. It is marked **not verified yet**.

### 2. Ask for a code (request)
```
🎫 your pass   ➜   we text/email the contact ON YOUR ACCOUNT
```
You must be logged in for this. You do not choose where the code goes — we
look up your own phone or email and send it there. Phone codes go over
WhatsApp, email codes over email. In dev they print to the terminal.

### 3. Verify the code
```
🔢 type the code   ➜   ✅ we check it   ➜   your account is marked verified
```
No new passes here; you already have them. What changes is that your account
is now verified.

### 4. Log in (next time)
```
📧 destination_type + destination + 🔑 password   ➜   ✅ correct   ➜   🎫 2 new passes
```
This only works once your contact is verified. If you skipped step 3, we say
"Please verify your account before logging in."

### 5. Stay logged in
```
⌛ access pass expires   ➜   🔄 app uses the refresh pass   ➜   🎫 fresh access pass
```
You never notice this. It happens quietly.

### 6. Log out
```
🚪 you log out   ➜   ❌ we cancel your refresh pass
```

---

## 🔐 Why this order?

It used to be the other way round: prove the contact first, then create the
account. That sounded safer, but it had a hole. To send a code we had to accept
a phone number or email **from whoever was asking** — and nothing tied that
address to the person asking. Anyone could make our server send mail to any
address they typed in.

Now the code request needs a pass, and we ignore whatever contact is in the
message and use the one saved on that account. There is no way to point it at
somebody else. The trade is that an account exists a bit earlier, before its
contact is checked — which is why an unchecked account cannot log back in.

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
| "Must be verified" for future endpoints | [`permissions.py`](../apps/accounts/permissions.py) |
| The web addresses (URLs) | [`urls.py`](../apps/accounts/urls.py) |

---

## 🧱 What we save about a user

**A member (ConsumerAccount):** name, phone, email, password, "is phone
verified?", "is email verified?", "is the account verified?", "accepted terms?"

**A code (OtpCode):** the destination, its type, the purpose, the code, wrong
tries, and when it expires.

> We never save the real password or the real code. We save a **scrambled**
> version. Even we cannot read them. We only check "does it match?"

---

## 🧪 Try it yourself

Start the app: `python manage.py runserver`
In dev, `OTP_SENDER=console`, so the code is printed in that terminal (no real
WhatsApp or email needed).

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1. Register. You get the passes here, at the START.
curl -X POST $BASE/auth/register -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678",
  "full_name":"Kevin","password":"Str0ng!Pass","confirm_password":"Str0ng!Pass",
  "gender":"male"}'
# You get back: {"detail":"Registration successful. Please verify your account
#                to continue.","access":"...","refresh":"..."}

TOKEN=paste-the-access-value-here

# 2. Ask for a code. Note the pass: this needs you to be logged in.
#    Watch the terminal for:  >>> OTP for +8801712345678 [phone]: 481920
curl -X POST $BASE/auth/otp/request -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","purpose":"register"}'

# 3. Verify with the code you saw
curl -X POST $BASE/auth/otp/verify -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","purpose":"register","code":"481920"}'
# You get back: {"detail":"Account verified successfully.","account_exists":true}

# 4. Later, log in with just the password (works now that you are verified)
curl -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "destination_type":"phone","destination":"01712345678","password":"Str0ng!Pass"}'
```

In steps 2 and 3 the `destination` you send is checked for shape and then
thrown away — we use the contact saved on your account. Try putting a friend's
number there: the code still goes to yours.

Or open `http://127.0.0.1:8000/api/docs/` in a browser.

Note: `01712345678` is cleaned to `+8801712345678` (Bangladesh) before we use it.

---

## 📋 All the endpoints

All start with `/api/v1/`.

| What it does | Address | Need a pass? |
|---|---|---|
| Create the account | `POST /auth/register` | No |
| Log in | `POST /auth/login` | No |
| Ask for a code | `POST /auth/otp/request` | **Yes** |
| Send the code again (same as request) | `POST /auth/otp/resend` | **Yes** |
| Verify the code | `POST /auth/otp/verify` | **Yes** |
| Get a new access pass | `POST /auth/token/refresh` | Refresh pass |
| Log out | `POST /auth/logout` | Yes |
| See / edit my profile | `GET` or `PATCH /auth/me` | Yes |

`/auth/me` works even before you are verified, on purpose: the app reads it when
it opens to decide whether to show you the "verify your account" screen.

---

## 🛡️ Why we added safety (simple words)

- **The code only goes to your own contact.** You cannot make us send a code to
  a phone or email that is not on your account. This is the big one.
- **A code dies in 5 minutes.** Old codes cannot be reused.
- **A code works once.** Asking for a new one cancels the old one.
- **Only 5 wrong tries.** A thief cannot keep guessing, and the wrong-try count
  sticks even when we return the error.
- **Wait 60 sec for a new code**, plus hourly and daily limits per contact and
  per IP (kept in Redis). Nobody can spam a phone.
- **A bad login always looks the same.** Wrong password and "no such account"
  give the exact same answer, so nobody can go fishing for who is registered.
- **You cannot log back in until you verify.** A half-finished account cannot be
  used from a new device.

---

## 📨 About sending codes

- **Phone: WhatsApp only.** Phone codes go through the WhatsApp Cloud API. There is no SMS.
- **Email:** sent through the normal email server in production.
- **Dev:** keep `OTP_SENDER=console` (the local default) and codes print to the terminal.

Details live in [`notifications.py`](../apps/accounts/notifications.py) and the settings files.

---

## 🔌 Room left for later

- **Google / Apple login, points, push, guest mode:** space is left for these.
- **Requiring verification for real actions** (booking, paying, reviewing):
  the `IsVerified` permission in [`permissions.py`](../apps/accounts/permissions.py)
  is written and waiting. Nothing uses it yet.

---

## ✅ In one line

Register to create the account and get your passes ➜ ask for a code, which only
ever goes to your own contact ➜ type it back to become verified ➜ log in with
the password after that. That is the whole thing. 🙌

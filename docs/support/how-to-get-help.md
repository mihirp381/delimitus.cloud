# How to get help

If something is wrong with your app, or you are stuck, write to a person at the support inbox: **<support_email>**.

Replace `<support_email>` with the address from the `support_email` setting before this page goes to a builder.

## What to send

- The app's name and whether it is prod or preview.
- What you did and what you saw, with the time and your time zone.
- The deploy id, if a deploy is involved.
- Any error code on the screen, such as `DB_TIER_FULL`.

Never send a password, a token or a secret value. We never need one.

## Before you write

- **The first load is slow.** Apps sleep when nobody uses them, and wake in a few seconds. After that they are fast. Ask your org admin about the warm option for an app people open every day.
- **A dashboard reloaded after an hour.** A session app's connection ends at 60 minutes. Keep what matters in the database or in the page's URL.
- **A deploy is waiting.** The first deploy that needs a database waits while the database is created, a few minutes.
- **`DB_TIER_FULL`.** The company's database is full. Your org admin can ask for the bigger database.

## What to expect

If many people are affected, we send a status note to your admin first, and update it at the time it names, even when there is no news.

If an app is leaking data or running up cost, say so in the subject line.

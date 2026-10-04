# SSC-049 approval email

Approvers get an email when a request arrives, a digest at 07:00 UTC, one reminder after three days, and the requester gets the decision. The worker sends it. The `notification_outbox` table holds no address and no body: both are built when the mail is sent, so a person who left is never mailed.

## Before you start

- A mail supplier account with SMTP submission, and a sending domain (`delimitus.com`) with SPF and DKIM set up at the supplier. Add the supplier to the trust pack (SSC-058) as a supplier of email.
- An address to send from, for example `approvals@delimitus.com`.
- The console's public https URL. Each mail links to `<url>/approvals/<id>`; there is no approve-by-link.

## 1. Set the worker's environment

| Name | Value |
|---|---|
| `SSC_MAIL_TRANSPORT` | `smtp` |
| `SSC_SMTP_HOST` | the supplier's submission host |
| `SSC_SMTP_TLS` | `starttls` (default, port 587) or `tls` (port 465); there is no plaintext setting |
| `SSC_SMTP_PORT` | only to override the port |
| `SSC_SMTP_USER` | the supplier login |
| `SSC_SMTP_PASSWORD` | from the secret store, never a file or a command line |
| `SSC_MAIL_FROM` | the from address |
| `SSC_CONSOLE_URL` | `https://...` |

Outside dev and tests, an unset `SSC_MAIL_TRANSPORT` sends nothing: mail waits in the outbox and the worker logs a warning. The log mailer (`log`) is refused there. The worker refuses to start with `smtp` and a missing value.

## 2. Check it

1. Ask for an `exceed_ceiling` request as a builder (share an app beyond a connection's ceiling).
2. The connection's owner and every active org admin, not the asker, get the mail within a minute (`notify:tick` runs every minute).
3. Approve it in the console with a reason. The asker gets the decision mail, with the reason.
4. The mail has names, what was asked, who asked and the link. If it has anything else, stop and report it.

## When mail does not arrive

- `select state, count(*) from ssc.notification_outbox group by state;` (as an admin of the database, no address is stored). `pending` rows with `attempts > 0` are retrying with backoff; after five tries a row is `failed` and is not retried. Failed mail never blocks an approval, so the request still sits in the console inbox.
- The worker log names the error class only. It never shows an address, a body or the password.
- To send again after fixing the supplier, set the failed rows back to `pending` with `attempts = 0`.

## Rotating the password

Change the secret, restart the worker. Mail queued meanwhile waits and goes out after the restart.

# Order tracking website: setup

Customers open a link (or scan the QR code on their delivery note) and see where their order is:
**Order received → Being prepared → Dispatched → On the way → Delivered**, plus the border for cross-border orders.
They can confirm they received it and rate the delivery. They never see prices or other customers.

Staff update deliveries in the desktop app: **Dispatch > Order tracking**.

## 1. Update the database (once)
In INVENTRA: **Settings > Business data > Fix database access**. Paste in Supabase (Ctrl+V) and click **Run**.
This adds the `order_tracking` table. Until then customers only see "received" and "dispatched".

## 2. Put the website online (same way as the clock-in app)
1. Create a new **private** GitHub repository (for example `inventra-track`).
2. Upload **every file in this folder** (*Add file → Upload files*, drag them in, *Commit changes*).
3. On your host (e.g. Render): **New → Web Service**, connect the repository.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python track_server.py`
4. Under **Environment**, add:
   - `SUPABASE_URL` and `SUPABASE_KEY`: the **same** values as the clock-in app (the tracking codes depend on the key)
   - `COMPANY_NAME`: your company name
   - `COMPANY_PHONE`, `COMPANY_EMAIL`, `COMPANY_WHATSAPP` (e.g. `27821234567`): shown as "Call us / E-mail / WhatsApp"
5. When it is live, open `https://YOUR-LINK/api/health`: it should say `"ok": true`.

## 3. Tell INVENTRA the link
Dispatch > Order tracking > **Website…** → paste the link (e.g. `https://inventra-track.onrender.com`) → Save.
From then on:
- **Copy link / Send to customer** on the Order tracking page gives each order its own link.
- Delivery notes print a QR code to that order's tracking page.

## How customers find their order
- With the link or QR code: nothing to type.
- Without it: they type the order number and the phone number or e-mail address on their customer record
  (Sales > Customers). After 8 wrong tries from one place they must wait 15 minutes.

## Updating files later
Upload the changed files to the same repository; the host restarts by itself. `tracking_domain.py` must be the same
file as `tracking_domain.py` in the INVENTRA folder.

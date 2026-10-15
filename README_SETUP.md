# Order tracking website: setup

Customers open a link (or scan the QR code on their delivery note) and see where their order is:
**Order received → Being prepared → Dispatched → On the way → Delivered**, plus the border for cross-border orders.
They can confirm they received it and rate the delivery. They never see prices or other customers.

Staff follow every order in the desktop app **Order Tracking** (its own card on the Home screen).
Drivers record their deliveries on their phones at **YOUR-LINK/driver**.

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
Order Tracking > Tracking website & drivers > **Set the link…** → paste the link (e.g. `https://inventra-track.onrender.com`) → Save.
From then on:
- **Copy link / Send to customer** on the Order tracking page gives each order its own link.
- Delivery notes print a QR code to that order's tracking page.

## How customers find their order
- With the link or QR code: nothing to type.
- Without it: they type their order number. After 8 wrong numbers from one place they must wait 15 minutes.

## Updating files later
Upload the changed files to the same repository; the host restarts by itself. `tracking_domain.py` must be the same
file as `tracking_domain.py` in the INVENTRA folder.

## Drivers (proof of delivery on the phone)
1. Order Tracking > **Tracking website & drivers** > **+ Add driver** (or use the drivers from Transport > Fleet).
2. Double-click the driver > **Set PIN** (4 to 8 digits). Tell the driver in person.
3. The driver opens **YOUR-LINK/driver** on their phone, signs in with their name and PIN, and adds it to the
   Home Screen. Their trips appear once the office plans them (Order Tracking > **Delivery trips**).
4. On the road they tap **Everything is loaded**, **I'm leaving now** (from then on the phone shares the
   vehicle's position: keep the page open), then per stop **I've arrived** and
   **Delivered** (name, condition, signature on the screen, photo) or **Delivery failed** (why).
   The customer's tracking page and the office see each step at once.

## Warehouse on the phone (scanning)
Warehouse staff open **YOUR-LINK/warehouse** on a phone (or a cheap Android scanner) and sign in with their normal
INVENTRA username and password. What they can do follows their job role:
- **Pick & pack** (Dispatch officer, Logistics officer, Stock controller): the pick list with bins; scan each product
  or serial number; wrong items, wrong serials and short picks are refused; "Packed" tells the customer and fills in
  the serial numbers when the order is dispatched in INVENTRA.
- **Goods received** (Stock controller, Logistics officer): scan what arrived against the purchase order; shortages,
  extras and damage become exceptions in the Control Tower.
- **Put away / move** (Stock controller): scan the product, then the bin.
- **Count a bin** (Stock controller): differences go to Stock Control for approval.
- **Look up**: scan anything to see what and where it is.
The camera scans barcodes on Android (Chrome) and iPhone; a USB or Bluetooth scanner also works (it types into the box).
Print bin labels with their bin code as a barcode or QR code.

## Suppliers and customer quotes
- **Supplier portal**: Purchasing > Suppliers > right-click > **Supplier portal link** gives each supplier a private
  link (YOUR-LINK/s/...). They confirm delivery dates, upload invoices and answer quote requests online.
- **Customer quotes**: Sales > Quotes > send the quote; the customer opens YOUR-LINK/q/... and accepts it: the order is
  created at once.

## Live vehicle positions (GPS)
A map can only show where a vehicle is if GPS sends its position. Two ways:
- **The driver's phone**: while a trip is out, the /driver page shares the position (keep the page open).
- **A GPS tracker per vehicle**: in INVENTRA, Transport > Vehicle tracking > **Connect a GPS tracker…**, double-click
  the vehicle to make its tracker id. Then either install the free **Traccar Client** app on a phone that stays in
  the vehicle (Server URL `YOUR-LINK/api/gps`, Device identifier = the tracker id, frequency 30 seconds), or ask your
  GPS tracking company to forward positions with the **OsmAnd** protocol to `YOUR-LINK/api/gps`.

## Files to upload
Easiest: INVENTRA > Order Tracking > Tracking website & drivers > **Check it**, then **Update the website…** >
**Put the files in a folder**. It gathers exactly the files that differ from the website.
The full list: `track_server.py`, `gps.py`, `delivery_proof.py`, `web_files.py`, `warehouse.py`, `supplier.py`, `quotes_web.py`, `supplier_page.html`, `quote_page.html`, `track_page.html`, `driver_page.html`, `warehouse_page.html`, `tracking_domain.py`,
`inventra_stock.py`, `inventra_sku.py`, `inventra_bins.py`, `inventra_quality.py`, `sales_quotes.py`, `requirements.txt`, `Procfile`,
`manifest.json`, `logo.png`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`.
The shared files come from the INVENTRA folder: run `python sync_web_files.py` there first so they are up to date.

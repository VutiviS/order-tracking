"""The customer's quote page (/q/<quote id>-<code> on the tracking site): see the quote, accept it (an order is created
at once, and the page then follows that order) or decline it with a reason. Rules in sales_quotes.py.
Mixed into the tracking site's Tracker (needs self.db, self.secret, self._limit, self._failed)."""

from datetime import datetime

import sales_quotes as SQ
import tracking_domain as TD


class QuoteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class QuotePortal:
    def _quote(self, key: str, ip: str) -> dict:
        self._limit("q:" + ip)
        qid = SQ.from_key(self.secret, key)
        rows = self.db.table(SQ.TABLE).select("*").eq("quote_id", qid).execute().data if qid else []
        if not rows:
            self._failed("q:" + ip)
            raise QuoteError("This quote link isn't valid. Ask us to send it again.", 404)
        return rows[0]

    def quote_view(self, key: str, ip: str) -> dict:
        q = self._quote(key, ip)
        status = "Expired" if SQ.expired(q) else q.get("status")
        out = {"quote_id": q["quote_id"], "client": q.get("client_name"), "status": status,
               "valid_until": str(q.get("valid_until") or "")[:10], "notes": q.get("notes") or "",
               "lines": [{"description": l.get("description") or l.get("sku"), "qty": SQ.num(l.get("qty")),
                          "price": SQ.num(l.get("price")), "total": round(SQ.num(l.get("qty")) * SQ.num(l.get("price")), 2)}
                         for l in q.get("lines") or []],
               "totals": SQ.totals(q), "accepted_by": q.get("accepted_by"), "order_id": q.get("order_id")}
        if q.get("order_id"):
            out["track"] = f"{q['order_id']}-{TD.track_code(self.secret, q['order_id']).replace('-', '')}"
        return out

    def quote_accept(self, body: dict, ip: str) -> dict:
        q = self._quote(body.get("key"), ip)
        try:
            done, rows = SQ.accept(q, body.get("name") or "", body.get("ref") or "", datetime.now().replace(microsecond=0))
        except ValueError as e:
            raise QuoteError(str(e), 409)
        self.db.table("customer_order").insert(rows).execute()
        self.db.table(SQ.TABLE).update({k: done[k] for k in ("status", "order_id", "accepted_by", "customer_ref",
                                                             "accepted_at", "updated_at")}).eq("quote_id", q["quote_id"]).execute()
        return dict(self.quote_view(body.get("key"), ip),
                    message=f"Thank you! Your order {done['order_id']} has been placed. Follow it on this page.")

    def quote_decline(self, body: dict, ip: str) -> dict:
        q = self._quote(body.get("key"), ip)
        if q.get("status") not in SQ.OPEN:
            raise QuoteError("This quote can't be changed any more.", 409)
        self.db.table(SQ.TABLE).update({"status": "Declined", "decline_reason": str(body.get("reason") or "")[:300],
                                        "updated_at": datetime.now().isoformat(timespec="seconds")}) \
            .eq("quote_id", q["quote_id"]).execute()
        return dict(self.quote_view(body.get("key"), ip), message="Thank you for letting us know.")

"""Local stand-in for the Stripe test API. Not Stripe. Used only to exercise
Archisynapse's StripeTestProcessor code path without a Stripe key."""
import json, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

SEEN = {}

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        key = (self.path, self.headers.get("Idempotency-Key"))
        if key not in SEEN:
            if self.path.endswith("/payment_intents"):
                SEEN[key] = {"id": f"pi_stub_{uuid.uuid4().hex[:16]}", "object": "payment_intent",
                             "status": "succeeded", "amount": int(body["amount"][0])}
            elif self.path.endswith("/refunds"):
                SEEN[key] = {"id": f"re_stub_{uuid.uuid4().hex[:16]}", "object": "refund",
                             "status": "succeeded", "payment_intent": body["payment_intent"][0]}
            else:
                self.send_response(404); self.end_headers(); return
        out = json.dumps(SEEN[key]).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", 12111), H).serve_forever()

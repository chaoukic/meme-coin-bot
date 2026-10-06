"""Jupiter Trigger Order API V2 client for exchange-side stop-losses on REAL positions (added Oct 2 2026, trial).

Never logs or prints the wallet key, the API key or the JWT/refresh tokens. All calls go through the 'jup' rate
limiter (common.LIMITS) so they share the scanning budget and leave headroom for swaps.
Flow (docs: developers.jup.ag/docs/trigger): auth challenge -> sign message with the wallet -> verify (access_refresh)
-> GET /vault (register once, an API call with no transaction) -> POST /deposit/craft -> sign deposit tx ->
POST /orders/price (lands the deposit) ; PATCH /orders/price/{id} ; POST cancel/{id} -> sign withdrawal ->
POST confirm-cancel/{id} ; GET /orders/history.
"""
import base64, threading, time
import requests
from common import LIMITS, CALLS, log

BASE = "https://api.jup.ag/trigger/v2"
MIN_ORDER_USD = 10.0          # Jupiter rejects price orders worth less than 10 USD (at deposit time)
_S = requests.Session()
_AUTH = {"access": None, "refresh": None, "exp": 0.0}
_ALOCK = threading.Lock()

class TriggerError(RuntimeError):
    def __init__(self, msg, status=None, body=None):
        super().__init__(msg); self.status = status; self.body = body

def _api_key():
    import live
    return live.jup_key()

def _wallet():
    import live
    return live.keypair(), live.pubkey()

def _req(method, path, auth=True, json_body=None, params=None, retry_auth=True):
    hdr = {"x-api-key": _api_key() or "", "Content-Type": "application/json"}
    if auth:
        hdr["Authorization"] = "Bearer " + _token()
    for attempt in range(3):
        LIMITS["jup"].wait()
        CALLS["jup"] = CALLS.get("jup", 0) + 1
        try:
            r = _S.request(method, BASE + path, headers=hdr, json=json_body, params=params, timeout=30)
        except requests.RequestException as e:
            if attempt == 2:
                raise TriggerError(f"{method} {path}: network error {type(e).__name__}")
            time.sleep(2 + 2 * attempt); continue
        if r.status_code == 429:
            CALLS["jup_429"] = CALLS.get("jup_429", 0) + 1
            LIMITS["jup"].backoff(10 * (attempt + 1)); continue
        if r.status_code == 401 and auth and retry_auth:
            _AUTH.update(access=None, exp=0)
            hdr["Authorization"] = "Bearer " + _token()
            retry_auth = False
            continue
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        if r.status_code >= 400:
            msg = body.get("error") or body.get("message") if isinstance(body, dict) else None
            raise TriggerError(f"{method} {path} -> HTTP {r.status_code}: {str(msg or body)[:300]}", r.status_code, body)
        return body
    raise TriggerError(f"{method} {path}: rate limited")

# ---------------------------------------------------------------- auth
def _login():
    kp, pub = _wallet()
    ch = _req("POST", "/auth/challenge", auth=False, json_body={"walletPubkey": pub, "type": "message"})
    text = ch.get("challenge") or ""
    if pub not in text or "Jupiter" not in text:   # only sign a genuine Jupiter login message for this wallet
        raise TriggerError("unexpected auth challenge text, not signing")
    sig = kp.sign_message(text.encode())
    v = _req("POST", "/auth/verify", auth=False, json_body={"type": "message", "walletPubkey": pub, "signature": str(sig),
                                                             "authMode": "access_refresh"})
    _set_tokens(v)

def _set_tokens(v):
    acc = v.get("accessToken") or v.get("token")
    if not acc:
        raise TriggerError("auth verify returned no token")
    exp = time.time() + 14 * 60
    _AUTH.update(access=acc, refresh=v.get("refreshToken"), exp=exp)

def _token():
    with _ALOCK:
        if _AUTH["access"] and time.time() < _AUTH["exp"] - 30:
            return _AUTH["access"]
        if _AUTH["refresh"]:
            try:
                _set_tokens(_req("POST", "/auth/refresh", auth=False, json_body={"refreshToken": _AUTH["refresh"]}))
                return _AUTH["access"]
            except TriggerError:
                _AUTH.update(refresh=None)
        _login()
        return _AUTH["access"]

# ---------------------------------------------------------------- vault / signing
def vault():
    try:
        return _req("GET", "/vault")
    except TriggerError as e:
        if e.status != 404:
            raise
    try:
        return _req("GET", "/vault/register")
    except TriggerError as e:
        if e.status == 409 and isinstance(e.body, dict) and e.body.get("details"):
            return e.body["details"]
        raise

def sign_tx_b64(tx_b64):
    """Add the wallet's signature to a base64 VersionedTransaction (keeps any other signatures)."""
    from solders.transaction import VersionedTransaction
    from solders.message import to_bytes_versioned
    kp, pub = _wallet()
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    keys = list(tx.message.account_keys)
    idx = [str(k) for k in keys].index(pub)
    n_sig = tx.message.header.num_required_signatures
    if idx >= n_sig:
        raise TriggerError("wallet is not a required signer of this transaction")
    sigs = list(tx.signatures)
    sigs[idx] = kp.sign_message(to_bytes_versioned(tx.message))
    tx.signatures = sigs
    return base64.b64encode(bytes(tx)).decode()

# ---------------------------------------------------------------- orders
def craft_deposit(mint, out_mint, amount_raw):
    _, pub = _wallet()
    return _req("POST", "/deposit/craft", json_body={"inputMint": mint, "outputMint": out_mint, "userAddress": pub,
                                                    "amount": str(int(amount_raw)), "orderType": "price", "orderSubType": "single"})

def place_stop(mint, out_mint, amount_raw, trigger_usd, slippage_bps, expires_ms):
    """Sell-below stop for amount_raw of `mint`. Returns {'id', 'txSignature', 'depositConfirmed'}."""
    _, pub = _wallet()
    vault()
    dep = craft_deposit(mint, out_mint, amount_raw)
    if not dep.get("requestId") or not dep.get("transaction"):
        raise TriggerError(f"deposit craft returned no transaction: {str(dep)[:200]}")
    signed = sign_tx_b64(dep["transaction"])
    return _req("POST", "/orders/price", json_body={
        "orderType": "single", "depositRequestId": dep["requestId"], "depositSignedTx": signed, "userPubkey": pub,
        "inputMint": mint, "outputMint": out_mint, "inputAmount": str(int(amount_raw)), "triggerMint": mint,
        "triggerCondition": "below", "triggerPriceUsd": float(trigger_usd), "slippageBps": int(slippage_bps),
        "expiresAt": int(expires_ms)})

def update_stop(order_id, trigger_usd, slippage_bps=None):
    body = {"orderType": "single", "triggerPriceUsd": float(trigger_usd)}
    if slippage_bps is not None:
        body["slippageBps"] = int(slippage_bps)
    return _req("PATCH", f"/orders/price/{order_id}", json_body=body)

def cancel_withdraw(order_id):
    """Two-step cancel: stops the order at once (ready_to_cancel), then signs + confirms the withdrawal to the wallet."""
    c = _req("POST", f"/orders/price/cancel/{order_id}")
    if not c.get("transaction"):
        raise TriggerError(f"cancel returned no withdrawal transaction: {str(c)[:200]}")
    signed = sign_tx_b64(c["transaction"])
    last = None
    for i in range(3):   # confirm can be retried with the same cancelRequestId
        try:
            return _req("POST", f"/orders/price/confirm-cancel/{order_id}",
                        json_body={"signedTransaction": signed, "cancelRequestId": c.get("requestId")})
        except TriggerError as e:
            last = e; time.sleep(3 + 3 * i)
    raise last

def order(order_id, mint=None):
    """The order record from /orders/history (active first, then past), or None."""
    for state in ("active", "past"):
        params = {"state": state, "limit": 50}
        if mint:
            params["mint"] = mint
        h = _req("GET", "/orders/history", params=params)
        for o in h.get("orders") or []:
            if o.get("id") == order_id:
                return o
    return None

def fill_summary(o):
    """(filled_fraction, output_raw_total, fill_tx) from an order record."""
    out, sig = 0, None
    for ev in o.get("events") or []:
        if ev.get("type") == "fill" and ev.get("state", "success") == "success":
            out += int(ev.get("outputAmount") or 0); sig = ev.get("txSignature") or sig
    if not out and o.get("outputAmount"):
        out = int(o["outputAmount"])
    return float(o.get("fillPercent") or 0), out, sig

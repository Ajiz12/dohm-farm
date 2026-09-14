#!/usr/bin/env python3
"""
DOHM Farming v20 - SEQUENTIAL (no thread)
Flow per cycle:
  stake 0.2 -> jeda 5-15s -> unstake 0.1 -> jeda 5-15s
  -> claim (same thread) -> next cycle

Camoufox/playwright CANNOT be used from multiple threads.
All ops sequential in MainThread. Health check via file-based heartbeat.
Auto-restart via supervisor. Hourly Telegram report.
"""
import os
import sys
import time
import re
import random
import fcntl
import hashlib
import traceback
import urllib.request
import urllib.parse
from datetime import datetime

from camoufox.sync_api import Camoufox

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════
WALLET_PASSWORD = os.environ.get('WALLET_PASSWORD')
SEED_PHRASE     = os.environ.get('SEED_PHRASE')
if not WALLET_PASSWORD or not SEED_PHRASE:
    print("[FATAL] WALLET_PASSWORD & SEED_PHRASE wajib di-set.")
    sys.exit(1)

POINTS_TARGET = float(os.environ.get('POINTS_TARGET', '25000'))
MAX_CYCLES    = int(os.environ.get('MAX_CYCLES', '0'))
STAKE_AMOUNT  = float(os.environ.get('STAKE_AMOUNT', '0.2'))
UNSTAKE_AMOUNT= float(os.environ.get('UNSTAKE_AMOUNT', '0.1'))

JEDA_MIN = int(os.environ.get('JEDA_MIN', '5'))
JEDA_MAX = int(os.environ.get('JEDA_MAX', '15'))

TX_WAIT_MAX      = int(os.environ.get('TX_WAIT_MAX', '900'))
TX_POLL_INTERVAL = int(os.environ.get('TX_POLL_INTERVAL', '5'))

GRACE_MIN = int(os.environ.get('GRACE_MIN', '10'))
GRACE_MAX = int(os.environ.get('GRACE_MAX', '20'))

CLAIM_MAX_WAIT = int(os.environ.get('CLAIM_MAX_WAIT', '300'))

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
NOTIFY_EVERY_PTS   = float(os.environ.get('NOTIFY_EVERY_PTS', '25'))

FAUCET_ENABLED   = int(os.environ.get('FAUCET_ENABLED', '1'))
FAUCET_MIN_HOURS = float(os.environ.get('FAUCET_MIN_HOURS', '6'))
FAUCET_MAX_HOURS = float(os.environ.get('FAUCET_MAX_HOURS', '12'))

AUTO_UPDATE = int(os.environ.get('AUTO_UPDATE', '0'))
UPDATE_URL  = os.environ.get('UPDATE_URL', '')

URL_STAKE     = 'https://testnet.dohm.finance/app/stake'
URL_PORTFOLIO = 'https://testnet.dohm.finance/app/portfolio'
URL_SETUP     = 'https://testnet.dohm.finance/app/setup'
LOCK_FILE     = '/tmp/dohm_farm.lock'
LOG_FILE      = '/tmp/dohm_farm.log'
HEARTBEAT_FILE= '/tmp/dohm_farm.heartbeat'
SCRIPT_PATH   = os.path.abspath(__file__)

# ═══════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════
state = {
    'stop': False,
    'initial_points': 0.0,
    'last_notify_pts': 0.0,
    'last_hourly_pts': 0.0,
    'last_hourly_time': 0.0,
    'claim_count': 0,
}
start_time = time.time()
_last_heartbeat = 0

# ═══════════════════════════════════════════
# LOG + NOTIF
# ═══════════════════════════════════════════
_log_lock = __import__('threading').Lock()

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] [MAIN] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line + '\n')
        except Exception:
            pass

def notify(msg):
    log(f"[NOTIF] {msg}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text': msg,
            'parse_mode': 'HTML',
            'disable_web_page_preview': 'true',
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        log(f"[NOTIF] gagal: {e}")

def heartbeat(payload: str):
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < 10:
        return
    _last_heartbeat = now
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(f"{datetime.now().isoformat()} | {payload}\n")
    except Exception:
        pass

# ═══════════════════════════════════════════
# AUTO UPDATE
# ═══════════════════════════════════════════
def check_and_update():
    if not AUTO_UPDATE or not UPDATE_URL:
        return False
    try:
        log(f"[UPDATE] cek {UPDATE_URL}")
        req = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'dohm-farm'})
        with urllib.request.urlopen(req, timeout=20) as r:
            remote = r.read()
        local = open(SCRIPT_PATH, 'rb').read()
        if hashlib.sha256(remote).hexdigest() == hashlib.sha256(local).hexdigest():
            log("[UPDATE] sudah terbaru")
            return False
        with open(SCRIPT_PATH + '.bak', 'wb') as f:
            f.write(local)
        with open(SCRIPT_PATH, 'wb') as f:
            f.write(remote)
        os.chmod(SCRIPT_PATH, 0o755)
        notify("🔄 <b>Auto-update</b> — restart otomatis...")
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log(f"[UPDATE] error: {e}")
    return False

# ═══════════════════════════════════════════
# LOCK
# ═══════════════════════════════════════════
def acquire_lock():
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        log("[LOCK] Script lain masih jalan.")
        return None

# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════
def sleep_fixed(min_s=5, max_s=15, label=""):
    total = random.uniform(min_s, max_s)
    log(f"  [jeda] {label}: {total:.1f}s")
    end = time.time() + total
    while time.time() < end and not state['stop']:
        time.sleep(min(1, end - time.time()))

def get_tx_hash(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(0x[a-fA-F0-9]{64})', r'([a-f0-9]{8,}\.[a-f0-9]{4,})', r'\b([a-f0-9]{64})\b'):
            m = re.findall(pat, txt)
            if m:
                return m[-1]
    except Exception:
        pass
    return None

def read_points_no_nav(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*pts', r'[Pp]oints?\s*[:=]?\s*(\d+\.?\d*)'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None

def get_points(page):
    try:
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(3)
            pts = read_points_no_nav(page)
            if pts and pts > 0:
                return pts
        return 0
    except Exception as e:
        log(f"  [get_points error] {e}")
        return 0

# ═══════════════════════════════════════════
# WALLET
# ═══════════════════════════════════════════
def check_wallet_status(page):
    try:
        if page.locator('button:has-text("Connect wallet"):visible').count() > 0:
            return 'needs_connect'
        try:
            body = page.inner_text('body')
        except Exception:
            return 'needs_connect'
        if re.search(r'\bbcrt1q[a-z0-9]{20,}\b', body) or re.search(r'\b0x[a-fA-F0-9]{40}\b', body):
            return 'connected'
        for label in ['Stake DOHM', 'Unstake DOHM']:
            if page.locator(f'button:has-text("{label}"):not([disabled]):visible').count() > 0:
                return 'connected'
        if page.locator('input[type="password"]').count() > 0:
            return 'needs_unlock'
    except Exception:
        pass
    return 'needs_connect'

def unlock_wallet(page):
    try:
        pw = page.locator('input[type="password"]')
        if pw.count() > 0:
            log("  [wallet] unlocking...")
            pw.fill(WALLET_PASSWORD)
            time.sleep(1)
            u = page.locator('button:has-text("Unlock"):visible')
            if u.count() > 0:
                u.first.click()
                time.sleep(4)
    except Exception as e:
        log(f"  [wallet] unlock err: {e}")

def restore_wallet(page):
    log("  [wallet] clicking Connect wallet...")
    try:
        page.locator('button:has-text("Connect wallet"):visible').first.click()
    except Exception:
        return False

    # Tunggu modal muncul (max 10s)
    log("  [wallet] tunggu modal...")
    for _ in range(10):
        time.sleep(1)
        r = page.locator('button:has-text("Restore from recovery phrase"):visible')
        if r.count() > 0:
            break

    r = page.locator('button:has-text("Restore from recovery phrase"):visible')
    if r.count() == 0:
        # Coba alternatif selector
        r2 = page.locator('text=/restore.*recovery/i')
        if r2.count() == 0:
            log("  [wallet] 'Restore from recovery phrase' ga ketemu")
            return False
        r = r2

    log("  [wallet] klik 'Restore from recovery phrase'")
    r.first.click()
    time.sleep(3)

    # Fill seed phrase
    ta = page.locator('textarea')
    if ta.count() > 0:
        log("  [wallet] filling seed phrase...")
        ta.fill(SEED_PHRASE)
        time.sleep(1)

    # Fill password
    pw = page.locator('input[type="password"]')
    if pw.count() > 0:
        log("  [wallet] filling password...")
        pw.fill(WALLET_PASSWORD)
        time.sleep(1)

    # Click Restore
    rb = page.locator('button:has-text("Restore"):visible')
    if rb.count() > 0:
        log("  [wallet] klik 'Restore'...")
        rb.first.click()
        time.sleep(12)

    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    time.sleep(8)
    result = check_wallet_status(page)
    log(f"  [wallet] restore result: {result}")
    return result == 'connected'

def ensure_wallet(page):
    s = check_wallet_status(page)
    log(f"  [wallet] status: {s}")
    if s == 'connected':
        return True
    if s == 'needs_unlock':
        unlock_wallet(page)
        return check_wallet_status(page) == 'connected'
    if restore_wallet(page):
        return True
    unlock_wallet(page)
    return check_wallet_status(page) == 'connected'

# ═══════════════════════════════════════════
# FORM + RETRY
# ═══════════════════════════════════════════
def fill_amount(page, amount):
    inp = page.locator('input:visible:not([disabled])').first
    if inp.count() == 0:
        return 'none'
    inp.click()
    inp.fill('')
    time.sleep(0.3)
    inp.fill(str(amount))
    time.sleep(2)
    return inp.input_value()

def verify_tab(page):
    a = page.locator('button[aria-selected="true"]').first
    return a.inner_text().strip() if a.count() > 0 else 'none'

def click_confirm_sign(page, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        b = page.locator('button:has-text("Confirm & sign"):not([disabled]):visible')
        if b.count() > 0:
            try:
                b.first.scroll_into_view_if_needed()
                b.first.click()
                time.sleep(4)
                return 'confirmed'
            except Exception:
                pass
        if get_tx_hash(page):
            return 'confirmed'
        time.sleep(1)
    return 'no-confirm'

def retry_action(fn, tries=3, base_delay=5, label=""):
    for i in range(tries):
        if state['stop']:
            return 'stopped'
        try:
            r = fn()
            if r not in ('failed-3x', 'no-confirm', None):
                return r
            log(f"  [retry:{label}] {i + 1}/{tries} -> {r}")
        except Exception as e:
            log(f"  [retry:{label}] {i + 1}/{tries} err: {e}")
        if i < tries - 1:
            time.sleep(base_delay * (2 ** i))
    return 'failed-3x'

# ═══════════════════════════════════════════
# WAIT TX CONFIRM (3-TAHAP)
# ═══════════════════════════════════════════
def wait_tx_confirm(page, action_label="", max_wait=None, poll_interval=None):
    if max_wait is None:
        max_wait = TX_WAIT_MAX
    if poll_interval is None:
        poll_interval = TX_POLL_INTERVAL

    start = time.time()
    last_log = 0

    log(f"  [confirm] {action_label} tahap 1: tunggu PENDING muncul")
    pending_seen = False
    t1_start = time.time()
    while time.time() - t1_start < 30:
        if state['stop']:
            return 'timeout'
        try:
            body = page.inner_text('body') or ''
        except Exception:
            body = ''
        if re.search(r'(pending|in\s*mempool|mempool|submitting|processing|broadcasting)',
                     body, re.IGNORECASE):
            pending_seen = True
            m = re.search(r'pending[^\n]{0,80}', body, re.IGNORECASE)
            snippet = m.group(0).strip() if m else 'pending...'
            log(f"  [confirm] {action_label} pending MUNCUL: {snippet[:70]}")
            break
        time.sleep(2)

    if not pending_seen:
        log(f"  [confirm] {action_label} pending ga muncul, skip ke grace")

    log(f"  [confirm] {action_label} tahap 2: tunggu PENDING hilang")
    settled_count = 0
    pending_gone = False
    while time.time() - start < max_wait:
        if state['stop']:
            return 'timeout'
        elapsed = time.time() - start
        try:
            body = page.inner_text('body') or ''
        except Exception:
            body = ''

        is_pending = bool(re.search(
            r'(pending|in\s*mempool|mempool|waiting\s+for\s+confirmation|'
            r'submitting|processing|broadcasting|queued)',
            body, re.IGNORECASE))

        if is_pending:
            settled_count = 0
            if time.time() - last_log > 20:
                m = re.search(r'pending[^\n]{0,80}', body, re.IGNORECASE)
                snippet = m.group(0).strip() if m else 'pending...'
                log(f"  [confirm] {action_label} MASIH PENDING ({elapsed:.0f}s): {snippet[:70]}")
                last_log = time.time()
        else:
            if pending_seen:
                settled_count += 1
                if settled_count >= 2:
                    log(f"  [confirm] {action_label} ✅ PENDING HILANG di {elapsed:.1f}s")
                    pending_gone = True
                    break
            else:
                if elapsed >= 5:
                    log(f"  [confirm] {action_label} ✅ settled (ga ada pending) di {elapsed:.1f}s")
                    pending_gone = True
                    break
        time.sleep(poll_interval)

    if not pending_gone:
        log(f"  [confirm] {action_label} ⚠️ TIMEOUT {max_wait}s")
        return 'timeout'

    grace = random.uniform(GRACE_MIN, GRACE_MAX)
    log(f"  [confirm] {action_label} tahap 3: grace {grace:.0f}s")
    grace_end = time.time() + grace
    while time.time() < grace_end and not state['stop']:
        time.sleep(1)

    log(f"  [confirm] {action_label} ✅ done")
    return 'confirmed'

# ═══════════════════════════════════════════
# ACTIONS
# ═══════════════════════════════════════════
def get_sohm_balance(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*sDOHM', r'[Bb]alance\s+(\d+\.?\d*)\s*sDOHM'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return 0

def do_stake(page, amount=0.2):
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    for _ in range(6):
        time.sleep(3)
        if page.locator('button:has-text("Stake"):visible').count() > 0:
            break
    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Stake':
            t = page.locator('button:has-text("Stake"):visible')
            if t.count() > 0:
                t.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Stake':
            time.sleep(3)
            continue
        val = fill_amount(page, amount)
        log(f"  input: '{val}'")
        s = page.locator('button:has-text("Stake DOHM"):not([disabled]):visible')
        if s.count() > 0:
            s.first.scroll_into_view_if_needed()
            s.first.click()
            time.sleep(2)
            conf = click_confirm_sign(page)
            log(f"  stake confirm: {conf}")
            return 'done' if conf == 'confirmed' else conf
        time.sleep(2)
    return 'failed-3x'

def do_unstake(page, amount=0.1):
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    for _ in range(6):
        time.sleep(3)
        if page.locator('button:has-text("Unstake"):visible').count() > 0:
            break
    active = verify_tab(page)
    if active != 'Unstake':
        t = page.locator('button:has-text("Unstake"):visible')
        if t.count() > 0:
            t.first.click()
        time.sleep(3)
    bal = get_sohm_balance(page)
    if bal <= 0:
        log(f"  no sDOHM ({bal})")
        return 'skip'
    amt = amount if amount < bal else round(max(bal - 0.001, 0), 4)
    if amt <= 0:
        return 'skip'
    log(f"  sDOHM: {bal:.4f}, unstaking: {amt}")
    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Unstake':
            t = page.locator('button:has-text("Unstake"):visible')
            if t.count() > 0:
                t.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Unstake':
            time.sleep(3)
            continue
        val = fill_amount(page, amt)
        log(f"  input: '{val}'")
        s = page.locator('button:has-text("Unstake DOHM"):visible')
        if s.count() > 0 and s.first.get_attribute('disabled', timeout=1000) is None:
            s.first.scroll_into_view_if_needed()
            s.first.click()
            time.sleep(2)
            conf = click_confirm_sign(page)
            log(f"  unstake confirm: {conf}")
            return 'done' if conf == 'confirmed' else conf
        time.sleep(3)
    return 'failed-3x'

def do_claim(page):
    """Claim semua matured bonds — SEQUENTIAL (same thread)."""
    page.goto(URL_PORTFOLIO, wait_until='load', timeout=30000)
    for _ in range(20):
        time.sleep(1)
        if page.locator('button:has-text("Claim"):visible').count() > 0:
            break
    ensure_wallet(page)
    time.sleep(3)
    total = 0
    for rnd in range(10):
        n = page.locator('button:has-text("Claim"):not([disabled]):visible').count()
        if n == 0:
            break
        log(f"  [claim] round {rnd + 1}: {n} tombol")
        for _ in range(n):
            b = page.locator('button:has-text("Claim"):not([disabled]):visible').first
            if b.count() == 0:
                break
            try:
                b.scroll_into_view_if_needed()
                b.click()
                total += 1
                log(f"  [claim] bond #{total} clicked, tunggu settle...")
                wait_tx_confirm(page, f"claim-{total}", max_wait=CLAIM_MAX_WAIT)
            except Exception as e:
                log(f"  [claim] err: {e}")
                break
        try:
            page.reload(wait_until='load', timeout=30000)
            time.sleep(5)
        except Exception:
            break
    log(f"  [claim] total: {total}")
    return 'done' if total > 0 else 'skip'

# ═══════════════════════════════════════════
# FAUCET
# ═══════════════════════════════════════════
def _safe_click(page, locator, label="", timeout=5000):
    try:
        if locator.count() == 0:
            return False
        el = locator.first
        el.scroll_into_view_if_needed(timeout=timeout)
        disabled = el.get_attribute('disabled', timeout=timeout)
        if disabled is not None:
            log(f"  [faucet] '{label}' disabled")
            return False
        el.click(timeout=timeout)
        return True
    except Exception as e:
        log(f"  [faucet] klik '{label}' err: {e}")
        return False

def _find_visible(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            continue
    return None

def try_claim_faucet(page):
    try:
        log(f"[FAUCET] buka {URL_SETUP}")
        page.goto(URL_SETUP, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(2)
            try:
                if page.locator('button:visible').count() > 0:
                    break
            except Exception:
                pass
        ensure_wallet(page)
        time.sleep(2)

        link_menu = _find_visible(page, [
            'a:has-text("Link")', 'button:has-text("Link")',
            '[role="tab"]:has-text("Link")', 'text="Link"',
        ])
        if not link_menu:
            log("[FAUCET] menu 'Link' ga ketemu")
            return 'not-available'
        log("[FAUCET] klik menu Link")
        try:
            link_menu.scroll_into_view_if_needed()
            link_menu.click()
        except Exception as e:
            log(f"[FAUCET] klik Link err: {e}")
            return 'error'
        time.sleep(3)

        wl = _find_visible(page, [
            'button:has-text("wallet Linked")', 'button:has-text("Wallet Linked")',
            'button:has-text("Linked")', 'text=/wallet\\s+Linked/i', 'text=/Linked/i',
        ])
        if wl:
            log("[FAUCET] klik 'wallet Linked'")
            try:
                wl.scroll_into_view_if_needed()
                wl.click()
                time.sleep(3)
            except Exception as e:
                log(f"[FAUCET] klik wallet Linked err: {e}")
        else:
            log("[FAUCET] tombol 'wallet Linked' ga ketemu (lanjut)")

        cont = _find_visible(page, [
            'button:has-text("Continue")', 'button:has-text("CONTINUE")',
        ])
        if cont:
            log("[FAUCET] klik Continue")
            try:
                cont.scroll_into_view_if_needed()
                cont.click()
                time.sleep(4)
            except Exception as e:
                log(f"[FAUCET] klik Continue err: {e}")
        else:
            log("[FAUCET] tombol Continue ga ketemu (lanjut)")

        time.sleep(3)
        claimed_any = False

        btc_btn = _find_visible(page, [
            'button:has-text("Get BTC")', 'button:has-text("GET BTC")',
            'text=/Get\\s+BTC/i',
        ])
        if btc_btn:
            log("[FAUCET] klik Get BTC")
            if _safe_click(page, btc_btn, "Get BTC"):
                time.sleep(3)
                conf = click_confirm_sign(page, timeout=20)
                tx = get_tx_hash(page)
                log(f"[FAUCET] BTC confirm={conf} tx={tx}")
                if conf == 'confirmed' or tx:
                    claimed_any = True
                time.sleep(5)
        else:
            log("[FAUCET] tombol 'Get BTC' ga ketemu")

        frbtc_btn = _find_visible(page, [
            'button:has-text("Get frBTC")', 'button:has-text("GET frBTC")',
            'button:has-text("frBTC")', 'text=/Get\\s+frBTC/i',
        ])
        if frbtc_btn:
            log("[FAUCET] klik Get frBTC")
            if _safe_click(page, frbtc_btn, "Get frBTC"):
                time.sleep(3)
                conf = click_confirm_sign(page, timeout=20)
                tx = get_tx_hash(page)
                log(f"[FAUCET] frBTC confirm={conf} tx={tx}")
                if conf == 'confirmed' or tx:
                    claimed_any = True
                time.sleep(5)
        else:
            log("[FAUCET] tombol 'Get frBTC' ga ketemu")

        if claimed_any:
            log("[FAUCET] ✅ minimal 1 token ke-claim")
            return 'claimed'

        try:
            body = page.inner_text('body') or ''
            if re.search(r'(cooldown|already|wait|next.*in|come back)', body, re.IGNORECASE):
                log("[FAUCET] cooldown")
                return 'cooldown'
        except Exception:
            pass
        log("[FAUCET] ga ada yang ke-claim")
        return 'not-available'

    except Exception as e:
        log(f"[FAUCET] exception: {e}")
        log(traceback.format_exc())
        return 'error'

# ═══════════════════════════════════════════
# CYCLE RUNNER (SEQUENTIAL — NO THREADS)
# ═══════════════════════════════════════════
def run_farming_session():
    log(f"[INIT] DOHM Farm v20 | {datetime.now()}")
    log(f"[INIT] Target: {POINTS_TARGET} pts | Notif tiap +{NOTIFY_EVERY_PTS} pts")
    log(f"[INIT] Flow: Stake → {JEDA_MIN}-{JEDA_MAX}s → Unstake → {JEDA_MIN}-{JEDA_MAX}s → Claim → next")
    log(f"[INIT] Grace: {GRACE_MIN}-{GRACE_MAX}s | TX_WAIT_MAX={TX_WAIT_MAX}s")
    if FAUCET_ENABLED:
        log(f"[INIT] Faucet: ON (cek tiap {FAUCET_MIN_HOURS}-{FAUCET_MAX_HOURS} jam)")
    notify(f"🚀 <b>DOHM Farm v20 start</b>\nTarget: {POINTS_TARGET} pts\nMode: {'infinite 24/7' if MAX_CYCLES == 0 else f'{MAX_CYCLES} cycles'}")

    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)
        ensure_wallet(page)

        initial_points = get_points(page)
        state['initial_points'] = initial_points
        state['last_notify_pts'] = initial_points
        state['last_hourly_pts'] = initial_points
        state['last_hourly_time'] = time.time()
        log(f"[INIT] Points: {initial_points:.2f} | Target: {POINTS_TARGET}")
        notify(f"📊 <b>Starting points:</b> {initial_points:.2f}")

        cycle_start = time.time()
        cycle_num = 0
        next_faucet_time = time.time()

        while not state['stop']:
            cycle_num += 1
            if MAX_CYCLES > 0 and cycle_num > MAX_CYCLES:
                log(f"[END] Max cycles ({MAX_CYCLES}) reached.")
                state['stop'] = True
                return True

            log(f"\n{'=' * 60}")
            log(f"CYCLE {cycle_num}{'/' + str(MAX_CYCLES) if MAX_CYCLES else ' (infinite)'} | "
                f"{datetime.now().strftime('%H:%M:%S')}")
            log(f"{'=' * 60}")

            pts = get_points(page)
            heartbeat(f"cycle={cycle_num} pts={pts:.2f} claims={state['claim_count']}")

            # ── CEK TARGET ──
            if pts >= POINTS_TARGET:
                msg = (
                    f"🎯 <b>TARGET TERCAPAI!</b>\n"
                    f"Points: <b>{pts:.2f}</b> / {POINTS_TARGET}\n"
                    f"Cycles: {cycle_num}\n"
                    f"Claims: {state['claim_count']}\n"
                    f"Elapsed: {(time.time() - cycle_start) / 3600:.2f}h"
                )
                log(f"[GOAL] {msg}")
                notify(msg)
                state['stop'] = True
                return True

            # ── NOTIF TIAP N PTS ──
            if pts - state['last_notify_pts'] >= NOTIFY_EVERY_PTS:
                gained = pts - state['initial_points']
                notify(f"📈 <b>{pts:.2f} pts</b> (+{gained:.2f} total)\nCycle: {cycle_num} | Claims: {state['claim_count']}")
                state['last_notify_pts'] = pts

            # ── NOTIF PERJAM ──
            now = time.time()
            if now - state['last_hourly_time'] >= 3600:
                elapsed_h = (now - start_time) / 3600
                gained = pts - state['initial_points']
                rate = gained / elapsed_h if elapsed_h > 0 else 0
                remaining = POINTS_TARGET - pts
                eta_h = remaining / rate if rate > 0 else 0
                notify(
                    f"⏰ <b>HOURLY REPORT</b>\n"
                    f"Points: <b>{pts:.2f}</b> / {POINTS_TARGET}\n"
                    f"Gained: +{gained:.2f} pts\n"
                    f"Rate: {rate:.2f} pts/jam\n"
                    f"Cycles: {cycle_num} | Claims: {state['claim_count']}\n"
                    f"Elapsed: {elapsed_h:.1f}h\n"
                    f"ETA target: ~{eta_h:.1f}h lagi"
                )
                state['last_hourly_pts'] = pts
                state['last_hourly_time'] = now

            log(f"  pts: {pts:.2f}")

            # ── AUTO UPDATE ──
            if AUTO_UPDATE and cycle_num % 5 == 0:
                check_and_update()

            # ── FAUCET ──
            if FAUCET_ENABLED and time.time() >= next_faucet_time:
                log("  [faucet] checking...")
                faucet_result = retry_action(lambda: try_claim_faucet(page), tries=2, label="faucet")
                log(f"  [faucet] result: {faucet_result}")
                if faucet_result in ('claimed', 'cooldown'):
                    delay_hours = random.uniform(FAUCET_MIN_HOURS, FAUCET_MAX_HOURS)
                    next_faucet_time = time.time() + delay_hours * 3600
                    log(f"  [faucet] next check in {delay_hours:.1f}h")
                elif faucet_result == 'not-available':
                    next_faucet_time = time.time() + 3600
                else:
                    next_faucet_time = time.time() + 1800
                page.goto(URL_STAKE, wait_until='load', timeout=30000)
                time.sleep(3)

            # ════════════════════════════════════════
            # SEQ: Stake → jeda → Unstake → jeda → Claim
            # ════════════════════════════════════════

            # ── STAKE ──
            log(f"  [SEQ 1/3] Stake {STAKE_AMOUNT} DOHM...")
            r1 = retry_action(lambda: do_stake(page, STAKE_AMOUNT), tries=3, label="stake")
            log(f"  -> {r1}")
            if r1 != 'done':
                log(f"  [!] stake gagal ({r1}), skip cycle")
                notify(f"⚠️ Stake gagal di cycle {cycle_num}: {r1}")
                time.sleep(60)
                continue

            sleep_fixed(JEDA_MIN, JEDA_MAX, "stake→unstake")

            # ── UNSTAKE ──
            log(f"  [SEQ 2/3] Unstake {UNSTAKE_AMOUNT} sDOHM...")
            r2 = retry_action(lambda: do_unstake(page, UNSTAKE_AMOUNT), tries=3, label="unstake")
            log(f"  -> {r2}")

            sleep_fixed(JEDA_MIN, JEDA_MAX, "unstake→claim")

            # ── CLAIM (same thread) ──
            log("  [SEQ 3/3] Claim...")
            r3 = retry_action(lambda: do_claim(page), tries=2, label="claim")
            log(f"  -> {r3}")
            if r3 in ('done', 'skip'):
                state['claim_count'] += 1

            log("  [SEQ] next cycle!")

# ═══════════════════════════════════════════
# SUPERVISOR
# ═══════════════════════════════════════════
if __name__ == '__main__':
    lock = acquire_lock()
    if not lock:
        sys.exit(1)

    restart_count = 0
    while True:
        try:
            done = run_farming_session()
            if done:
                log("[SUPERVISOR] Selesai. Exit.")
                notify(f"✅ <b>DOHM Farm selesai</b>\nClaims: {state['claim_count']}")
                break
            else:
                restart_count += 1
                delay = min(300, 15 * restart_count)
                log(f"[SUPERVISOR] Session end, restart #{restart_count} in {delay}s...")
                notify(f"🔄 <b>Auto-restart #{restart_count}</b>\nDelay: {delay}s")
                time.sleep(delay)
        except KeyboardInterrupt:
            log("[SUPERVISOR] Ctrl+C, exit.")
            notify("🛑 DOHM Farm dihentikan manual.")
            break
        except Exception as e:
            restart_count += 1
            log(f"[SUPERVISOR] Crash #{restart_count}: {e}")
            log(traceback.format_exc())
            notify(f"⚠️ Crash #{restart_count}: {e}")
            delay = min(300, 30 * restart_count)
            log(f"[SUPERVISOR] Restart in {delay}s...")
            time.sleep(delay)
